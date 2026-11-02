"""Exact ground truth for the "Samples of electronic invoices" template, from the PDF text layer.

All 991 sample invoices are one generated layout, so their printed values can be parsed with fixed
rules instead of labelled by hand: invoice number, date, seller, every line item (description
with its wrapped lines, Qty, Net price, Net worth), and the SUMMARY totals. This only covers
that template. Real documents need labels from label_app.py.

Parsing uses the same layout text the text model reads (pipeline/ingest.py), so an ingest bug
that dropped a word would be invisible here. Numbers are parsed deterministically:
"7 847,32" -> Decimal("7847.32"). Currency is USD: "$" is the only symbol any of them prints.
"""
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional

from pipeline import ingest
from pipeline.checks import check_invoice
from schemas import Label, LineItem


def _num(text: str) -> Decimal:
    return Decimal(text.replace("$", "").replace(" ", "").replace(",", "."))


def _columns(line: str) -> list[str]:
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def is_template(text: str) -> bool:
    return ("Invoice no:" in text and "Date of issue:" in text and "Seller:" in text
            and "SUMMARY" in text and "Net worth" in text)


def parse(text: str, doc_id: str = "") -> Label:
    """Ground truth for one template invoice. Raises ValueError if the text doesn't fit."""
    if not is_template(text):
        raise ValueError(f"{doc_id}: not the sample-invoice template")
    lines = text.splitlines()
    month, day, year = re.search(r"Date of issue:\s+(\d\d)/(\d\d)/(\d{4})", text).groups()
    seller = next(i for i, l in enumerate(lines) if l.strip().startswith("Seller:"))
    start = next(i for i, l in enumerate(lines) if "Description" in l and "Qty" in l) + 2
    end = next(i for i, l in enumerate(lines) if l.strip() == "SUMMARY")

    items = []
    for line in lines[start:end]:
        row = re.match(r"\s*\d+\.\s+(.*)$", line)
        if row:
            cols = _columns(row[1])  # description..., Qty, UM, Net price, Net worth, VAT %, Gross
            if len(cols) < 7:
                raise ValueError(f"{doc_id}: line item row has {len(cols)} columns: {line.strip()!r}")
            items.append(LineItem(description=" ".join(cols[:-6]), quantity=_num(cols[-6]),
                                  unit_price=_num(cols[-4]), amount=_num(cols[-3]), page=1))
        elif items and line.strip():
            items[-1].description += " " + line.strip()  # wrapped description line

    totals = _columns(next(l for l in lines if l.strip().startswith("Total")))
    label = Label(
        doc_id=doc_id, vendor_name=_columns(lines[seller + 1])[0],
        invoice_number=re.search(r"Invoice no:\s*(\S+)", text)[1],
        invoice_date_printed=f"{month}/{day}/{year}", invoice_date=f"{year}-{month}-{day}",
        currency_printed="$", currency="USD", line_items=items, adjustments=[],
        subtotal=_num(totals[1]), tax=_num(totals[2]), tax_inclusive=False, total=_num(totals[3]),
        source_pages=[1])
    # The template's own printed math is always right; if a parsed invoice fails the checks,
    # it was parsed wrong, so refuse it rather than score against it.
    failures = check_invoice(label)
    if failures:
        raise ValueError(f"{doc_id}: parsed ground truth fails checks: {failures}")
    label.expected_flag = False
    return label


def truth_for(path: Path) -> Optional[Label]:
    """Ground truth for a template PDF, or None if it isn't one (or isn't a PDF with text)."""
    try:
        doc = ingest.load(path)
    except Exception:
        return None
    if doc.kind != "text" or not is_template(doc.text()):
        return None
    return parse(doc.text(), doc.doc_id)
