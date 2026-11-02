"""Runs the pipeline stages in order over a set of documents. Used by app.py and eval/run_eval.py.

    ingest -> extract + checks + repair (per document, WORKERS at a time)
           -> clean (vendors, duplicates) -> categorize (verified only) -> aggregate -> output

Every document comes out as a ProcessedDoc, whatever happens to it: a file that can't be read or
an API failure after the client's retries becomes a flagged document with the reason, so one bad
file never stops a batch. Model-call tokens, latency and cost are added to the document that
caused them.

Command line:
    python -m pipeline.run data/docs              # everything under a folder
    python -m pipeline.run a.pdf b.jpg --out out/ --fresh
--fresh ignores and doesn't update the vendor registry and category memory in data/state/.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

import config
from pipeline import categorize, clean, ingest, output, repair
from pipeline.aggregate import Spend, aggregate
from schemas import ProcessedDoc


@dataclass
class RunResult:
    docs: list[ProcessedDoc]
    spend: Spend
    files: dict[str, Path] = field(default_factory=dict)  # "xlsx", "sankey"

    @property
    def cost_usd(self):
        costs = [d.cost_usd for d in self.docs]
        return None if None in costs else sum(costs)


def _add_usage(doc: ProcessedDoc, calls: list) -> None:
    for c in calls:
        doc.input_tokens += c.input_tokens
        doc.output_tokens += c.output_tokens
        doc.latency_s += c.latency_s
        doc.cost_usd = None if doc.cost_usd is None or c.cost_usd is None else doc.cost_usd + c.cost_usd


def from_outcome(doc: ingest.Document, outcome: repair.Outcome) -> ProcessedDoc:
    result = outcome.result
    return ProcessedDoc(
        doc_id=doc.doc_id, source_path=str(doc.path), status=outcome.status,
        invoice=result.invoice if result else None,
        uncertain_fields=list(result.uncertain_fields) if result else [],
        reasons=list(outcome.reasons), model=outcome.model, escalated=outcome.escalated,
        input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
        latency_s=outcome.latency_s, cost_usd=outcome.cost_usd)


def process(path: Path, doc_id: Optional[str] = None, use_checks: bool = True,
            escalate: bool = True, model: Optional[str] = None) -> ProcessedDoc:
    """Load and extract one file through the repair loop. Never raises for a bad file or an API
    failure: those come back as flagged documents."""
    doc_id = doc_id or Path(path).stem
    try:
        doc = ingest.load(path, doc_id)
    except Exception as e:
        return ProcessedDoc(doc_id=doc_id, source_path=str(path), status="flagged", cost_usd=Decimal(0),
                            reasons=[f"could not load the file: {type(e).__name__}: {e}"])
    try:
        return from_outcome(doc, repair.run(doc, model=model, use_checks=use_checks, escalate=escalate))
    except Exception as e:
        # cost unknown (None): calls were made and logged before the failure
        return ProcessedDoc(doc_id=doc_id, source_path=str(path), status="flagged",
                            reasons=[f"model API failed: {type(e).__name__}: {e}"])


def find_files(inputs) -> list[Path]:
    """Files and folders (searched recursively) -> supported files, sorted, doc_ids unique."""
    files = []
    for p in map(Path, inputs):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*")
                            if f.is_file() and f.suffix.lower() in ingest.PDF_SUFFIXES | ingest.IMAGE_SUFFIXES)
        else:
            files.append(p)
    seen = {}
    for f in files:
        if f.stem in seen:
            raise ValueError(f"two files would get doc_id {f.stem!r}: {seen[f.stem]} and {f}")
        seen[f.stem] = f
    return files


def extract_all(files: list[Path], workers: int = config.WORKERS,
                progress: Optional[Callable[[int, int, ProcessedDoc], None]] = None,
                **options) -> list[ProcessedDoc]:
    """process() every file, `workers` at a time, keeping input order. options go to process()."""
    docs = [None] * len(files)
    with ThreadPoolExecutor(max(1, workers)) as pool:
        futures = {pool.submit(process, f, **options): i for i, f in enumerate(files)}
        for done, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            docs[i] = future.result()
            if progress:
                progress(done, len(files), docs[i])
    return docs


def run(inputs, out_dir: Path = output.OUT_DIR, persist_state: bool = True,
        workers: int = config.WORKERS, write_files: bool = True,
        progress: Optional[Callable[[int, int, ProcessedDoc], None]] = None) -> RunResult:
    """The whole pipeline over files and/or folders. Returns the documents, the spend tables
    and the paths of the files written."""
    docs = extract_all(find_files(inputs), workers=workers, progress=progress)

    registry = clean.load_registry() if persist_state else {}
    clean.clean(docs, registry)          # adds its call usage to each document itself

    memory = categorize.load_memory() if persist_state else {}
    for doc in docs:
        if doc.status == "verified":
            _add_usage(doc, categorize.categorize(doc.invoice, vendor=doc.vendor, memory=memory))

    if persist_state:
        clean.save_registry(registry)
        categorize.save_memory(memory)

    spend = aggregate(docs)
    files = {}
    if write_files:
        out_dir = Path(out_dir)
        files = {"xlsx": output.write_xlsx(docs, spend, out_dir / "spend.xlsx"),
                 "sankey": output.write_sankey(spend, out_dir / "sankey.html")}
    return RunResult(docs, spend, files)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract, check, clean, categorize and aggregate invoices.")
    ap.add_argument("inputs", nargs="+", help="files and/or folders of PDFs and images")
    ap.add_argument("--out", default=str(output.OUT_DIR), help="output folder (default out/)")
    ap.add_argument("--workers", type=int, default=config.WORKERS, help="documents processed at once")
    ap.add_argument("--fresh", action="store_true", help="don't read or update data/state/")
    args = ap.parse_args(argv)

    def progress(done, total, doc):
        print(f"[{done}/{total}] {doc.doc_id}: {doc.status}"
              + (f" ({'; '.join(doc.reasons)})" if doc.reasons else ""), flush=True)

    result = run(args.inputs, out_dir=Path(args.out), persist_state=not args.fresh,
                 workers=args.workers, progress=progress)
    counts = {s: sum(d.status == s for d in result.docs) for s in ("verified", "flagged", "duplicate")}
    print(f"\n{len(result.docs)} documents: {counts['verified']} verified, {counts['flagged']} flagged, "
          f"{counts['duplicate']} duplicate | escalated {sum(d.escalated for d in result.docs)} | "
          f"cost ${result.cost_usd if result.cost_usd is not None else '?'}")
    print(result.spend.summary.to_string(index=False))
    for name, path in result.files.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
