"""Vendor normalization and duplicate detection.

Fuzzy-matches vendor names (rapidfuzz) against the registry in data/state/vendors.json; scores in
the configurable ambiguous band are decided by the model (prompts/vendor_match.md).
Duplicates: same normalized vendor + invoice number, or same vendor + total with dates within 3 days.

The registry maps canonical name -> every name printed for that vendor. The canonical name is the
first one seen. Names are compared by vendor_key(): lowercase, no punctuation, "&" as "and", no
legal suffix (Inc, LLC, GmbH...). Scores are fuzz.ratio: VENDOR_MATCH_HIGH or more is the same
vendor; the model decides when the score is VENDOR_MATCH_LOW or more, or when one name is the
other plus more words ("Starbucks" / "Starbucks Coffee #1234"); anything else is a new vendor.
Every answer adds the name to the registry, so the same name is never asked about twice.

Two invoices with different invoice numbers are never duplicates, even with the same total and
dates: a coffee shop's two receipts for 4.50 on consecutive days are two purchases.
The first invoice of a duplicate group (in input order) is the original. Only verified documents
take part: a flagged one is not counted in the totals, so it can't be an original either.
"""
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from rapidfuzz import fuzz, process

import config
import llm
from pipeline.checks import money
from schemas import Invoice, ProcessedDoc

REGISTRY_PATH = config.ROOT / "data" / "state" / "vendors.json"
DUPLICATE_DAYS = timedelta(days=3)
SUFFIXES = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co", "corp",
            "corporation", "company", "plc", "gmbh", "ag", "sa", "bv", "srl", "pty", "pte"}


class VendorMatch(BaseModel):
    same: bool


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


def _word_prefix(a: str, b: str) -> bool:
    """One name is the other plus more words: 'starbucks' / 'starbucks coffee 1234'."""
    short, long = sorted((a.split(), b.split()), key=len)
    return len(short) < len(long) and long[:len(short)] == short


def _candidate(key: str, aliases: dict):
    """The known name to compare against, with its score, or None.

    fuzz.ratio, not WRatio: WRatio's partial matching scores unrelated names that share words
    ('brooks and sons' / 'jacobs russell and perry' = 86) and merged different companies
    ('gates and sons' / 'yates and sons' = 93). A name that is another plus extra words
    ('starbucks coffee 1234') scores low on ratio, so it is sent to the model as well.
    """
    if not aliases:
        return None
    best = process.extractOne(key, aliases.keys(), scorer=fuzz.ratio)
    if best[1] >= config.VENDOR_MATCH_LOW:
        return best[0], best[1]
    prefixed = next((k for k in aliases if _word_prefix(key, k)), None)
    return (prefixed, fuzz.ratio(key, prefixed)) if prefixed else None


def match_vendor(name: Optional[str], registry: dict, doc_id: Optional[str] = None,
                 model: Optional[str] = None):
    """Return (canonical name, calls) and add name to the registry, in place."""
    if not name:
        return None, []
    aliases = {vendor_key(a): (canonical, a) for canonical, names in registry.items() for a in names}
    key = vendor_key(name)
    canonical, calls = None, []
    candidate = _candidate(key, aliases)
    if candidate:
        known_canonical, known_name = aliases[candidate[0]]
        if candidate[1] >= config.VENDOR_MATCH_HIGH:
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


def _add_usage(doc: ProcessedDoc, calls: list) -> None:
    for c in calls:
        doc.input_tokens += c.input_tokens
        doc.output_tokens += c.output_tokens
        doc.latency_s += c.latency_s
        doc.cost_usd = None if doc.cost_usd is None or c.cost_usd is None else doc.cost_usd + c.cost_usd


def clean(docs: list[ProcessedDoc], registry: Optional[dict] = None, model: Optional[str] = None) -> list:
    """Set vendor on every document with an invoice and mark duplicates, in place.

    A duplicate gets status "duplicate", duplicate_of and the reason. The model calls' tokens,
    latency and cost are added to their document; the calls are also returned. registry is
    updated in place; pass load_registry() to keep vendors across runs.
    """
    registry = {} if registry is None else registry
    calls, originals = [], []
    for doc in docs:
        if doc.invoice is None:
            continue
        doc.vendor, doc_calls = match_vendor(doc.invoice.vendor_name, registry, doc.doc_id, model)
        _add_usage(doc, doc_calls)
        calls += doc_calls
        if doc.status != "verified":
            continue
        duplicate = find_duplicate(doc.invoice, doc.vendor, originals)
        if duplicate:
            doc.status = "duplicate"
            doc.duplicate_of, reason = duplicate
            doc.reasons.append(reason)
        else:
            originals.append((doc.invoice, doc.vendor))
    return calls
