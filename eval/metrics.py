"""Metrics: field accuracy, line-item accuracy, dollar error per category, duplicate recall and
flag precision/recall (only where labels include them), cost per document, p50/p95 latency.

Pure functions over (prediction, truth) pairs; run_eval.py does the model calls.

- Fields are compared exactly: text with whitespace collapsed, money as Decimal, dates as ISO
  strings. A field whose truth is None (not labelled, e.g. CORD has no vendor) is skipped.
- Line items are paired before comparing (pair_items), so one skipped or extra row costs one
  item, not every row after it. A pair is correct when description, quantity, unit price and
  amount all match. Accuracy = correct / max(truth rows, predicted rows), summed over documents.
- Spend per category uses aggregate.spend_rows, the same bucketing as the real output (line
  items by category, tax, adjustments, rounding). Dollar error = sum over categories of
  |predicted - true|, per currency; spend accuracy = 1 - dollar error / true spend.
"""
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz

from pipeline.aggregate import spend_rows
from schemas import Invoice, LineItem, ProcessedDoc

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "currency", "subtotal", "tax", "total"]
UNMATCHED = "unmatched"  # category for a predicted line item with no true counterpart


def _norm(value):
    return " ".join(value.split()) if isinstance(value, str) else value


def field_results(pred: Optional[Invoice], truth: Invoice) -> dict[str, bool]:
    """{field: correct} for every field the truth has a value for."""
    return {f: pred is not None and _norm(getattr(pred, f)) == _norm(getattr(truth, f))
            for f in FIELDS if getattr(truth, f) is not None}


def item_correct(pred: LineItem, truth: LineItem) -> bool:
    return (_norm(pred.description) == _norm(truth.description) and pred.quantity == truth.quantity
            and pred.unit_price == truth.unit_price and pred.amount == truth.amount)


def pair_items(pred: list[LineItem], truth: list[LineItem]) -> list[tuple[int, int]]:
    """(pred index, truth index) pairs. Greedy: identical items first, then same amount, then
    the closest description (fuzz.ratio >= 60). Unpaired items on either side are errors."""
    candidates = []
    for i, p in enumerate(pred):
        for j, t in enumerate(truth):
            similar = fuzz.ratio(_norm(p.description or ""), _norm(t.description or ""))
            same_amount = p.amount is not None and p.amount == t.amount
            if item_correct(p, t) or same_amount or similar >= 60:
                candidates.append((item_correct(p, t), same_amount, similar, -abs(i - j), i, j))
    pairs, used_p, used_t = [], set(), set()
    for *_, i, j in sorted(candidates, reverse=True):
        if i not in used_p and j not in used_t:
            pairs.append((i, j))
            used_p.add(i)
            used_t.add(j)
    return sorted(pairs)


def line_item_counts(pred: Optional[Invoice], truth: Invoice) -> tuple[int, int]:
    """(correct items, max(truth rows, predicted rows))."""
    pred_items = pred.line_items if pred else []
    pairs = pair_items(pred_items, truth.line_items)
    correct = sum(item_correct(pred_items[i], truth.line_items[j]) for i, j in pairs)
    return correct, max(len(truth.line_items), len(pred_items))


def with_truth_categories(pred: Invoice, truth: Invoice) -> Invoice:
    """A copy of pred whose line items carry the category of the true item they pair with, so
    dollar error measures extraction, not categorizer disagreement."""
    pred = pred.model_copy(deep=True)
    pairs = dict(pair_items(pred.line_items, truth.line_items))
    for i, item in enumerate(pred.line_items):
        item.category = truth.line_items[pairs[i]].category if i in pairs else UNMATCHED
    return pred


def category_spend(docs: list[ProcessedDoc]) -> dict[tuple[str, str], Decimal]:
    """{(currency, category): amount} over the verified documents."""
    spend = defaultdict(Decimal)
    for d in docs:
        inv = d.invoice
        if d.status != "verified" or inv is None:
            continue
        # Configs without checks can "verify" an invoice missing what spend needs; its spend is
        # then simply missing (an error), never invented.
        if inv.total is None or inv.currency is None or any(i.amount is None for i in inv.line_items) \
                or any(a.amount is None for a in inv.adjustments):
            continue
        if inv.invoice_date is None:  # month isn't needed here
            d = d.model_copy(update={"invoice": inv.model_copy(update={"invoice_date": "unknown"})})
        for row in spend_rows(d):
                spend[row["currency"], row["category"]] += row["amount"]
    return dict(spend)


def dollar_error(pred: dict, truth: dict) -> dict[tuple[str, str], Decimal]:
    """{(currency, category): |predicted - true|} over every key on either side."""
    return {k: abs(pred.get(k, Decimal(0)) - truth.get(k, Decimal(0))) for k in set(pred) | set(truth)}


def spend_accuracy(pred: dict, truth: dict) -> Optional[float]:
    """1 - total dollar error / total true spend (all currencies together; one currency here)."""
    total = sum(truth.values(), Decimal(0))
    if not total:
        return None
    return float(1 - sum(dollar_error(pred, truth).values(), Decimal(0)) / total)


def total_accuracy(pred: dict, truth: dict) -> Optional[float]:
    """1 - |predicted total - true total| / true total, per currency then summed. Ignores
    categories: it only asks whether the money adds up to what was printed."""
    total = sum(truth.values(), Decimal(0))
    if not total:
        return None
    currencies = {c for c, _ in pred} | {c for c, _ in truth}
    error = sum((abs(sum((v for (c, _), v in pred.items() if c == cur), Decimal(0))
                     - sum((v for (c, _), v in truth.items() if c == cur), Decimal(0))) for cur in currencies),
                Decimal(0))
    return float(1 - error / total)


def precision_recall(predicted: list[bool], actual: list[Optional[bool]]) -> dict:
    """Over the pairs where actual is known. None where undefined (no positives)."""
    pairs = [(p, a) for p, a in zip(predicted, actual) if a is not None]
    tp = sum(p and a for p, a in pairs)
    fp = sum(p and not a for p, a in pairs)
    fn = sum(a and not p for p, a in pairs)
    return {"labelled": len(pairs), "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "false_positive_rate": fp / sum(not a for _, a in pairs) if any(not a for _, a in pairs) else None}


def percentile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile, q in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil
    return ordered[int(rank) - 1]
