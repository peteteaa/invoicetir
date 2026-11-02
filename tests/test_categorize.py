"""categorize.py with a fake llm.call, so no network."""
from decimal import Decimal
from types import SimpleNamespace

import pydantic
import pytest

import config
import llm
from pipeline import categorize
from schemas import Invoice, LineItem


@pytest.fixture
def fake_llm(monkeypatch):
    """Queue replies (a dict, or None for invalid JSON); returns (replies, messages of each call)."""
    monkeypatch.setattr(config, "PROMPTS_DIR", categorize.ROOT / "prompts", raising=False)
    monkeypatch.setattr(config, "TEXT_MODEL", "fake-model", raising=False)
    monkeypatch.setattr(config, "MAX_RETRIES", 2, raising=False)
    replies, calls = [], []

    def call(*, messages, schema, **kwargs):
        calls.append(messages)
        reply = replies.pop(0)
        if reply is None:
            return SimpleNamespace(parsed=None, error="invalid JSON", text="oops")
        return SimpleNamespace(parsed=schema.model_validate(reply), error=None, text="{}")

    monkeypatch.setattr(llm, "call", call, raising=False)
    return replies, calls


def invoice(*descriptions):
    return Invoice(doc_id="d1", vendor_name="Acme Cloud",
                   line_items=[LineItem(description=d, amount=Decimal("10")) for d in descriptions])


def test_model_assigns_and_memory_learns(fake_llm):
    replies, _ = fake_llm
    replies.append({"items": [{"index": 0, "category": "cloud"}, {"index": 1, "category": "software"}]})
    inv, memory = invoice("Compute hours", "IDE license"), {}
    categorize.categorize(inv, memory=memory)
    assert [i.category for i in inv.line_items] == ["cloud", "software"]
    assert memory == {"Acme Cloud": {"Compute hours": "cloud", "IDE license": "software"}}


def test_similar_description_reuses_vendor_category(fake_llm):
    _, calls = fake_llm
    inv = invoice("compute  HOURS")
    assert categorize.categorize(inv, memory={"Acme Cloud": {"Compute hours": "cloud"}}) == []
    assert inv.line_items[0].category == "cloud"
    assert not calls


def test_retries_then_falls_back_to_other(fake_llm):
    replies, calls = fake_llm
    replies += [None, {"items": [{"index": 0, "category": "travel"}]}, None]
    inv = invoice("Flight SFO-JFK", "Hotel")
    categorize.categorize(inv)
    assert len(calls) == 3
    assert [i.category for i in inv.line_items] == ["travel", "other"]


def test_reply_schema_rejects_unknown_category():
    schema = categorize.reply_schema(list(categorize.load_categories()))
    with pytest.raises(pydantic.ValidationError):
        schema.model_validate({"items": [{"index": 0, "category": "snacks"}]})
