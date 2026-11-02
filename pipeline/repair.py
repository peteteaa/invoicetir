"""Repair loop around extract.py and checks.py.

1. Extract with the small model (TEXT_MODEL or VISION_MODEL). If the reply is invalid or fails
   the checks, re-prompt the same model with the exact failures (prompts/repair.md), up to
   MAX_RETRIES; invalid JSON counts as a retry.
2. Escalate once to ESCALATION_MODEL with every failure so far (prompts/escalate.md) when the
   small model still fails, calls the document unreadable, or (ESCALATE_ON_UNCERTAIN) is
   unsure about a material field.
   A value that is simply absent (vendor_name missing, a line with no amount) is never retried
   or escalated: re-reading can't print it. The document is flagged "not printed" straight away.
3. Flag for human review, with the reasons, when the escalation model still fails the checks,
   calls the document unreadable, or is itself unsure about a material field. Never guess.

Material fields are the ones that change a spend number: total, subtotal, tax, adjustments, and
line-item unit price and amount. Doubts about text (an item name, the vendor) don't send a
document to review unless config.REVIEW_TEXT_DOUBTS is on: on 100 real CORD receipts they caused
16 of the 23 false alarms and could not change an amount. The math checks still stop every wrong
total, and clean.py reconciles vendor names.
Uncertainty that comes from normalize.py's fixed rules ("$" could be several currencies, an
ambiguous date) is not material on its own.

Not material either:
- a field whose value is null. Nothing was printed, so there is nothing to misread, and
  checks.py already decides whether a missing value is a failure (a missing subtotal or tax is
  fine, a missing total is not).
- a line's quantity. It becomes 1 when none is printed, so a missing one can't be told apart
  from a printed one, and it changes no spend number: when a unit price is printed,
  quantity x unit_price = amount is checked, and otherwise only the amount counts.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

import config
from pipeline.checks import MISSING_FIELD, check_result
from pipeline.extract import Attempt, default_model, extract
from pipeline.ingest import Document
from schemas import ExtractionResult

MATERIAL = {"total", "subtotal", "tax"}
MATERIAL_LINE_FIELDS = {"unit_price", "amount"}
TEXT_FIELDS, TEXT_LINE_FIELDS = {"vendor_name"}, {"description"}   # only with REVIEW_TEXT_DOUBTS
_UNKNOWN = object()   # a field name that doesn't resolve to a value of the invoice


@dataclass
class Outcome:
    status: str                              # "verified" or "flagged"
    result: Optional[ExtractionResult]       # the extraction the status refers to
    model: str                               # the model that produced it
    reasons: list[str] = field(default_factory=list)   # why it was flagged (or escalated)
    attempts: list[Attempt] = field(default_factory=list)
    escalated: bool = False

    @property
    def calls(self): return [c for a in self.attempts for c in a.calls]
    @property
    def input_tokens(self): return sum(a.input_tokens for a in self.attempts)
    @property
    def output_tokens(self): return sum(a.output_tokens for a in self.attempts)
    @property
    def latency_s(self): return sum(a.latency_s for a in self.attempts)
    @property
    def cost_usd(self):
        costs = [a.cost_usd for a in self.attempts]
        return None if None in costs else sum(costs)


def material_uncertain(result: Optional[ExtractionResult]) -> list[str]:
    """The uncertain fields that matter for spend, e.g. ["total", "line_items[2].amount"]."""
    if result is None:
        return []
    found = []
    for name in result.uncertain_fields:
        path = re.sub(r"\[\d+\]", "", name).split(".")
        top, line = MATERIAL, MATERIAL_LINE_FIELDS
        if config.REVIEW_TEXT_DOUBTS:
            top, line = top | TEXT_FIELDS, line | TEXT_LINE_FIELDS
        if not (path[0] in top or path[0] == "adjustments"
                or (path[0] == "line_items" and (len(path) == 1 or path[1] in line))):
            continue
        if _value(result.invoice, name) is None:   # not printed: checks.py's call, not doubt
            continue
        found.append(name)
    return found


def _value(invoice, name: str):
    """The value a field name like "line_items[2].amount" points to, or _UNKNOWN if it points to
    nothing (no invoice, an index out of range, an unknown field)."""
    value = invoice
    for part in re.findall(r"[^.\[\]]+", name):
        if value is None:
            return _UNKNOWN
        if part.isdigit():
            if not isinstance(value, list) or int(part) >= len(value):
                return _UNKNOWN
            value = value[int(part)]
        elif hasattr(value, part):
            value = getattr(value, part)
        else:
            return _UNKNOWN
    return value


def failures(attempt: Attempt) -> list[str]:
    """Why an attempt is not acceptable: invalid JSON, or check failures."""
    if attempt.result is None:
        return [attempt.error or "no valid extraction"]
    return check_result(attempt.result)


def _split(fails: list[str]) -> tuple[list[str], list[str]]:
    """(absent values, everything else). Re-reading can fix a misread or a sum that doesn't add
    up, but it can't make a vendor or date appear that the document doesn't print."""
    missing = [f for f in fails if MISSING_FIELD.match(f)]
    return missing, [f for f in fails if f not in missing]


def _not_printed(missing: list[str], who: str = "") -> list[str]:
    fields = ", ".join(f.replace(" missing", "") for f in missing)
    return [f"{who}not printed (or not read): {fields}"]


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items)


def _prompt(name: str, failure_list: list[str]) -> str:
    return (config.PROMPTS_DIR / name).read_text().replace("{failures}", _bullets(failure_list))


def run(doc: Document, model: Optional[str] = None, use_checks: bool = True,
        escalate: bool = True) -> Outcome:
    """Extract one document through the repair loop. The flags map to the eval configs:
    use_checks=False -> one attempt, no checks; escalate=False -> retries but no escalation."""
    model = model or default_model(doc)
    attempts: list[Attempt] = []

    first = extract(doc, model=model, stage="extract")
    attempts.append(first)
    if not use_checks:
        # No checks, but an answer without an invoice (invalid, or "unreadable") is still not one
        # to count: verified documents must have an invoice for clean, categorize and aggregate.
        usable = first.result is not None and first.result.invoice is not None
        return Outcome("verified" if usable else "flagged", first.result, model,
                       [] if usable else failures(first), attempts)

    seen: list[str] = []   # every failure, in order, for the escalation prompt and the reasons
    last = first
    why_escalate: list[str] = []
    for retry in range(config.MAX_RETRIES + 1):
        fails = failures(last)
        if not fails:
            unsure = material_uncertain(last.result) if config.ESCALATE_ON_UNCERTAIN else []
            if not unsure:
                return Outcome("verified", last.result, model, [], attempts)
            why_escalate = [f"{model.split('/')[-1]} unsure about: {', '.join(unsure)}"]
            break
        missing, fixable = _split(fails)
        seen += [f for f in fixable if f not in seen]
        if last.result is not None and last.result.unreadable:
            why_escalate = ["small model reported the document as unreadable"]
            break
        if not fixable:   # only absent values: no retry or escalation can print them
            return Outcome("flagged", last.result, model, _not_printed(missing), attempts)
        if retry == config.MAX_RETRIES:
            why_escalate = fixable
            break
        # Only the fixable failures: listing "vendor_name missing" invites an invented vendor.
        followup = [{"role": "assistant", "content": last.raw or "(no answer)"},
                    {"role": "user", "content": _prompt("repair.md", fixable)}]
        last = extract(doc, model=model, stage=f"repair{retry + 1}", followup=followup)
        attempts.append(last)

    if not escalate:
        return Outcome("flagged", last.result, model, why_escalate, attempts)

    big = config.ESCALATION_MODEL
    if not big:
        config.require("ESCALATION_MODEL")
    note = seen or why_escalate
    final = extract(doc, model=big, stage="escalate",
                    followup=[{"role": "user", "content": _prompt("escalate.md", note)}])
    attempts.append(final)

    name = big.split("/")[-1]
    missing, fixable = _split(failures(final))
    if fixable:
        return Outcome("flagged", final.result, big, [f"{name}: {f}" for f in fixable + missing], attempts, True)
    if missing:
        return Outcome("flagged", final.result, big, _not_printed(missing, f"{name}: "), attempts, True)
    unsure = material_uncertain(final.result)
    if unsure:
        return Outcome("flagged", final.result, big, [f"{name} unsure about: {', '.join(unsure)}"],
                       attempts, True)
    return Outcome("verified", final.result, big, [], attempts, True)
