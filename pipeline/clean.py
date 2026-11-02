"""Vendor normalization and duplicate detection.

Fuzzy-matches vendor names (rapidfuzz) against the registry in data/state/vendors.json; scores in
the configurable ambiguous band are decided by the model (prompts/vendor_match.md).
Duplicates: same normalized vendor + invoice number, or same vendor + total with dates within 3 days.

The registry maps canonical name -> every name printed for that vendor. The canonical name is the
first one seen. Names are compared by vendor_key(): lowercase, no punctuation, "&" as "and", no
legal suffix (Inc, LLC, GmbH...). A name scoring VENDOR_MATCH_HIGH or more is the same vendor,
below VENDOR_MATCH_LOW a new one, and in between the model decides. Every answer adds the name to
the registry, so the same name is never asked about twice.

Two invoices with different invoice numbers are never duplicates, even with the same total and
dates: a coffee shop's two receipts for 4.50 on consecutive days are two purchases.
The first invoice of a duplicate group (in input order) is the original.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from rapidfuzz import fuzz, process

import config
import llm
from pipeline.checks import money
from schemas import Invoice

REGISTRY_PATH = config.ROOT / "data" / "state" / "vendors.json"
DUPLICATE_DAYS = timedelta(days=3)
SUFFIXES = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co", "corp",
            "corporation", "company", "plc", "gmbh", "ag", "sa", "bv", "srl", "pty", "pte"}


class VendorMatch(BaseModel):
    same: bool


@dataclass
class Cleaned:
    vendor: Optional[str]               # canonical name from the registry; None if none printed
    duplicate_of: Optional[str] = None  # doc_id of the original
    reason: str = ""                    # why it is a duplicate


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_registry(registry: dict, path: Path = REGISTRY_PATH) -> None:
    path.write_text(json.dumps(registry, indent=2, sort_keys=True, ensure_ascii=False))


def vendor_key(name: str) -> str:
    """'Jacobs, Russell & Perry LLC' -> 'jacobs russell and perry'."""
    words = re.sub(r"[^\w\s]", " ", name.lower().replace("&", " and ")).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


def _same_vendor(name: str, known: str, doc_id: Optional[str], model: Optional[str]):
    """Ask the model. Returns (same, calls); an unusable answer after the retries is False."""
    messages = [{"role": "system", "content": (config.PROMPTS_DIR / "vendor_match.md").read_text()},
                {"role": "user", "content": f"Name A: {known}\nName B: {name}"}]
    calls = []
    for _ in range(1 + config.MAX_RETRIES):  # invalid JSON uses the same budget
        c = llm.call(doc_id=doc_id, stage="vendor_match", model=model or config.TEXT_MODEL,
                     messages=messages, schema=VendorMatch)
        calls.append(c)
        if c.parsed is not None:
            return c.parsed.same, calls
        messages += [{"role": "assistant", "content": c.text or ""},
                     {"role": "user", "content": f"Your reply was not usable: {c.error}. Reply again with JSON only."}]
    return False, calls


def match_vendor(name: Optional[str], registry: dict, doc_id: Optional[str] = None,
                 model: Optional[str] = None):
    """Return (canonical name, calls) and add name to the registry, in place."""
    if not name:
        return None, []
    aliases = {vendor_key(a): (canonical, a) for canonical, names in registry.items() for a in names}
    match = process.extractOne(vendor_key(name), aliases.keys(), scorer=fuzz.WRatio,
                               score_cutoff=config.VENDOR_MATCH_LOW) if aliases else None
    canonical, calls = None, []
    if match:
        known_canonical, known_name = aliases[match[0]]
        if match[1] >= config.VENDOR_MATCH_HIGH:
            canonical = known_canonical
        else:
            same, calls = _same_vendor(name, known_name, doc_id, model)
            canonical = known_canonical if same else None
    if canonical is None:
        canonical = name
        registry[canonical] = []
    if name not in registry[canonical]:
        registry[canonical].append(name)
    return canonical, calls


def _invoice_number(value: Optional[str]) -> Optional[str]:
    """'INV-001' and 'inv 001' compare equal."""
    return re.sub(r"[\W_]", "", value or "").upper() or None


def _date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def find_duplicate(invoice: Invoice, vendor: Optional[str], originals: list):
    """originals: (invoice, vendor) pairs kept so far. Returns (doc_id, reason) or None."""
    if vendor is None:
        return None
    number, day = _invoice_number(invoice.invoice_number), _date(invoice.invoice_date)
    for other, other_vendor in originals:
        if other_vendor != vendor:
            continue
        other_number, other_day = _invoice_number(other.invoice_number), _date(other.invoice_date)
        if number and number == other_number:
            return other.doc_id, f"same vendor and invoice number {invoice.invoice_number}"
        if number and other_number:
            continue
        if (invoice.total is not None and invoice.total == other.total and day and other_day
                and abs(day - other_day) <= DUPLICATE_DAYS):
            return other.doc_id, (f"same vendor and total {money(invoice.total)}, "
                                  f"dates {other.invoice_date} and {invoice.invoice_date}")
    return None


def clean(invoices: list[Invoice], registry: Optional[dict] = None, model: Optional[str] = None):
    """Return (one Cleaned per invoice, in order; llm.CallResult of every model call).

    registry is updated in place; pass load_registry() to keep vendors across runs.
    """
    registry = {} if registry is None else registry
    results, calls, originals = [], [], []
    for inv in invoices:
        vendor, c = match_vendor(inv.vendor_name, registry, inv.doc_id, model)
        calls += c
        duplicate = find_duplicate(inv, vendor, originals)
        if duplicate:
            results.append(Cleaned(vendor, *duplicate))
        else:
            results.append(Cleaned(vendor))
            originals.append((inv, vendor))
    return results, calls
