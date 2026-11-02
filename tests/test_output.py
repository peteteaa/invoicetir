"""pytest for pipeline/output.py: the xlsx and the Sankey, built from tests/fixtures/."""
import openpyxl

from pipeline import output
from pipeline.aggregate import aggregate
from schemas import ProcessedDoc
from test_aggregate import doc


def docs():
    copy = doc("clean_invoice", "duplicate", doc_id="copy")
    copy.duplicate_of, copy.reasons = "clean_invoice", ["same vendor and invoice number 30362538"]
    wrong = doc("printed_math_wrong", "flagged")
    wrong.reasons = ["line 1: 4 x 12.50 != 48.00"]
    return [doc("clean_invoice", vendor="Jacobs, Russell and Perry"),
            doc("receipt_adjustments_no_subtotal", categories=("meals", "meals")),
            doc("receipt_tax_inclusive"), copy, wrong,
            ProcessedDoc(doc_id="blank", source_path="blank.pdf", status="flagged", reasons=["unreadable"])]


def test_xlsx_has_every_sheet_and_exact_amounts(tmp_path):
    path = output.write(docs(), tmp_path)["xlsx"]
    book = openpyxl.load_workbook(path)
    assert book.sheetnames == ["Summary", "By category", "By vendor", "By month", "Line items",
                               "Flagged", "Duplicates"]
    summary = {r[0]: r[2] for r in book["Summary"].iter_rows(min_row=2, max_row=3, values_only=True)}
    assert summary == {"EUR": 16.53, "USD": 23124.05}
    assert book["Summary"]["C2"].number_format == output.MONEY_FORMAT
    flagged = list(book["Flagged"].iter_rows(min_row=2, values_only=True))
    assert [(r[0], r[4]) for r in flagged] == [("printed_math_wrong", "line 1: 4 x 12.50 != 48.00"),
                                               ("blank", "unreadable")]
    assert list(book["Duplicates"].iter_rows(min_row=2, values_only=True)) == [
        ("copy", "clean_invoice", "same vendor and invoice number 30362538")]


def test_line_items_sheet_keeps_doc_id_and_page(tmp_path):
    book = openpyxl.load_workbook(output.write(docs(), tmp_path)["xlsx"])
    rows = list(book["Line items"].iter_rows(values_only=True))
    header = rows[0]
    first = dict(zip(header, rows[1]))
    assert (first["doc_id"], first["page"], first["category"]) == ("clean_invoice", 1, "office")


def test_sankey_flows_add_up_and_group_small_vendors(monkeypatch):
    monkeypatch.setattr(output, "TOP_VENDORS", 1)
    spend = aggregate(docs())
    fig = output.sankey(spend, "USD")
    data = fig.data[0]
    from_total = sum(v for s, v in zip(data.link.source, data.link.value) if s == 0)
    assert round(from_total, 2) == 23099.25 + 21.50 + 1.80 + 1.50  # every bucket is positive here
    assert any(label.startswith("Other vendors (1)") for label in data.node.label)


def test_sankey_html_has_one_figure_per_currency(tmp_path):
    html = output.write(docs(), tmp_path)["sankey"].read_text()
    assert "Spend in EUR" in html and "Spend in USD" in html


def test_empty_run_still_writes_both_files(tmp_path):
    paths = output.write([], tmp_path)
    assert paths["xlsx"].exists() and "No verified invoices" in paths["sankey"].read_text()
