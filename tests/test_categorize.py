"""pipeline/categorize.py. No mocks: the memory and schema tests never reach the model; the
model tests call Fireworks and skip without FIREWORKS_API_KEY."""
from decimal import Decimal

import pydantic
import pytest

import config
from pipeline import categorize
from schemas import Invoice, LineItem

live = pytest.mark.skipif(not config.FIREWORKS_API_KEY, reason="needs FIREWORKS_API_KEY")


def invoice(vendor, *descriptions):
    return Invoice(doc_id="d1", vendor_name=vendor,
                   line_items=[LineItem(description=d, amount=Decimal("10")) for d in descriptions])


def test_similar_description_reuses_vendor_category_without_the_model():
    inv = invoice("Acme Cloud", "compute  HOURS")
    assert categorize.categorize(inv, memory={"Acme Cloud": {"Compute hours": "cloud"}}) == []
    assert inv.line_items[0].category == "cloud"


def test_reply_schema_rejects_unknown_category():
    schema = categorize.reply_schema(list(categorize.load_categories()))
    with pytest.raises(pydantic.ValidationError):
        schema.model_validate({"items": [{"index": 0, "category": "snacks"}]})


@live
def test_model_assigns_clear_categories_and_memory_learns():
    inv, memory = invoice("Acme Cloud", "AWS EC2 compute hours", "JetBrains IDE annual license",
                          "Flight SFO-JFK economy"), {}
    calls = categorize.categorize(inv, memory=memory)
    assert len(calls) == 1
    assert [i.category for i in inv.line_items] == ["cloud", "software", "travel"]
    assert memory == {"Acme Cloud": {"AWS EC2 compute hours": "cloud",
                                     "JetBrains IDE annual license": "software",
                                     "Flight SFO-JFK economy": "travel"}}
    # the next invoice from the same vendor reuses the answers without a call
    again = invoice("Acme Cloud", "aws ec2 compute HOURS")
    assert categorize.categorize(again, memory=memory) == []
    assert again.line_items[0].category == "cloud"
