"""eval/metrics.py and eval/template_truth.py. Pure; the template test reads data/docs (skips without)."""
from decimal import Decimal as D
from pathlib import Path

import pytest

from eval import metrics, template_truth
from schemas import Invoice, LineItem, ProcessedDoc

from sample_docs import DOCS  # noqa: E402


def item(desc, amount, qty="1", price=None, category=None):
    return LineItem(description=desc, quantity=D(qty), unit_price=D(price or amount), amount=D(amount),
                    category=category)


def inv(items, **kw):
    base = dict(vendor_name="Acme", invoice_number="1", invoice_date="2024-01-02", currency="USD",
                subtotal=sum((i.amount for i in items), D(0)), tax=D("0.00"), total=sum((i.amount for i in items), D(0)))
    return Invoice(line_items=items, **{**base, **kw})


def verified(invoice, doc_id="d"):
    return ProcessedDoc(doc_id=doc_id, source_path="x", status="verified", invoice=invoice)


def test_fields_compare_exactly_and_skip_unlabelled():
    truth = inv([item("A", "10.00")], vendor_name=None)
    got = metrics.field_results(inv([item("A", "10.00")], vendor_name="Anything", total=D("10.01")), truth)
    assert "vendor_name" not in got and got["total"] is False and got["invoice_number"] is True


def test_skipped_row_costs_one_item_not_every_row_after_it():
    truth = inv([item("A", "1.00"), item("B", "2.00"), item("C", "3.00"), item("D", "4.00")])
    pred = inv([item("A", "1.00"), item("C", "3.00"), item("D", "4.00")])
    assert metrics.line_item_counts(pred, truth) == (3, 4)


def test_misread_amount_is_paired_by_description_and_counted_wrong():
    truth = inv([item("Laptop stand", "45.00")])
    pred = inv([item("Laptop stand", "54.00")])
    assert metrics.pair_items(pred.line_items, truth.line_items) == [(0, 0)]
    assert metrics.line_item_counts(pred, truth) == (0, 1)


def test_truth_categories_are_copied_by_pairing():
    truth = inv([item("Flight", "300.00", category="travel"), item("Lunch", "20.00", category="meals")])
    pred = inv([item("Lunch", "20.00"), item("Taxi", "15.00")])
    assert [i.category for i in metrics.with_truth_categories(pred, truth).line_items] == ["meals", metrics.UNMATCHED]


def test_spend_accuracy_and_total_accuracy():
    truth = metrics.category_spend([verified(inv([item("Flight", "300.00", category="travel"),
                                                  item("Lunch", "100.00", category="meals")]))])
    moved = {("USD", "travel"): D("400.00")}                       # right money, wrong category
    assert metrics.total_accuracy(moved, truth) == 1.0
    assert metrics.spend_accuracy(moved, truth) == 0.5             # 100 too much + 100 missing
    assert metrics.total_accuracy({("USD", "travel"): D("300.00")}, truth) == 0.75


def test_flagged_and_incomplete_documents_add_no_spend():
    good = inv([item("A", "10.00", category="office")])
    no_total = good.model_copy(update={"total": None})
    docs = [verified(good), ProcessedDoc(doc_id="f", source_path="x", status="flagged", invoice=good),
            verified(no_total, "n")]
    assert metrics.category_spend(docs) == {("USD", "office"): D("10.00")}


def test_precision_recall_ignores_unlabelled():
    r = metrics.precision_recall([True, True, False, True], [True, False, True, None])
    assert (r["labelled"], r["precision"], r["recall"], r["false_positive_rate"]) == (3, 0.5, 0.5, 1.0)
    assert metrics.precision_recall([True], [None])["precision"] is None


def test_percentile_nearest_rank():
    assert metrics.percentile([5, 1, 3, 2, 4], 50) == 3
    assert metrics.percentile(list(range(1, 101)), 95) == 95
    assert metrics.percentile([], 50) is None


@pytest.mark.skipif(not DOCS.is_dir(), reason="needs data/docs")
def test_template_truth_parses_wrapped_descriptions_and_totals():
    label = template_truth.truth_for(DOCS / "invoice_31.pdf")
    assert (label.vendor_name, label.invoice_date, label.total) == ("Sanchez, Foster and Peterson", "2014-09-07", D("125.77"))
    assert label.line_items[-1].description == "Steam Yacht Varuna, Brand New, Free shipping in the US"
    assert label.expected_flag is False
