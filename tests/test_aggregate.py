"""pytest for pipeline/aggregate.py, using handcrafted JSON fixtures in tests/fixtures/ (not documents).
"""
import json
from decimal import Decimal
from pathlib import Path

from pipeline.aggregate import aggregate
from schemas import Invoice, ProcessedDoc

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal


def doc(name: str, status: str = "verified", categories=("office", "office"), vendor=None,
        doc_id=None, **changes) -> ProcessedDoc:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    data.update(changes)
    for item, category in zip(data["line_items"], categories):
        item["category"] = category
    invoice = Invoice.model_validate(data)
    return ProcessedDoc(doc_id=doc_id or name, source_path=f"{name}.pdf", status=status,
                        invoice=invoice, vendor=vendor)


def table(df, key):
    return {(r.currency, getattr(r, key)): r.amount for r in df.itertuples()}


def test_categories_tax_and_adjustments_add_up_to_the_printed_total():
    spend = aggregate([doc("receipt_adjustments_no_subtotal", categories=("meals", "meals"))])
    assert table(spend.by_category, "category") == {
        ("USD", "meals"): D("21.50"), ("USD", "adjustments"): D("1.50"), ("USD", "tax"): D("1.80")}
    assert spend.summary.to_dict("records") == [
        {"currency": "USD", "invoices": 1, "amount": D("24.80"), "tax": D("1.80"), "adjustments": D("1.50")}]


def test_tax_inclusive_tax_is_not_added_again():
    spend = aggregate([doc("receipt_tax_inclusive")])
    assert table(spend.by_category, "category") == {("EUR", "office"): D("16.53")}


def test_a_cent_off_printed_total_goes_to_rounding():
    spend = aggregate([doc("clean_invoice", total="23099.26")])  # checks allow a cent
    assert table(spend.by_category, "category")[("USD", "rounding")] == D("0.01")
    assert spend.summary.amount[0] == D("23099.26")


def test_flagged_and_duplicates_are_left_out_but_counted():
    spend = aggregate([doc("clean_invoice"), doc("clean_invoice", "duplicate", doc_id="copy"),
                       doc("printed_math_wrong", "flagged")])
    assert spend.summary.amount.tolist() == [D("23099.25")]
    assert (spend.flagged, spend.duplicates) == (1, 1)
    assert set(spend.rows.doc_id) == {"clean_invoice"}


def test_currencies_are_kept_apart():
    spend = aggregate([doc("clean_invoice"), doc("receipt_tax_inclusive")])
    assert dict(zip(spend.summary.currency, spend.summary.amount)) == {"EUR": D("16.53"), "USD": D("23099.25")}


def test_vendor_and_month_use_the_normalized_vendor_and_invoice_date():
    spend = aggregate([
        doc("clean_invoice", vendor="Jacobs, Russell and Perry"),
        doc("clean_invoice", doc_id="b", vendor="Jacobs, Russell and Perry", vendor_name="JACOBS RUSSELL & PERRY",
            invoice_number="2", invoice_date="2013-09-01"),
    ])
    assert table(spend.by_vendor, "vendor") == {("USD", "Jacobs, Russell and Perry"): D("46198.50")}
    assert table(spend.by_month, "month") == {("USD", "2013-08"): D("23099.25"), ("USD", "2013-09"): D("23099.25")}


def test_rows_keep_doc_id_and_page():
    rows = aggregate([doc("clean_invoice")]).rows
    lines = rows[rows.category == "office"]
    assert lines[["doc_id", "page"]].values.tolist() == [["clean_invoice", 1], ["clean_invoice", 1]]


def test_no_verified_documents():
    spend = aggregate([doc("printed_math_wrong", "flagged")])
    assert spend.rows.empty and spend.by_category.empty and spend.summary.empty
    assert spend.flagged == 1
