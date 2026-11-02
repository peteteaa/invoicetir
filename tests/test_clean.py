"""pipeline/clean.py. No mocks: tests that never reach the model run anywhere (they assert no
call was made), and tests that need the model call Fireworks and skip without FIREWORKS_API_KEY."""
from decimal import Decimal

import pytest

import config
from pipeline import clean
from schemas import Invoice, ProcessedDoc

live = pytest.mark.skipif(not config.FIREWORKS_API_KEY, reason="needs FIREWORKS_API_KEY")


def doc(doc_id, vendor="Acme Corp", number=None, total="100.00", day="2024-03-01", status="verified"):
    invoice = Invoice(doc_id=doc_id, vendor_name=vendor, invoice_number=number,
                      total=Decimal(total), invoice_date=day)
    return ProcessedDoc(doc_id=doc_id, source_path=f"{doc_id}.pdf", status=status, invoice=invoice,
                        cost_usd=Decimal("0.001"))


def duplicates(docs):
    assert clean.clean(docs) == []  # these names never need the model
    return [d.duplicate_of for d in docs]


# --- vendors, no model ----------------------------------------------------------------------

def test_vendor_key_ignores_punctuation_ampersand_and_suffix():
    assert clean.vendor_key("Jacobs Russell & Perry, LLC") == clean.vendor_key("Jacobs, Russell and Perry")


def test_same_name_formatted_differently_merges_without_the_model():
    registry = {"Jacobs, Russell and Perry": ["Jacobs, Russell and Perry"]}
    canonical, calls = clean.match_vendor("JACOBS RUSSELL & PERRY INC.", registry)
    assert (canonical, calls) == ("Jacobs, Russell and Perry", [])
    assert registry["Jacobs, Russell and Perry"] == ["Jacobs, Russell and Perry", "JACOBS RUSSELL & PERRY INC."]


def test_distant_name_is_a_new_vendor_without_the_model():
    registry = {"Acme Corp": ["Acme Corp"]}
    assert clean.match_vendor("Zenith Supplies", registry) == ("Zenith Supplies", [])
    assert set(registry) == {"Acme Corp", "Zenith Supplies"}


@pytest.mark.parametrize("name, known", [
    ("Gates and Sons", "Yates and Sons"),        # WRatio 93: was merged without asking
    ("Martin Group", "Martinez Group"),
    ("Starbucks Coffee #1234", "Starbucks"),     # ratio 60, but one name is the other plus words
])
def test_close_but_different_names_go_to_the_model_not_auto_merged(name, known):
    aliases = {clean.vendor_key(known): (known, known)}
    candidate = clean._candidate(clean.vendor_key(name), aliases)
    assert candidate is not None
    assert candidate[1] < config.VENDOR_MATCH_HIGH


def test_names_sharing_only_common_words_are_not_candidates():
    aliases = {clean.vendor_key("Jacobs, Russell and Perry"): ("x", "x")}
    assert clean._candidate(clean.vendor_key("Brooks and Sons"), aliases) is None  # WRatio gave 86


# --- vendors, live model --------------------------------------------------------------------

@live
def test_model_merges_a_store_number_variant_and_is_asked_once():
    registry = {"Starbucks": ["Starbucks"]}
    canonical, calls = clean.match_vendor("Starbucks Coffee #1234", registry)
    assert canonical == "Starbucks" and len(calls) == 1
    assert clean.match_vendor("Starbucks Coffee #1234", registry) == ("Starbucks", [])  # remembered


@live
def test_model_keeps_different_companies_apart():
    registry = {"Smith Ltd": ["Smith Ltd"]}
    canonical, calls = clean.match_vendor("Smith, Bailey and Ortiz", registry)
    assert canonical == "Smith, Bailey and Ortiz" and len(calls) == 1


@live
def test_model_call_usage_is_added_to_its_document():
    docs = [doc("a", vendor="Starbucks"), doc("b", vendor="Starbucks Coffee #1234")]
    calls = clean.clean(docs)
    assert len(calls) == 1
    assert docs[0].input_tokens == 0
    assert docs[1].input_tokens == calls[0].input_tokens > 0
    assert docs[1].cost_usd == Decimal("0.001") + calls[0].cost_usd


# --- duplicates, no model -------------------------------------------------------------------

def test_same_invoice_number_with_different_formatting_is_a_duplicate():
    docs = [doc("a", number="INV-001"), doc("b", "ACME CORP.", "inv 001", total="99.00")]
    assert duplicates(docs) == [None, "a"]
    assert docs[1].status == "duplicate"
    assert "invoice number" in docs[1].reasons[0]


def test_same_total_within_3_days_is_a_duplicate():
    docs = [doc("a"), doc("b", day="2024-03-04"), doc("c", day="2024-03-05")]
    assert duplicates(docs) == [None, "a", None]
    assert docs[1].reasons == ["same vendor and total 100.00, dates 2024-03-01 and 2024-03-04"]


def test_different_invoice_numbers_are_never_duplicates():
    assert duplicates([doc("a", number="1001"), doc("b", number="1002")]) == [None, None]


def test_different_vendors_are_never_duplicates():
    assert duplicates([doc("a"), doc("b", vendor="Zenith Supplies")]) == [None, None]


def test_duplicate_points_to_the_original():
    assert duplicates([doc("a"), doc("b", day="2024-03-02"), doc("c", day="2024-03-03")]) == [None, "a", "a"]


def test_flagged_document_gets_a_vendor_but_is_never_an_original():
    docs = [doc("a", vendor="ACME CORP.", status="flagged"), doc("b"), ProcessedDoc(
        doc_id="c", source_path="c.pdf", status="flagged")]
    assert duplicates(docs) == [None, None, None]
    assert [d.vendor for d in docs] == ["ACME CORP.", "ACME CORP.", None]
    assert [d.status for d in docs] == ["flagged", "verified", "flagged"]
