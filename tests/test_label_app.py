"""label_app.py's helpers: corrected-field tracking, editor rows, document order. No UI, no model."""
import json
from pathlib import Path

import pandas as pd

import label_app

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_unchanged_label_has_no_corrections_even_if_formatted_differently():
    pre = fixture("clean_invoice")
    label = fixture("clean_invoice")
    label["line_items"][1]["quantity"] = "4.00"   # same value as "4"
    label["doc_id"] = "something else"             # set by code, not compared
    assert label_app.corrected_fields(pre, label) == []


def test_changed_added_and_removed_fields_are_corrections():
    pre = fixture("clean_invoice")
    label = fixture("clean_invoice")
    label["total"] = "23099.26"
    label["line_items"][0]["category"] = "office"
    label["adjustments"] = [{"label": "Shipping", "amount": "5.00"}]
    assert label_app.corrected_fields(pre, label) == [
        "adjustments[0].amount", "adjustments[0].label", "line_items[0].category", "total"]


def test_failed_prefill_counts_every_filled_field():
    label = {"vendor_name": "Acme", "total": "10.00"}
    assert label_app.corrected_fields(None, label) == ["total", "vendor_name"]


def test_table_rows_drops_empty_rows_and_blanks_become_none():
    df = pd.DataFrame([{"description": " Paper ", "quantity": "", "amount": "4.00", "page": 1.0},
                       {"description": None, "quantity": "  ", "amount": None, "page": pd.NA}])
    assert label_app.table_rows(df, ["description", "quantity", "amount", "page"]) == [
        {"description": "Paper", "quantity": None, "amount": "4.00", "page": 1}]


def test_documents_are_in_natural_order(tmp_path):
    for name in ["invoice_10.pdf", "invoice_2.pdf", "receipt.png", "notes.txt"]:
        (tmp_path / name).write_bytes(b"")
    assert list(label_app.documents(tmp_path)) == ["invoice_2", "invoice_10", "receipt"]


def test_next_unlabeled_wraps_around():
    ids = ["a", "b", "c", "d"]
    assert label_app.next_unlabeled(ids, "c", {"a", "c", "d"}) == "b"
    assert label_app.next_unlabeled(ids, "a", set(ids)) == "a"
