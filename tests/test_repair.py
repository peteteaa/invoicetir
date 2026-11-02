"""pipeline/repair.py.

The material-field rule is a pure function. The loop itself is tested live against Fireworks on
real documents; those tests skip when FIREWORKS_API_KEY is unset or data/docs is missing.
Cost per run: about $0.02.
"""
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

import config
from pipeline import ingest, repair
from schemas import ExtractionResult, Invoice, LineItem

DOCS = Path(__file__).resolve().parent.parent / "data" / "docs" / "Samples of electronic invoices"


@pytest.mark.parametrize("fields, material", [
    (["currency", "invoice_date"], []),                                   # normalization rules only
    (["invoice_number"], []),
    (["total"], ["total"]),
    (["vendor_name", "currency"], ["vendor_name"]),
    (["line_items[2].amount", "line_items[0].description"], ["line_items[2].amount", "line_items[0].description"]),
    (["line_items[1].category", "line_items[1].page"], []),
    (["line_items"], ["line_items"]),
    (["adjustments[0].amount", "tax"], ["adjustments[0].amount", "tax"]),
])
def test_material_uncertain(fields, material):
    assert repair.material_uncertain(ExtractionResult(uncertain_fields=fields)) == material


def test_material_uncertain_without_result():
    assert repair.material_uncertain(None) == []


def test_fields_not_printed_are_not_material():
    """A receipt with no subtotal, no tax and no quantity is not a reason to escalate: checks.py
    decides whether a missing value is a failure. A printed value the model doubts still is."""
    invoice = Invoice(vendor_name="Blue Door Cafe", total=Decimal("12.00"), line_items=[
        LineItem(description="Avocado toast", quantity=None, unit_price=None, amount=Decimal("12.00"))])
    result = ExtractionResult(invoice=invoice, uncertain_fields=[
        "subtotal", "tax", "line_items[0].quantity", "line_items[0].unit_price",
        "line_items[0].amount", "total", "line_items[5].amount"])
    assert repair.material_uncertain(result) == ["line_items[0].amount", "total", "line_items[5].amount"]


live = pytest.mark.skipif(not config.FIREWORKS_API_KEY or not DOCS.is_dir(),
                          reason="needs FIREWORKS_API_KEY and data/docs")


@live
def test_clean_invoice_is_verified_in_one_call():
    outcome = repair.run(ingest.load(DOCS / "invoice_22.pdf"))
    assert outcome.status == "verified"
    assert len(outcome.attempts) == 1 and not outcome.escalated
    assert outcome.result.invoice.vendor_name == "Jenkins-Christian"


@live
def test_wrong_printed_math_is_flagged_not_fixed(tmp_path):
    """A real invoice with one printed line amount changed: 4 x 3 288,00 printed as 13 125,00.
    The models must copy the printed value; the loop must flag the document, never guess."""
    pdf = pymupdf.open(DOCS / "invoice_106.pdf")
    page = pdf[0]
    rect = page.search_for("13 152,00")[0]
    page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions()
    page.insert_text((rect.x0, rect.y1 - 1.5), "13 125,00", fontsize=rect.height * 0.78, fontname="helv")
    path = tmp_path / "invoice_106_badmath.pdf"
    pdf.save(path)

    outcome = repair.run(ingest.load(path))
    assert outcome.status == "flagged"
    assert outcome.escalated
    assert len(outcome.attempts) == config.MAX_RETRIES + 2       # small model attempts + escalation
    assert any("line 2: 4 x 3288.00 != 13125.00" in r for r in outcome.reasons)
    amounts = [i.amount for i in outcome.result.invoice.line_items]
    assert str(amounts[1]) == "13125.00"                          # copied as printed, not fixed
