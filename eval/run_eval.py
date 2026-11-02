"""`python -m eval.run_eval --config all`

Runs only on documents that have labels in data/labels/. Configs:
  small_raw        small model, no checks
  small_checks     small model + checks + retries
  small_escalate   small model + checks + retries + escalation
  large_per_doc    large model per document, no checks
  large_all        large model given all documents at once, asked for the spend summary directly
  claude_per_doc   Claude (frontier baseline, default Opus 5.5) per document, no checks
  claude_all       Claude given all documents at once, asked for the spend summary directly
The Claude configs need ANTHROPIC_API_KEY; with --config all they are skipped without one.
Writes out/results.csv and out/accuracy_vs_cost.html (total spend accuracy vs cost per document,
one point per config), plus
out/eval_details.csv (one row per document per config).

Ground truth (--source): "labels" = data/labels/<doc_id>.json from label_app.py; "template" =
eval/template_truth.py for the sample-invoice template; "auto" (default) = labels if there are
any, else template. --limit picks a seeded sample (default 50; 0 = all): large_per_doc costs
about $0.015 per invoice. --as-images renders each PDF to a PNG first, to test the vision model.

How the scores are defined (see eval/metrics.py):
- Field and line-item accuracy compare the final extraction with the truth, even when the
  pipeline flagged the document: they measure reading, not the decision.
- Total spend accuracy compares the money with the printed totals, ignoring categories.
- Spend scores use only labels that say what the true spend is (metrics.spendable: a total and
  an amount for every line); "spend_docs" says how many. The rest still count for fields.
- Dollar error per category counts verified documents only, so spend the pipeline flagged is
  missing from its totals. "coverage" is the share of true spend in verified documents.
- To measure extraction rather than categorizer disagreement, a per-document config's line
  items take the category of the true item they pair with. The true items are categorized
  once (unless the labels carry categories) by pipeline/categorize.py; that cost per document
  is added to every per-document config, since the real pipeline pays it too. large_all
  categorizes for itself, so its error includes disagreeing with that categorizer.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import random
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
import plotly.graph_objects as go
from pydantic import BaseModel, create_model

import config
import llm
from eval import claude_baseline, metrics, template_truth
from pipeline import categorize, clean, ingest, run
from pipeline.extract import document_message
from schemas import Label, Money, ProcessedDoc

OUT_DIR = config.ROOT / "out"
LABELS_DIR = config.ROOT / "data" / "labels"
DOCS_DIR = config.ROOT / "data" / "docs"
BATCH_DOCS = int(config._env("EVAL_BATCH_DOCS", "25"))   # documents per large_all request

PER_DOC = {
    "small_raw": {"use_checks": False, "escalate": False},
    "small_checks": {"use_checks": True, "escalate": False},
    "small_escalate": {"use_checks": True, "escalate": True},
    "large_per_doc": {"use_checks": False, "escalate": False, "large": True},
    "claude_per_doc": {"claude": True},
}
SUMMARY = {"large_all": "kimi", "claude_all": "claude"}   # all documents at once
CONFIGS = [*PER_DOC, *SUMMARY]
CLAUDE_CONFIGS = {"claude_per_doc", "claude_all"}


@dataclass
class Case:
    doc_id: str
    path: Path            # what the pipeline reads (a rendered PNG with --as-images)
    truth: Label


# --- cases ----------------------------------------------------------------------------------

def load_cases(source: str, limit: int, seed: int, as_images: bool, out_dir: Path, only: str = "") -> list[Case]:
    files = {p.stem: p for p in run.find_files([DOCS_DIR])} if DOCS_DIR.is_dir() else {}
    label_files = sorted(LABELS_DIR.glob("*.json")) if LABELS_DIR.is_dir() else []
    if source == "auto":
        source = "labels" if label_files else "template"

    cases = []
    if source == "labels":
        for f in label_files:
            label = Label.model_validate_json(f.read_text())
            doc_id = label.doc_id or f.stem
            if doc_id not in files:
                print(f"skipping label {f.name}: no document {doc_id} in {DOCS_DIR}", file=sys.stderr)
                continue
            cases.append(Case(doc_id, files[doc_id], label))
    else:
        for doc_id, path in sorted(files.items()):
            truth = template_truth.truth_for(path)
            if truth is not None:
                cases.append(Case(doc_id, path, truth))
    if only:  # e.g. "cord_": keep one dataset, so real receipts and synthetic invoices don't mix
        cases = [c for c in cases if c.doc_id.startswith(only)]
    if not cases:
        raise SystemExit(f"No documents with ground truth (source: {source}{', only ' + only if only else ''}).")

    if limit and limit < len(cases):
        cases = sorted(random.Random(seed).sample(cases, limit), key=lambda c: c.doc_id)
    if as_images:
        image_dir = out_dir / "eval_images"
        image_dir.mkdir(parents=True, exist_ok=True)
        for c in cases:
            if c.path.suffix.lower() == ".pdf":
                png = image_dir / f"{c.doc_id}.png"
                if not png.exists():
                    png.write_bytes(ingest.render_page(c.path, 1, config.RENDER_DPI))
                c.path = png
    print(f"{len(cases)} documents, ground truth from {source}" + (", as images" if as_images else ""))
    return cases


def categorize_truth(cases: list[Case]) -> Optional[Decimal]:
    """Give every true line item a category; return the categorizer's cost per document."""
    memory, calls = {}, []
    for c in cases:
        if all(i.category for i in c.truth.line_items):
            continue
        calls += categorize.categorize(c.truth, memory=memory)
    costs = [x.cost_usd for x in calls]
    if None in costs:
        return None
    return sum(costs, Decimal(0)) / len(cases)


# --- per-document configs -------------------------------------------------------------------

def _claude_doc(case: Case) -> ProcessedDoc:
    """claude_per_doc: one Claude extraction, no checks (the same rule as large_per_doc)."""
    try:
        doc = ingest.load(case.path, case.doc_id)
    except Exception as e:
        return ProcessedDoc(doc_id=case.doc_id, source_path=str(case.path), status="flagged",
                            cost_usd=Decimal(0), reasons=[f"could not load the file: {e}"])
    result, error, usage = claude_baseline.extract(doc)
    ok = result is not None and result.invoice is not None
    return ProcessedDoc(doc_id=case.doc_id, source_path=str(case.path), status="verified" if ok else "flagged",
                        invoice=result.invoice if result else None,
                        uncertain_fields=list(result.uncertain_fields) if result else [],
                        reasons=[] if ok else [error or "no invoice in the answer"], model=claude_baseline.MODEL,
                        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                        latency_s=usage["latency_s"], cost_usd=usage["cost_usd"])


def run_per_doc(name: str, cases: list[Case]) -> list[ProcessedDoc]:
    opts = dict(PER_DOC[name])
    if opts.pop("claude", False):
        with ThreadPoolExecutor(max(1, config.WORKERS)) as pool:
            docs = list(pool.map(_claude_doc, cases))
        print(f"  {name} [{len(docs)}/{len(cases)}] done", flush=True)
        return docs
    model = config.ESCALATION_MODEL if opts.pop("large", False) else None
    docs = run.extract_all([c.path for c in cases], model=model, **opts,
                           progress=lambda done, total, d: print(f"  {name} [{done}/{total}] {d.doc_id}: {d.status}",
                                                                 flush=True) if done % 10 == 0 or done == total else None)
    for d, c in zip(docs, cases):
        d.doc_id = c.doc_id
    if any(c.truth.is_duplicate_of for c in cases):
        clean.clean(docs, {})          # only needed to score duplicate recall
    return docs


def score_per_doc(name: str, docs: list[ProcessedDoc], cases: list[Case],
                  categorize_cost: Optional[Decimal]) -> tuple[dict, list[dict]]:
    fields = {f: [] for f in metrics.FIELDS}
    details, item_correct, item_total = [], 0, 0
    pred_for_spend, truth_for_spend = [], []
    for d, c in zip(docs, cases):
        got = metrics.field_results(d.invoice, c.truth)
        for f, ok in got.items():
            fields[f].append(ok)
        correct, total = metrics.line_item_counts(d.invoice, c.truth)
        item_correct += correct
        item_total += total
        if metrics.spendable(c.truth):   # else the label can't say what the true spend is
            truth_for_spend.append(ProcessedDoc(doc_id=c.doc_id, source_path=str(c.path), status="verified",
                                                invoice=c.truth))
        if d.invoice is not None and metrics.spendable(c.truth):
            pred_for_spend.append(d.model_copy(update={"invoice": metrics.with_truth_categories(d.invoice, c.truth)}))
        kind = metrics.error_kind(d.invoice, c.truth)
        details.append({"config": name, "doc_id": c.doc_id, "status": d.status, "escalated": d.escalated,
                        "fields_correct": sum(got.values()), "fields": len(got),
                        "items_correct": correct, "items": total,
                        "fully_correct": kind == "none",
                        "money_correct": kind != "money",
                        "error_kind": kind,   # none | text (money right) | money
                        "truth_total": c.truth.total,
                        "pred_total": d.invoice.total if d.invoice else None,
                        "cost_usd": d.cost_usd, "latency_s": round(d.latency_s, 2),
                        "reasons": "; ".join(d.reasons)})

    truth_spend = metrics.category_spend(truth_for_spend)
    pred_spend = metrics.category_spend(pred_for_spend)
    verified_ids = {d.doc_id for d in docs if d.status == "verified"}
    covered = metrics.category_spend([t for t in truth_for_spend if t.doc_id in verified_ids])
    # Trust: how right is what the config verified, and how wrong is what it flagged.
    verified_rows = [x for x in details if x["status"] == "verified"]
    flagged_rows = [x for x in details if x["status"] == "flagged"]
    verified_spend_acc = metrics.total_accuracy(pred_spend, covered) if covered else None
    flags = metrics.precision_recall([d.status == "flagged" for d in docs], [c.truth.expected_flag for c in cases])
    dups = metrics.precision_recall([d.status == "duplicate" for d in docs],
                                    [bool(c.truth.is_duplicate_of) if any(x.truth.is_duplicate_of for x in cases) else None
                                     for c in cases])
    costs = [d.cost_usd for d in docs]
    cost = None if None in costs or categorize_cost is None else \
        float(sum(costs, Decimal(0)) / len(docs) + categorize_cost)
    all_fields = [ok for v in fields.values() for ok in v]
    row = {
        "config": name, "docs": len(docs),
        "field_accuracy": sum(all_fields) / len(all_fields) if all_fields else None,
        "line_item_accuracy": item_correct / item_total if item_total else None,
        "docs_fully_correct": sum(x["fully_correct"] for x in details) / len(details),
        "total_spend_accuracy": metrics.total_accuracy(pred_spend, truth_spend),
        "spend_accuracy": metrics.spend_accuracy(pred_spend, truth_spend),
        "spend_docs": len(truth_for_spend),
        "dollar_error": float(sum(metrics.dollar_error(pred_spend, truth_spend).values(), Decimal(0))),
        "verified_spend_accuracy": verified_spend_acc,
        "verified_money_correct": (sum(x["money_correct"] for x in verified_rows) / len(verified_rows)) if verified_rows else None,
        "flagged_money_wrong": (sum(not x["money_correct"] for x in flagged_rows) / len(flagged_rows)) if flagged_rows else None,
        "money_errors_verified": sum(not x["money_correct"] for x in verified_rows),
        "coverage": float(sum(covered.values(), Decimal(0)) / sum(truth_spend.values(), Decimal(0))),
        "verified": len(verified_ids), "flagged": sum(d.status == "flagged" for d in docs),
        "escalated": sum(d.escalated for d in docs),
        "flag_precision": flags["precision"], "flag_recall": flags["recall"],
        "false_flag_rate": flags["false_positive_rate"], "duplicate_recall": dups["recall"],
        "cost_per_doc_usd": cost,
        "p50_latency_s": metrics.percentile([d.latency_s for d in docs], 50),
        "p95_latency_s": metrics.percentile([d.latency_s for d in docs], 95),
        **{f"acc_{f}": (sum(v) / len(v) if v else None) for f, v in fields.items()},
        "dollar_error_by_category": _by_category(metrics.dollar_error(pred_spend, truth_spend)),
    }
    return row, details


def _by_category(errors: dict) -> str:
    return json.dumps({f"{cur} {cat}": f"{amount:.2f}" for (cur, cat), amount in sorted(errors.items()) if amount})


# --- large_all ------------------------------------------------------------------------------

def summary_schema(names: list[str]) -> type[BaseModel]:
    row = create_model("SpendRow", currency=(str, ...), category=(Literal[tuple(names)], ...), amount=(Money, ...))
    return create_model("SpendSummary", spend=(list[row], ...))


def run_large_all(cases: list[Case], provider: str = "kimi") -> tuple[dict, dict]:
    """Ask ESCALATION_MODEL (provider "kimi") or Claude ("claude") for the spend summary of every
    document, BATCH_DOCS per request."""
    categories = categorize.load_categories()
    listing = "\n".join(f"- {n}: {d}" for n, d in categories.items())
    system = (config.PROMPTS_DIR / "summary.md").read_text().replace("{categories}", listing)
    schema = summary_schema([*categories, "tax", "adjustments"])
    spend, calls, failed = {}, [], []
    batches = [cases[i:i + BATCH_DOCS] for i in range(0, len(cases), BATCH_DOCS)]
    for n, batch in enumerate(batches, 1):
        if provider == "claude":
            user = [b for c in batch for b in claude_baseline.content(ingest.load(c.path, c.doc_id))]
            user.append({"type": "text", "text": f"That was all {len(batch)} documents."})
            parsed, error, t_in, t_out, latency, cost = claude_baseline.call(
                doc_id=f"batch{n}", stage="claude_all", system=system, user=user, schema=schema)
            calls.append(SimpleNamespace(cost_usd=cost, latency_s=latency))
            if parsed is None:
                print(f"  claude_all batch {n} failed: {error}", flush=True)
                failed.append(n)
                continue
            for r in parsed.spend:
                spend[r.currency, r.category] = spend.get((r.currency, r.category), Decimal(0)) + r.amount
            print(f"  claude_all batch {n}/{len(batches)} done", flush=True)
            continue
        content = []
        for c in batch:
            part = document_message(ingest.load(c.path, c.doc_id))["content"]
            content += [{"type": "text", "text": part}] if isinstance(part, str) else part
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": content + [{"type": "text", "text": f"That was all {len(batch)} documents."}]}]
        parsed = None
        for _ in range(1 + config.MAX_RETRIES):
            try:
                c = llm.call(doc_id=f"batch{n}", stage="large_all", model=config.ESCALATION_MODEL,
                             messages=messages, schema=schema)
            except Exception as e:  # e.g. a request over the size limit: a failed batch, not a lost eval
                print(f"  large_all batch {n} failed: {type(e).__name__}: {e}", flush=True)
                break
            calls.append(c)
            if c.parsed is not None:
                parsed = c.parsed
                break
            messages += [{"role": "assistant", "content": c.text or ""},
                         {"role": "user", "content": f"Your reply was not usable: {c.error}. Reply again with JSON only."}]
        if parsed is None:
            failed.append(n)
            continue
        for r in parsed.spend:
            spend[r.currency, r.category] = spend.get((r.currency, r.category), Decimal(0)) + r.amount
        print(f"  large_all batch {n}/{len(batches)} done", flush=True)
    costs = [c.cost_usd for c in calls]
    usage = {"calls": len(calls), "batches": len(batches), "failed_batches": failed,
             "cost": None if None in costs else sum(costs, Decimal(0)),
             "latency": [c.latency_s for c in calls]}
    return spend, usage


def score_large_all(spend: dict, usage: dict, cases: list[Case], name: str = "large_all") -> dict:
    truth_spend = metrics.category_spend([ProcessedDoc(doc_id=c.doc_id, source_path=str(c.path), status="verified",
                                                       invoice=c.truth) for c in cases])
    errors = metrics.dollar_error(spend, truth_spend)
    per_doc_latency = sum(usage["latency"]) / len(cases)
    return {
        "config": name, "docs": len(cases), "field_accuracy": None, "line_item_accuracy": None,
        "docs_fully_correct": None, "spend_docs": len(cases),
        "total_spend_accuracy": metrics.total_accuracy(spend, truth_spend),
        "spend_accuracy": metrics.spend_accuracy(spend, truth_spend),
        "dollar_error": float(sum(errors.values(), Decimal(0))),
        "coverage": None if usage["failed_batches"] else 1.0,
        "verified": None, "flagged": None, "escalated": None, "flag_precision": None, "flag_recall": None,
        "false_flag_rate": None, "duplicate_recall": None,
        "cost_per_doc_usd": None if usage["cost"] is None else float(usage["cost"] / len(cases)),
        # one request covers many documents, so per-document latency is the request time shared out
        "p50_latency_s": per_doc_latency, "p95_latency_s": per_doc_latency,
        **{f"acc_{f}": None for f in metrics.FIELDS},
        "dollar_error_by_category": _by_category(errors),
        "notes": f"{usage['batches']} request(s) of up to {BATCH_DOCS} documents"
                 + (f"; failed batches {usage['failed_batches']}" if usage["failed_batches"] else ""),
    }


# --- output ---------------------------------------------------------------------------------

def chart(results: pd.DataFrame, path: Path) -> Path:
    """Total spend accuracy vs cost per document, one point per config.

    Total spend accuracy only compares money with the printed totals. Accuracy by category is in
    the hover: its "true" categories come from the categorizer, not from people (unless labels
    carry categories), so moving money between categories there is disagreement, not proof of
    an error."""
    rows = results.dropna(subset=["cost_per_doc_usd", "total_spend_accuracy"])
    fig = go.Figure(go.Scatter(
        x=rows.cost_per_doc_usd, y=rows.total_spend_accuracy * 100, mode="markers+text", text=rows.config,
        textposition="top center", marker={"size": 12, "color": "#2a78d6"},
        customdata=rows[["spend_accuracy", "field_accuracy", "coverage", "p50_latency_s"]].astype(float).fillna(-1).values,
        hovertemplate=("%{text}<br>total spend accuracy %{y:.2f}%<br>cost/doc $%{x:.5f}<br>"
                       "spend accuracy by category %{customdata[0]:.3f}<br>field accuracy %{customdata[1]:.3f}<br>"
                       "coverage %{customdata[2]:.3f}<br>p50 latency %{customdata[3]:.1f}s<extra></extra>")))
    fig.update_layout(title=f"Total spend accuracy vs cost per document ({int(results.docs.max())} documents)",
                      xaxis={"title": "cost per document (USD, log scale)", "type": "log"},
                      yaxis={"title": "total spend accuracy (%)"}, paper_bgcolor="#fcfcfb", plot_bgcolor="#fcfcfb",
                      font={"color": "#0b0b0b"}, margin={"l": 64, "r": 24, "t": 56, "b": 56})
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(path, include_plotlyjs=True)
    return path


def save_predictions(docs: list[ProcessedDoc], folder: Path) -> None:
    """Every document's final answer, so it can be re-scored later without new model calls."""
    folder.mkdir(parents=True, exist_ok=True)
    for d in docs:
        (folder / f"{d.doc_id}.json").write_text(d.model_dump_json(indent=1))


def load_predictions(cases: list[Case], folder: Path) -> list[ProcessedDoc]:
    missing = [c.doc_id for c in cases if not (folder / f"{c.doc_id}.json").exists()]
    if missing:
        raise SystemExit(f"--rescore: {len(missing)} predictions missing in {folder}, e.g. {missing[:3]}")
    return [ProcessedDoc.model_validate_json((folder / f"{c.doc_id}.json").read_text()) for c in cases]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare the pipeline configs on labelled documents.")
    ap.add_argument("--config", default="all", help=f"all, or a comma list of {', '.join(CONFIGS)}")
    ap.add_argument("--source", default="auto", choices=["auto", "labels", "template"])
    ap.add_argument("--limit", type=int, default=50, help="seeded sample size; 0 = every labelled document")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rescore", action="store_true",
                    help="score the predictions saved in OUT/predictions again, without extraction calls "
                         "(per-document configs; summary configs keep their row from OUT/results.csv)")
    ap.add_argument("--only", default="", help="only doc_ids starting with this, e.g. cord_")
    ap.add_argument("--as-images", action="store_true", help="render PDFs to PNG (tests the vision model)")
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args(argv)

    names = CONFIGS if args.config == "all" else [c.strip() for c in args.config.split(",")]
    unknown = set(names) - set(CONFIGS)
    if unknown:
        ap.error(f"unknown config(s): {', '.join(sorted(unknown))}")
    if CLAUDE_CONFIGS & set(names) and not claude_baseline.has_credentials():
        if args.config == "all":
            print("skipping claude_per_doc and claude_all: no Anthropic credential (set ANTHROPIC_API_KEY in .env)")
            names = [n for n in names if n not in CLAUDE_CONFIGS]
        else:
            ap.error("the Claude configs need ANTHROPIC_API_KEY in .env (or an `ant auth login` profile)")
    out = Path(args.out)
    cases = load_cases(args.source, args.limit, args.seed, args.as_images, out, args.only)

    rows, details = [], []
    categorize_cost = None
    if any(n in PER_DOC for n in names):
        print("categorizing ground-truth line items")
        categorize_cost = categorize_truth(cases)
    for name in names:
        start = time.monotonic()
        print(f"{name}:")
        if name in SUMMARY and args.rescore:
            previous = pd.read_csv(out / "results.csv") if (out / "results.csv").exists() else None
            if previous is not None and name in set(previous.config):
                rows.append(previous[previous.config == name].iloc[0].to_dict())
                print("  kept from the previous results.csv")
            else:
                print("  nothing saved to rescore; skipped")
        elif name in SUMMARY:
            # it only produces spend, so it only runs on labels that say what the spend is
            spend_cases = [c for c in cases if metrics.spendable(c.truth)]
            if len(spend_cases) < len(cases):
                print(f"  {len(cases) - len(spend_cases)} documents left out: their labels lack a total or an amount")
            spend, usage = run_large_all(spend_cases, SUMMARY[name])
            rows.append(score_large_all(spend, usage, spend_cases, name))
        else:
            if args.rescore:   # score saved answers again: no model calls
                docs = load_predictions(cases, out / "predictions" / name)
            else:
                docs = run_per_doc(name, cases)
                save_predictions(docs, out / "predictions" / name)
            row, d = score_per_doc(name, docs, cases, categorize_cost)
            rows.append(row)
            details += d
        print(f"  {time.monotonic() - start:.0f}s")

    results = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    results.to_csv(out / "results.csv", index=False)
    pd.DataFrame(details).to_csv(out / "eval_details.csv", index=False)
    chart(results, out / "accuracy_vs_cost.html")

    show = ["config", "line_item_accuracy", "docs_fully_correct", "total_spend_accuracy", "coverage",
            "verified_spend_accuracy", "verified_money_correct", "flagged_money_wrong", "flagged", "escalated",
            "cost_per_doc_usd", "p50_latency_s"]
    with pd.option_context("display.width", 200, "display.float_format", "{:.4f}".format):
        print("\n" + results[show].to_string(index=False))
    print(f"\n{out / 'results.csv'}\n{out / 'eval_details.csv'}\n{out / 'accuracy_vs_cost.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
