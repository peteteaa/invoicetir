"""Spend by vendor, category and month (pandas, Decimal).

Asserts that all totals equal the exact sum of verified invoices.

Only documents with status "verified" count; flagged and duplicate ones are only counted. Each
invoice becomes spend rows that add up exactly to its printed total:
- one row per line item, under its category;
- "tax" (unless tax_inclusive: then the tax is already inside the line prices);
- one "adjustments" row per discount, service charge, shipping or tip;
- "rounding" for what is left, if anything. checks.py allows each sum to be a cent off, so
  lines + adjustments + tax can differ from the printed total by a cent or two.
Amounts stay Decimal (object columns) and are never converted between currencies: every table
is per currency.
"""
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from schemas import ProcessedDoc

COLUMNS = ["doc_id", "page", "vendor", "month", "currency", "category", "description", "amount"]


@dataclass
class Spend:
    rows: pd.DataFrame          # COLUMNS: every line item, tax, adjustment and rounding row
    by_category: pd.DataFrame   # currency, category, amount
    by_vendor: pd.DataFrame     # currency, vendor, amount
    by_month: pd.DataFrame      # currency, month (YYYY-MM), amount
    summary: pd.DataFrame       # currency, invoices, amount, tax, adjustments
    flagged: int                # documents left out of the totals
    duplicates: int


def _sum(values) -> Decimal:
    return sum(values, Decimal(0))


def spend_rows(doc: ProcessedDoc) -> list[dict]:
    """The rows of one verified document; they add up to invoice.total exactly."""
    inv = doc.invoice
    base = {"doc_id": doc.doc_id, "vendor": doc.vendor or inv.vendor_name,
            "month": inv.invoice_date[:7], "currency": inv.currency}
    rows = [{**base, "page": item.page, "category": item.category or "uncategorized",
             "description": item.description, "amount": item.amount} for item in inv.line_items]
    if inv.tax and not inv.tax_inclusive:
        rows.append({**base, "page": None, "category": "tax", "description": "Tax", "amount": inv.tax})
    rows += [{**base, "page": None, "category": "adjustments", "description": adj.label,
              "amount": adj.amount} for adj in inv.adjustments]
    rest = inv.total - _sum(r["amount"] for r in rows)
    if rest:
        rows.append({**base, "page": None, "category": "rounding",
                     "description": "Printed total minus the sum of its parts", "amount": rest})
    return rows


def _by(rows: pd.DataFrame, key: str, by_amount: bool = True) -> pd.DataFrame:
    table = rows.groupby(["currency", key], as_index=False)["amount"].agg(_sum)
    if by_amount:
        return table.sort_values(["currency", "amount"], ascending=[True, False], ignore_index=True)
    return table.sort_values(["currency", key], ignore_index=True)


def aggregate(docs: list[ProcessedDoc]) -> Spend:
    verified = [d for d in docs if d.status == "verified"]
    rows = pd.DataFrame([r for d in verified for r in spend_rows(d)], columns=COLUMNS)
    rows["page"] = rows["page"].astype("Int64")  # 1, not 1.0; tax and adjustment rows have none

    totals = {}  # currency -> exact sum of the verified invoices' printed totals
    for d in verified:
        totals[d.invoice.currency] = totals.get(d.invoice.currency, Decimal(0)) + d.invoice.total

    by_category, by_vendor, by_month = _by(rows, "category"), _by(rows, "vendor"), _by(rows, "month", False)
    for name, table in [("rows", rows), ("by_category", by_category), ("by_vendor", by_vendor),
                        ("by_month", by_month)]:
        got = {c: _sum(g) for c, g in table.groupby("currency")["amount"]}
        assert got == totals, f"{name} totals {got} != sum of verified invoices {totals}"

    summary = pd.DataFrame(
        [{"currency": c, "invoices": sum(d.invoice.currency == c for d in verified), "amount": t,
          "tax": _sum(rows.amount[(rows.currency == c) & (rows.category == "tax")]),
          "adjustments": _sum(rows.amount[(rows.currency == c) & (rows.category == "adjustments")])}
         for c, t in sorted(totals.items())],
        columns=["currency", "invoices", "amount", "tax", "adjustments"])
    return Spend(rows, by_category, by_vendor, by_month, summary,
                 flagged=sum(d.status == "flagged" for d in docs),
                 duplicates=sum(d.status == "duplicate" for d in docs))
