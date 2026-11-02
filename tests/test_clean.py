"""clean.py with a fake llm.call, so no network."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import config
import llm
from pipeline import clean
from schemas import Invoice, ProcessedDoc


@pytest.fixture
def fake_llm(monkeypatch):
    """Queue replies (a dict, or None for invalid JSON); returns (replies, messages of each call)."""
    monkeypatch.setattr(config, "VENDOR_MATCH_LOW", 80)
    monkeypatch.setattr(config, "VENDOR_MATCH_HIGH", 92)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    monkeypatch.setattr(config, "TEXT_MODEL", "fake-model")
    replies, calls = [], []

    def call(*, messages, schema, **kwargs):
        calls.append(messages)
        reply = replies.pop(0)
        usage = {"input_tokens": 50, "output_tokens": 5, "latency_s": 0.5, "cost_usd": Decimal("0.0001")}
        if reply is None:
            return SimpleNamespace(parsed=None, error="invalid JSON", text="oops", **usage)
        return SimpleNamespace(parsed=schema.model_validate(reply), error=None, text="{}", **usage)

    monkeypatch.setattr(llm, "call", call)
    return replies, calls


def doc(doc_id, vendor="Acme Corp", number=None, total="100.00", day="2024-03-01", status="verified"):
    invoice = Invoice(doc_id=doc_id, vendor_name=vendor, invoice_number=number,
                      total=Decimal(total), invoice_date=day)
    return ProcessedDoc(doc_id=doc_id, source_path=f"{doc_id}.pdf", status=status, invoice=invoice,
                        cost_usd=Decimal("0.001"))


def duplicates(docs):
    clean.clean(docs)
    return [d.duplicate_of for d in docs]


# --- vendors --------------------------------------------------------------------------------

def test_vendor_key_ignores_punctuation_ampersand_and_suffix():
    assert clean.vendor_key("Jacobs Russell & Perry, LLC") == clean.vendor_key("Jacobs, Russell and Perry")


def test_close_name_merges_without_the_model(fake_llm):
    _, calls = fake_llm
    registry = {"Jacobs, Russell and Perry": ["Jacobs, Russell and Perry"]}
    assert clean.match_vendor("JACOBS RUSSELL & PERRY INC.", registry)[0] == "Jacobs, Russell and Perry"
    assert registry["Jacobs, Russell and Perry"] == ["Jacobs, Russell and Perry", "JACOBS RUSSELL & PERRY INC."]
    assert not calls


def test_distant_name_is_a_new_vendor_without_the_model(fake_llm):
    _, calls = fake_llm
    registry = {"Acme Corp": ["Acme Corp"]}
    assert clean.match_vendor("Zenith Supplies", registry)[0] == "Zenith Supplies"
    assert set(registry) == {"Acme Corp", "Zenith Supplies"}
    assert not calls


@pytest.mark.parametrize("same, expected", [(True, "Starbucks"), (False, "Starbucks Coffee #1234")])
def test_ambiguous_name_is_decided_by_the_model(fake_llm, same, expected):
    replies, calls = fake_llm
    replies.append({"same": same})
    registry = {"Starbucks": ["Starbucks"]}
    assert clean.match_vendor("Starbucks Coffee #1234", registry)[0] == expected
    assert len(calls) == 1
    # answered once: the name is now in the registry, so it is never asked about again
    assert clean.match_vendor("Starbucks Coffee #1234", registry)[0] == expected
    assert len(calls) == 1


def test_unusable_model_answers_keep_vendors_apart(fake_llm):
    replies, calls = fake_llm
    replies += [None, None, None]
    registry = {"Starbucks": ["Starbucks"]}
    assert clean.match_vendor("Starbucks Coffee #1234", registry)[0] == "Starbucks Coffee #1234"
    assert len(calls) == 3


# --- duplicates -----------------------------------------------------------------------------

def test_same_invoice_number_with_different_formatting_is_a_duplicate(fake_llm):
    docs = [doc("a", number="INV-001"), doc("b", "ACME CORP.", "inv 001", total="99.00")]
    assert duplicates(docs) == [None, "a"]
    assert docs[1].status == "duplicate"
    assert "invoice number" in docs[1].reasons[0]


def test_same_total_within_3_days_is_a_duplicate(fake_llm):
    docs = [doc("a"), doc("b", day="2024-03-04"), doc("c", day="2024-03-05")]
    assert duplicates(docs) == [None, "a", None]
    assert docs[1].reasons == ["same vendor and total 100.00, dates 2024-03-01 and 2024-03-04"]


def test_different_invoice_numbers_are_never_duplicates(fake_llm):
    assert duplicates([doc("a", number="1001"), doc("b", number="1002")]) == [None, None]


def test_different_vendors_are_never_duplicates(fake_llm):
    assert duplicates([doc("a"), doc("b", vendor="Zenith Supplies")]) == [None, None]


def test_duplicate_points_to_the_original(fake_llm):
    assert duplicates([doc("a"), doc("b", day="2024-03-02"), doc("c", day="2024-03-03")]) == [None, "a", "a"]


def test_flagged_document_gets_a_vendor_but_is_never_an_original(fake_llm):
    docs = [doc("a", vendor="ACME CORP.", status="flagged"), doc("b"), ProcessedDoc(
        doc_id="c", source_path="c.pdf", status="flagged")]
    assert duplicates(docs) == [None, None, None]
    assert [d.vendor for d in docs] == ["ACME CORP.", "ACME CORP.", None]
    assert [d.status for d in docs] == ["flagged", "verified", "flagged"]


def test_model_call_usage_is_added_to_its_document(fake_llm):
    replies, _ = fake_llm
    replies.append({"same": True})
    docs = [doc("a", vendor="Starbucks"), doc("b", vendor="Starbucks Coffee #1234")]
    calls = clean.clean(docs)
    assert len(calls) == 1
    assert (docs[1].input_tokens, docs[1].output_tokens, docs[1].latency_s, docs[1].cost_usd) == (
        50, 5, 0.5, Decimal("0.0011"))
    assert docs[0].input_tokens == 0
