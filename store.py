"""Keeps every processed document and the reviewer's decision about it, so approvals survive a
restart and a rerun never processes the same file twice. Used by server.py.

One JSON file per document in data/state/docs/<doc_id>.json: the ProcessedDoc as the pipeline
left it, plus the review, if any. A review never overwrites the pipeline's version: effective()
applies it on read, so clearing the review restores what the pipeline said.

Review actions:
  approve        count the document. With an edited invoice, that invoice replaces the model's.
                 Allowed when checks still fail (a person decided); the failures stay in reasons.
                 The invoice needs a total, a currency and a valid date, or it can't be totalled.
  exclude        leave the document out of the totals (status flagged).
  not_duplicate  count a document that clean.py marked as a duplicate.
"""
import json
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

import config
from pipeline.checks import check_invoice
from schemas import Invoice, ProcessedDoc

DOCS_DIR = config.ROOT / "data" / "state" / "docs"
COMPARED = ["vendor_name", "invoice_number", "invoice_date", "currency", "subtotal", "tax",
            "tax_inclusive", "total", "line_items", "adjustments"]

_lock = threading.Lock()  # the server writes from request threads and the processing thread


class Review(BaseModel):
    action: Literal["approve", "exclude", "not_duplicate"]
    at: str                                   # ISO 8601, UTC
    note: Optional[str] = None
    invoice: Optional[Invoice] = None         # approve: the reviewer's invoice
    vendor: Optional[str] = None              # approve: normalized vendor for that invoice
    changed_fields: list[str] = []            # fields the reviewer changed, see COMPARED


class StoredDoc(BaseModel):
    doc: ProcessedDoc                         # as the pipeline left it
    review: Optional[Review] = None


def _dir(directory: Optional[Path]) -> Path:
    return directory or DOCS_DIR


def _natural(doc_id: str):
    """invoice_2 before invoice_10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", doc_id)]


def load_all(directory: Optional[Path] = None) -> list[StoredDoc]:
    paths = sorted(_dir(directory).glob("*.json"), key=lambda p: _natural(p.stem))
    return [StoredDoc.model_validate_json(p.read_text()) for p in paths]


def get(doc_id: str, directory: Optional[Path] = None) -> Optional[StoredDoc]:
    path = _dir(directory) / f"{doc_id}.json"
    return StoredDoc.model_validate_json(path.read_text()) if path.exists() else None


def relative(path: str) -> str:
    """Paths inside the repo are stored relative to it, so a store committed on one machine
    works on every clone."""
    try:
        return Path(path).resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return path


def source(doc: ProcessedDoc) -> Path:
    """The document's file on this machine. An absolute path from another clone (stored before
    paths were relative) falls back to the same place under this repo's data/."""
    path = Path(doc.source_path)
    if not path.is_absolute():
        return config.ROOT / path
    parts = path.parts
    if not path.exists() and "data" in parts:
        return config.ROOT.joinpath(*parts[len(parts) - 1 - parts[::-1].index("data"):])
    return path


def save(stored: StoredDoc, directory: Optional[Path] = None) -> None:
    stored.doc.source_path = relative(stored.doc.source_path)
    path = _dir(directory) / f"{stored.doc.doc_id}.json"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stored.model_dump_json(indent=2))


def missing_for_totals(invoice: Invoice) -> list[str]:
    """What aggregate.py needs to count an invoice: a total, a currency and an ISO date."""
    missing = [f for f in ("total", "currency") if getattr(invoice, f) in (None, "")]
    try:
        date.fromisoformat(invoice.invoice_date or "")
    except ValueError:
        missing.append("invoice_date")
    return missing


def changed_fields(before: Optional[Invoice], after: Invoice) -> list[str]:
    if before is None:
        return list(COMPARED)
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return [f for f in COMPARED if old.get(f) != new.get(f)]


def review(doc_id: str, action: str, invoice: Optional[Invoice] = None, vendor: Optional[str] = None,
           note: Optional[str] = None, directory: Optional[Path] = None) -> StoredDoc:
    stored = get(doc_id, directory)
    if stored is None:
        raise KeyError(doc_id)
    if action == "not_duplicate" and stored.doc.status != "duplicate":
        raise ValueError("only a duplicate can be marked as not a duplicate")
    changed = []
    if action == "approve":
        invoice = invoice or stored.doc.invoice
        if invoice is None:
            raise ValueError("nothing was extracted: enter the invoice before approving")
        missing = missing_for_totals(invoice)
        if missing:
            raise ValueError(f"can't count this invoice without: {', '.join(missing)}")
        changed = changed_fields(stored.doc.invoice, invoice)
    stored.review = Review(action=action, at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           note=note, invoice=invoice if action == "approve" else None,
                           vendor=vendor if action == "approve" else None, changed_fields=changed)
    save(stored, directory)
    return stored


def clear_review(doc_id: str, directory: Optional[Path] = None) -> StoredDoc:
    stored = get(doc_id, directory)
    if stored is None:
        raise KeyError(doc_id)
    stored.review = None
    save(stored, directory)
    return stored


def effective(stored: StoredDoc) -> ProcessedDoc:
    """The document with its review applied: what the totals count."""
    doc, r = stored.doc.model_copy(deep=True), stored.review
    if r is None:
        return doc
    suffix = f": {r.note}" if r.note else ""
    if r.action == "exclude":
        doc.status = "flagged"
        doc.reasons.append(f"excluded by reviewer{suffix}")
    elif r.action == "not_duplicate":
        doc.status, doc.duplicate_of = "verified", None
        doc.reasons.append(f"reviewer: not a duplicate{suffix}")
    else:
        doc.invoice = r.invoice
        doc.vendor = r.vendor or doc.vendor or r.invoice.vendor_name
        doc.status, doc.duplicate_of = "verified", None
        failures = check_invoice(r.invoice)
        doc.reasons = [f"approved by reviewer despite: {f}" for f in failures] or [f"approved by reviewer{suffix}"]
    return doc
