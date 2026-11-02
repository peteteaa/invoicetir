"""clean.py with a fake llm.call, so no network."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import config
import llm
from pipeline import clean
from schemas import Invoice


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
        if reply is None:
            return SimpleNamespace(parsed=None, error="invalid JSON", text="oops")
        return SimpleNamespace(parsed=schema.model_validate(reply), error=None, text="{}")

    monkeypatch.setattr(llm, "call", call)
    return replies, calls


def inv(doc_id, vendor="Acme Corp", number=None, total="100.00", day="2024-03-01"):
    return Invoice(doc_id=doc_id, vendor_name=vendor, invoice_number=number,
                   total=Decimal(total), invoice_date=day)


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
    results, _ = clean.clean([inv("a", number="INV-001"), inv("b", "ACME CORP.", "inv 001", total="99.00")])
    assert results[1].duplicate_of == "a"
    assert "invoice number" in results[1].reason


def test_same_total_within_3_days_is_a_duplicate(fake_llm):
    results, _ = clean.clean([inv("a"), inv("b", day="2024-03-04"), inv("c", day="2024-03-05")])
    assert [r.duplicate_of for r in results] == [None, "a", None]
    assert results[1].reason == "same vendor and total 100.00, dates 2024-03-01 and 2024-03-04"


def test_different_invoice_numbers_are_never_duplicates(fake_llm):
    results, _ = clean.clean([inv("a", number="1001"), inv("b", number="1002")])
    assert [r.duplicate_of for r in results] == [None, None]


def test_different_vendors_are_never_duplicates(fake_llm):
    results, _ = clean.clean([inv("a"), inv("b", vendor="Zenith Supplies")])
    assert [r.duplicate_of for r in results] == [None, None]


def test_duplicate_points_to_the_original(fake_llm):
    results, _ = clean.clean([inv("a"), inv("b", day="2024-03-02"), inv("c", day="2024-03-03")])
    assert [r.duplicate_of for r in results] == [None, "a", "a"]
