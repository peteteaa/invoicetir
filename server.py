"""Web app for frontend/: `uvicorn server:app --reload`, then open http://localhost:8000

A JSON API over the pipeline and store.py, plus the static frontend. One processing job runs at a
time, in a background thread: new files (uploads, or files in data/docs/ not processed yet) go
through run.extract_all, then clean.py runs over every stored document so duplicates are found
across runs, then categorize.py over the new verified ones. Documents already in the store are
never extracted again.

Money leaves the server as strings ("1284.37"), never floats; the browser parses them only to
draw. Totals always come from aggregate.py over store.effective() documents, so approvals,
exclusions and "not a duplicate" decisions show up everywhere at once.
"""
import io
import threading
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Literal, Optional

import pandas as pd
import pymupdf
import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import store
from pipeline import categorize, clean, ingest, output, run
from pipeline.aggregate import aggregate
from pipeline.checks import check_invoice
from schemas import Invoice, ProcessedDoc

DOCS_DIR = config.ROOT / "data" / "docs"
UPLOAD_DIR = DOCS_DIR / "uploads"
FRONTEND_DIR = config.ROOT / "frontend"
RESULTS_CSV = output.OUT_DIR / "results.csv"
PAGE_DPI = 110
TOP_VENDORS = 12
BUCKETS = {"tax", "adjustments", "rounding", "uncategorized"}  # not spend categories

app = FastAPI(title="Tally")
_job = {"running": False, "done": 0, "total": 0, "started": None, "finished": None, "error": None}
_job_lock = threading.Lock()


def _m(value) -> Optional[str]:
    return None if value is None else str(value)


def _sum(values) -> Decimal:
    return sum(values, Decimal(0))


def _docs() -> list[ProcessedDoc]:
    return [store.effective(s) for s in store.load_all()]


def _source(doc_id: str) -> Path:
    stored = store.get(doc_id)
    if stored is None:
        raise HTTPException(404, f"no document {doc_id}")
    return Path(stored.doc.source_path)


# --- processing --------------------------------------------------------------------------

def _saved(done: int, total: int, doc: ProcessedDoc) -> None:
    store.save(store.StoredDoc(doc=doc))       # shows up in the inbox right away
    _job.update(done=done)


def _process(files: list[Path]) -> None:
    """Each document is saved as soon as it's extracted; vendors, duplicates and categories are
    filled in at the end, over every stored document. A job that dies halfway loses nothing:
    the next one cleans and categorizes what it left behind."""
    try:
        run.extract_all(files, progress=_saved)
        stored = store.load_all()
        registry = clean.load_registry()
        clean.clean([s.doc for s in stored], registry)
        memory = categorize.load_memory()
        for s in stored:
            doc = s.doc
            if doc.status == "verified" and any(item.category is None for item in doc.invoice.line_items):
                run._add_usage(doc, categorize.categorize(doc.invoice, vendor=doc.vendor, memory=memory))
        clean.save_registry(registry)
        categorize.save_memory(memory)
        for s in stored:                       # vendor, duplicate and category changes
            store.save(s)
    except Exception as e:
        _job["error"] = f"{type(e).__name__}: {e}"
    finally:
        _job.update(running=False, finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _require_key() -> None:
    if not config.FIREWORKS_API_KEY:
        raise HTTPException(400, "FIREWORKS_API_KEY is not set: add it to .env and restart the server")


def _start(files: list[Path]) -> dict:
    _require_key()
    with _job_lock:
        if _job["running"]:
            raise HTTPException(409, "a processing job is already running")
        if not files:
            raise HTTPException(400, "no new documents to process")
        _job.update(running=True, done=0, total=len(files), error=None, finished=None,
                    started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    threading.Thread(target=_process, args=(files,), daemon=True).start()
    return dict(_job)


def _unprocessed() -> list[Path]:
    known = {s.doc.doc_id for s in store.load_all()}
    return [f for f in run.find_files([DOCS_DIR]) if f.stem not in known] if DOCS_DIR.is_dir() else []


class ProcessRequest(BaseModel):
    limit: Optional[int] = None   # process only the first N new files (each costs model calls)


@app.post("/api/process")
def process_folder(req: ProcessRequest):
    files = _unprocessed()
    return _start(files[:req.limit] if req.limit else files)


@app.post("/api/upload")
def upload(files: list[UploadFile] = File(...)):
    _require_key()                             # before saving anything
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    taken = {f.stem for f in run.find_files([DOCS_DIR])} | {s.doc.doc_id for s in store.load_all()}
    saved = []
    for f in files:
        name = Path(f.filename or "upload").name
        stem, suffix, n = Path(name).stem, Path(name).suffix, 1
        while stem in taken:                   # doc_id is the file stem and must stay unique
            n += 1
            stem = f"{Path(name).stem}-{n}"
        taken.add(stem)
        path = UPLOAD_DIR / f"{stem}{suffix}"
        path.write_bytes(f.file.read())
        saved.append(path)
    return _start(saved)


@app.get("/api/job")
def job():
    return dict(_job)


# --- documents and review ----------------------------------------------------------------

def _row(stored: store.StoredDoc) -> dict:
    d, inv = store.effective(stored), stored.doc.invoice
    inv = d.invoice or inv
    return {"doc_id": d.doc_id, "file": Path(d.source_path).name, "status": d.status,
            "pipeline_status": stored.doc.status,
            "review": stored.review.action if stored.review else None,
            "vendor": d.vendor or (inv.vendor_name if inv else None),
            "date": inv.invoice_date if inv else None, "currency": inv.currency if inv else None,
            "total": _m(inv.total) if inv else None, "reasons": d.reasons,
            "duplicate_of": d.duplicate_of, "escalated": d.escalated, "model": d.model,
            "cost_usd": _m(d.cost_usd), "latency_s": round(d.latency_s, 2)}


@app.get("/api/docs")
def docs():
    return [_row(s) for s in store.load_all()]


@app.get("/api/docs/{doc_id}")
def doc(doc_id: str):
    stored = store.get(doc_id)
    if stored is None:
        raise HTTPException(404, f"no document {doc_id}")
    current = store.effective(stored)
    path = Path(stored.doc.source_path)
    try:
        with pymupdf.open(path) as f:
            pages = f.page_count
    except Exception:
        pages = 0
    return {"row": _row(stored), "doc": current.model_dump(mode="json"),
            "original": stored.doc.model_dump(mode="json"),
            "review": stored.review.model_dump(mode="json") if stored.review else None,
            "failures": check_invoice(current.invoice) if current.invoice else ["nothing was extracted"],
            "pages": pages}


@app.get("/api/docs/{doc_id}/pages/{page}")
def page(doc_id: str, page: int):
    path = _source(doc_id)
    try:
        png = ingest.render_page(path, page, PAGE_DPI)
    except Exception as e:
        raise HTTPException(404, f"can't render page {page}: {e}")
    return Response(png, media_type="image/png")


@app.get("/api/docs/{doc_id}/file")
def original(doc_id: str):
    path = _source(doc_id)
    if not path.is_file():
        raise HTTPException(404, f"{path.name} is no longer on disk")
    return FileResponse(path)


class CheckRequest(BaseModel):
    invoice: Invoice


@app.post("/api/check")
def check(req: CheckRequest):
    """Live checks while a reviewer edits; nothing is saved."""
    return {"failures": check_invoice(req.invoice), "missing": store.missing_for_totals(req.invoice)}


class ReviewRequest(BaseModel):
    action: Literal["approve", "exclude", "not_duplicate"]
    invoice: Optional[Invoice] = None
    note: Optional[str] = None


def _vendor_and_categories(doc_id: str, invoice: Invoice) -> Optional[str]:
    """Normalize the vendor and fill in missing line-item categories for an approved invoice.
    Uses the saved registry and memory first; the model only for what they don't know. Without
    an API key (or if a call fails) the printed name stays and categories stay empty."""
    registry, vendor = clean.load_registry(), invoice.vendor_name
    try:
        vendor, _ = clean.match_vendor(invoice.vendor_name, registry, doc_id)
        clean.save_registry(registry)
    except Exception:
        pass
    if any(item.category is None for item in invoice.line_items):
        memory, filled = categorize.load_memory(), invoice.model_copy(deep=True)
        try:
            categorize.categorize(filled, vendor=vendor, memory=memory)  # sets every line's category
            categorize.save_memory(memory)
            for item, got in zip(invoice.line_items, filled.line_items):
                item.category = item.category or got.category      # keep the reviewer's choices
        except Exception:
            pass
    return vendor


@app.post("/api/docs/{doc_id}/review")
def post_review(doc_id: str, req: ReviewRequest):
    stored = store.get(doc_id)
    if stored is None:
        raise HTTPException(404, f"no document {doc_id}")
    vendor = None
    if req.action == "approve":
        invoice = req.invoice or store.effective(stored).invoice
        if invoice is not None:
            invoice.doc_id = doc_id
            vendor = _vendor_and_categories(doc_id, invoice)
        req.invoice = invoice
    try:
        store.review(doc_id, req.action, req.invoice, vendor, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return doc(doc_id)


@app.delete("/api/docs/{doc_id}/review")
def delete_review(doc_id: str):
    try:
        store.clear_review(doc_id)
    except KeyError:
        raise HTTPException(404, f"no document {doc_id}")
    return doc(doc_id)


# --- spend -------------------------------------------------------------------------------

def _verified(docs: list[ProcessedDoc], currency: str) -> list[ProcessedDoc]:
    return [d for d in docs if d.status == "verified" and d.invoice.currency == currency]


def _currencies(docs: list[ProcessedDoc]) -> list[str]:
    """Most invoices first."""
    counts = Counter(d.invoice.currency for d in docs if d.status == "verified")
    return [c for c, _ in counts.most_common()]


def _in_month(docs: list[ProcessedDoc], month: Optional[str]) -> list[ProcessedDoc]:
    return docs if not month or month == "all" else [d for d in docs if d.invoice.invoice_date.startswith(month)]


def _previous(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _pick(docs, currency, month):
    currencies = _currencies(docs)
    currency = currency if currency in currencies else (currencies[0] if currencies else None)
    in_currency = _verified(docs, currency) if currency else []
    months = sorted({d.invoice.invoice_date[:7] for d in in_currency})
    if month != "all" and month not in months:
        month = months[-1] if months else "all"   # latest month with invoices, not today's
    return currencies, currency, in_currency, months, month


@app.get("/api/spend")
def spend(currency: Optional[str] = None, month: Optional[str] = None):
    docs = _docs()
    currencies, currency, in_currency, months, month = _pick(docs, currency, month)
    selected = _in_month(in_currency, month)
    result = {"currency": currency, "currencies": currencies, "months": months, "month": month,
              "summary": {"amount": "0", "invoices": 0, "tax": "0", "adjustments": "0", "vendors": 0},
              "previous": None, "by_category": [], "by_vendor": [], "flows": [], "by_month": []}
    if not selected:
        return result

    s = aggregate(selected)
    row = s.summary.iloc[0]
    result["summary"] = {"amount": _m(row.amount), "invoices": int(row.invoices), "tax": _m(row.tax),
                         "adjustments": _m(row.adjustments), "vendors": int(s.by_vendor.vendor.nunique())}
    result["by_category"] = [{"category": r.category, "amount": _m(r.amount)} for r in s.by_category.itertuples()]
    invoices = Counter(d.vendor or d.invoice.vendor_name for d in selected)
    result["by_vendor"] = [{"vendor": r.vendor, "amount": _m(r.amount), "invoices": invoices[r.vendor]}
                           for r in s.by_vendor.itertuples()]

    top = set(s.by_vendor.vendor.head(TOP_VENDORS))
    rows = s.rows[~s.rows.category.isin(BUCKETS - {"uncategorized"})].copy()
    rows["vendor"] = rows.vendor.where(rows.vendor.isin(top), "Other vendors")
    flows = rows.groupby(["category", "vendor"], as_index=False)["amount"].agg(_sum)
    result["flows"] = [{"category": r.category, "vendor": r.vendor, "amount": _m(r.amount)}
                       for r in flows.itertuples() if r.amount > 0]

    by_month = aggregate(in_currency).by_month
    result["by_month"] = [{"month": r.month, "amount": _m(r.amount)} for r in by_month.itertuples()]
    if month != "all":
        prev = _previous(month)
        amounts = {r["month"]: r["amount"] for r in result["by_month"]}
        result["previous"] = {"month": prev, "amount": amounts.get(prev, "0")}
    return result


@app.get("/api/lines")
def lines(currency: Optional[str] = None, month: Optional[str] = None):
    docs = _docs()
    _, currency, in_currency, _, month = _pick(docs, currency, month)
    selected = _in_month(in_currency, month)
    if not selected:
        return []
    items = {(d.doc_id, i): item for d in selected for i, item in enumerate(d.invoice.line_items)}
    out, seen = [], Counter()
    for r in aggregate(selected).rows.itertuples():
        row = {"doc_id": r.doc_id, "page": None if pd.isna(r.page) else int(r.page), "vendor": r.vendor,
               "month": r.month, "category": r.category, "description": r.description,
               "amount": _m(r.amount), "quantity": None, "unit_price": None}
        if r.category not in BUCKETS - {"uncategorized"}:   # a line item: rows keep invoice order
            item = items.get((r.doc_id, seen[r.doc_id]))
            seen[r.doc_id] += 1
            if item:
                row.update(quantity=_m(item.quantity), unit_price=_m(item.unit_price))
        out.append(row)
    return out


@app.get("/api/vendors")
def vendors(currency: Optional[str] = None):
    docs = _docs()
    _, currency, in_currency, _, _ = _pick(docs, currency, "all")
    registry = clean.load_registry()
    all_docs = Counter(d.vendor or d.invoice.vendor_name for d in docs if d.invoice)
    if not in_currency:
        return []
    s = aggregate(in_currency)
    items = s.rows[~s.rows.category.isin(BUCKETS)]
    usual = {v: g.groupby("category")["amount"].agg(_sum).idxmax() for v, g in items.groupby("vendor")}
    counted = Counter(d.vendor or d.invoice.vendor_name for d in in_currency)
    return [{"vendor": r.vendor, "amount": _m(r.amount), "invoices": counted[r.vendor],
             "documents": all_docs[r.vendor], "category": usual.get(r.vendor),
             "aliases": [n for n in registry.get(r.vendor, []) if n != r.vendor]}
            for r in s.by_vendor.itertuples()]


@app.get("/api/export.xlsx")
def export():
    docs = _docs()
    buf = io.BytesIO()
    output.write_xlsx(docs, aggregate(docs), buf)
    return StreamingResponse(io.BytesIO(buf.getvalue()), headers={"Content-Disposition": 'attachment; filename="spend.xlsx"'},
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --- workspace info ----------------------------------------------------------------------

def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(p * len(values)))], 2)


@app.get("/api/stats")
def stats():
    stored = store.load_all()
    docs = [store.effective(s) for s in stored]
    costs = [d.cost_usd for d in docs]
    latencies = [s.doc.latency_s for s in stored if s.doc.latency_s]
    eval_rows = pd.read_csv(RESULTS_CSV).astype(str).to_dict("records") if RESULTS_CSV.exists() else None
    return {"documents": len(docs), "status": dict(Counter(d.status for d in docs)),
            "reviewed": sum(s.review is not None for s in stored),
            "escalated": sum(s.doc.escalated for s in stored),
            "flagged_by_pipeline": sum(s.doc.status == "flagged" for s in stored),
            "cost_usd": _m(_sum(c for c in costs if c is not None)), "cost_unknown": sum(c is None for c in costs),
            "latency_p50": round(median(latencies), 2) if latencies else None,
            "latency_p95": _percentile(latencies, 0.95),
            "input_tokens": sum(d.input_tokens for d in docs), "output_tokens": sum(d.output_tokens for d in docs),
            "models": dict(Counter(d.model for d in docs if d.model)), "eval": eval_rows}


@app.get("/api/meta")
def meta():
    categories = yaml.safe_load((config.ROOT / "categories.yaml").read_text())
    return {"categories": [{"name": n, "description": d, "color": c}
                           for (n, d), c in zip(categories.items(), output.SERIES)],
            "has_api_key": bool(config.FIREWORKS_API_KEY), "unprocessed": len(_unprocessed()),
            "config": {"VISION_MODEL": config.VISION_MODEL, "TEXT_MODEL": config.TEXT_MODEL,
                       "ESCALATION_MODEL": config.ESCALATION_MODEL, "MAX_RETRIES": config.MAX_RETRIES,
                       "ZOOM_ENABLED": config.ZOOM_ENABLED, "WORKERS": config.WORKERS},
            "job": dict(_job)}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
