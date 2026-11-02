"""pytest for pipeline/checks.py, using handcrafted JSON fixtures in tests/fixtures/ (not documents)."""
import json
from pathlib import Path

import pytest

from pipeline.checks import check_invoice, check_result
from schemas import ExtractionResult, Invoice

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str, **changes) -> Invoice:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    data.update(changes)
    return Invoice.model_validate(data)


def with_line(name: str, n: int, **changes) -> Invoice:
    """The fixture with line n (1-based) changed."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    data["line_items"][n - 1].update(changes)
    return Invoice.model_validate(data)


# --- documents whose math is right -------------------------------------------------------

@pytest.mark.parametrize("name", ["clean_invoice", "receipt_adjustments_no_subtotal", "receipt_tax_inclusive"])
def test_correct_documents_pass(name):
    assert check_invoice(load(name)) == []


def test_sub_cent_line_product_is_within_tolerance():
    # 0.5 x 3.125 = 1.5625, printed as 1.56
    assert check_invoice(load("receipt_tax_inclusive")) == []


# --- line math -----------------------------------------------------------------------------

def test_wrong_line_math_matches_spec_example():
    assert check_invoice(load("printed_math_wrong")) == ["line 1: 4 x 12.50 != 48.00"]


def test_line_off_by_two_cents_fails():
    failures = check_invoice(with_line("clean_invoice", 2, amount="13152.02"))
    assert "line 2: 4 x 3288.00 != 13152.02" in failures


def test_line_without_unit_price_skips_product_check():
    assert check_invoice(with_line("clean_invoice", 1, unit_price=None)) == []


def test_missing_line_amount_is_one_failure_not_a_cascade():
    assert check_invoice(with_line("clean_invoice", 1, amount=None)) == ["line 1: amount missing"]


# --- sums ----------------------------------------------------------------------------------

def test_wrong_subtotal():
    failures = check_invoice(load("clean_invoice", subtotal="20999.00", total="23098.93"))
    assert failures == ["sum of line amounts 20999.32 != subtotal 20999.00"]


def test_wrong_total():
    assert check_invoice(load("clean_invoice", total="23099.52")) == [
        "subtotal 20999.32 + tax 2099.93 = 23099.25 != total 23099.52"]


def test_total_one_cent_off_passes():
    assert check_invoice(load("clean_invoice", total="23099.26")) == []


def test_missing_tax_counts_as_zero():
    assert check_invoice(load("clean_invoice", tax=None, total="20999.32")) == []
    assert check_invoice(load("clean_invoice", tax=None)) == [
        "subtotal 20999.32 = 20999.32 != total 23099.25"]


def test_no_subtotal_uses_lines_adjustments_and_tax():
    failures = check_invoice(load("receipt_adjustments_no_subtotal", total="26.80"))
    assert failures == ["sum of line amounts 21.50 + adjustments 1.50 + tax 1.80 = 24.80 != total 26.80"]


def test_tax_inclusive_ignores_tax_in_total():
    # Adding the tax would give 19.17; tax is already inside the prices.
    failures = check_invoice(load("receipt_tax_inclusive", tax_inclusive=False))
    assert failures == ["subtotal 16.53 + tax 2.64 = 19.17 != total 16.53"]
    assert check_invoice(load("receipt_tax_inclusive")) == []


def test_missing_adjustment_amount():
    data = json.loads((FIXTURES / "receipt_adjustments_no_subtotal.json").read_text())
    data["adjustments"][1]["amount"] = None
    assert check_invoice(Invoice.model_validate(data)) == ["adjustment 2 (Tip): amount missing"]


# --- dates, currency, required fields -----------------------------------------------------

@pytest.mark.parametrize("value", ["2013-13-08", "2013-02-30", "08/12/2013", "2013-8-12", "yesterday"])
def test_invalid_dates(value):
    assert check_invoice(load("clean_invoice", invoice_date=value)) == [
        f"invoice_date {value!r} is not a valid YYYY-MM-DD date"]


def test_leap_day_is_valid():
    assert check_invoice(load("clean_invoice", invoice_date="2024-02-29")) == []


@pytest.mark.parametrize("value", ["$", "usd", "US", "XXX", "EURO"])
def test_invalid_currency(value):
    assert check_invoice(load("clean_invoice", currency=value)) == [
        f"currency {value!r} is not an ISO 4217 code"]


@pytest.mark.parametrize("field, message", [
    ("vendor_name", "vendor_name missing"),
    ("invoice_date", "invoice_date missing"),
    ("currency", "currency missing"),
    ("total", "total missing"),
])
def test_missing_required_field(field, message):
    assert check_invoice(load("clean_invoice", **{field: None})) == [message]


def test_blank_vendor_counts_as_missing():
    assert check_invoice(load("clean_invoice", vendor_name="   ")) == ["vendor_name missing"]


def test_no_line_items():
    assert check_invoice(load("clean_invoice", line_items=[])) == ["no line items"]


def test_invoice_number_is_optional():
    assert check_invoice(load("clean_invoice", invoice_number=None)) == []


def test_several_failures_are_all_reported():
    invoice = load("printed_math_wrong", currency="usd", invoice_date=None)
    assert check_invoice(invoice) == [
        "invoice_date missing",
        "currency 'usd' is not an ISO 4217 code",
        "line 1: 4 x 12.50 != 48.00",
    ]


# --- whole extraction results --------------------------------------------------------------

def test_check_result_handles_missing_and_unreadable():
    assert check_result(None) == ["no valid extraction"]
    assert check_result(ExtractionResult(unreadable=True)) == ["model reported the document as unreadable"]
    assert check_result(ExtractionResult(invoice=None)) == ["no invoice in the extraction"]
    assert check_result(ExtractionResult(invoice=load("clean_invoice"))) == []


# --- configurable required fields -----------------------------------------------------------

def test_required_fields_can_be_relaxed_but_present_values_must_still_be_valid():
    no_header = load("clean_invoice", vendor_name=None, invoice_date=None)
    assert check_invoice(no_header, required=["total"]) == []
    assert check_invoice(load("clean_invoice", invoice_date="2013-13-08"), required=["total"]) == [
        "invoice_date '2013-13-08' is not a valid YYYY-MM-DD date"]


@pytest.mark.parametrize("failure, absent", [
    ("vendor_name missing", True), ("total missing", True), ("line 3: amount missing", True),
    ("adjustment 2 (Tip): amount missing", True), ("no line items", False),
    ("line 1: 4 x 12.50 != 48.00", False), ("currency 'usd' is not an ISO 4217 code", False),
])
def test_missing_field_pattern(failure, absent):
    from pipeline.checks import MISSING_FIELD
    assert bool(MISSING_FIELD.match(failure)) is absent
