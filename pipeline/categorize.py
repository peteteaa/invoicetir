"""Assigns each line item a category from categories.yaml (prompts/categorize.md).

Reuses a vendor's prior category for similar descriptions to stay consistent. The memory maps
vendor -> {description: category} and is kept in data/state/vendor_categories.json. Only items
with no similar prior description go to the model, in one call per invoice.
"""
import json
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import create_model
from rapidfuzz import fuzz, process, utils

import config
import llm
from schemas import Invoice

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES_PATH = ROOT / "categories.yaml"
MEMORY_PATH = ROOT / "data" / "state" / "vendor_categories.json"
SIMILAR = 90  # rapidfuzz score (0-100) at which a prior description counts as the same item


def load_categories() -> dict[str, str]:
    return yaml.safe_load(CATEGORIES_PATH.read_text())


def load_memory(path: Path = MEMORY_PATH) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_memory(memory: dict, path: Path = MEMORY_PATH) -> None:
    path.write_text(json.dumps(memory, indent=2, sort_keys=True))


def reply_schema(names: list[str]):
    """The reply model, with category limited to the known names."""
    item = create_model("CategorizedItem", index=(int, ...), category=(Literal[tuple(names)], ...))
    return create_model("Categorization", items=(list[item], ...))


def prompt(categories: dict[str, str]) -> str:
    listing = "\n".join(f"- {name}: {desc}" for name, desc in categories.items())
    return (config.PROMPTS_DIR / "categorize.md").read_text().replace("{categories}", listing)


def _prior(description: Optional[str], known: dict) -> Optional[str]:
    if not description or not known:
        return None
    match = process.extractOne(description, known.keys(), scorer=fuzz.token_sort_ratio,
                               processor=utils.default_process, score_cutoff=SIMILAR)
    return known[match[0]] if match else None


def categorize(invoice: Invoice, vendor: Optional[str] = None, memory: Optional[dict] = None,
               model: Optional[str] = None) -> list:
    """Set the category of every line item, in place, and add the model's answers to memory.

    vendor is the normalized name from clean.py (defaults to the printed name). Returns the
    llm.CallResult of every model call, for cost tracking.
    """
    categories = load_categories()
    vendor = vendor or invoice.vendor_name
    memory = {} if memory is None else memory
    known = memory.setdefault(vendor, {}) if vendor else {}  # no memory without a vendor

    todo = []
    for i, item in enumerate(invoice.line_items):
        item.category = _prior(item.description, known)
        if item.category is None:
            todo.append(i)
    if not todo:
        return []

    listing = "\n".join(f"{i}: {invoice.line_items[i].description or '(no description)'}" for i in todo)
    messages = [{"role": "system", "content": prompt(categories)},
                {"role": "user", "content": f"Vendor: {vendor or '(unknown)'}\nLine items:\n{listing}"}]
    schema = reply_schema(list(categories))
    calls, got = [], {}
    for _ in range(1 + config.MAX_RETRIES):  # invalid JSON uses the same budget
        c = llm.call(doc_id=invoice.doc_id, stage="categorize", model=model or config.TEXT_MODEL,
                     messages=messages, schema=schema)
        calls.append(c)
        if c.parsed is not None:
            got.update({it.index: it.category for it in c.parsed.items if it.index in todo})
        missing = [i for i in todo if i not in got]
        if not missing:
            break
        problem = c.error if c.parsed is None else f"no category for line items {missing}"
        messages += [{"role": "assistant", "content": c.text or ""},
                     {"role": "user", "content": f"Your reply was not usable: {problem}. Reply again with JSON only."}]

    for i in todo:
        item = invoice.line_items[i]
        item.category = got.get(i, "other")  # "other" only if still missing after the retries
        if item.description and i in got:
            known[item.description] = item.category
    return calls
