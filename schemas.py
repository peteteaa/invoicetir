"""Pydantic models. All money is Decimal quantized to cents, never float.

LineItem          description, quantity (default 1), unit_price, amount, category (filled later),
                  page (the page the line appears on, for traceability)
Adjustment        label, amount: invoice-level discounts (negative), service charges, shipping, tips
Invoice           doc_id, vendor_name (exactly as printed), invoice_number (optional),
                  invoice_date_printed (as printed), invoice_date (ISO 8601, set by
                  pipeline/normalize.py), currency_printed (as printed), currency (ISO 4217,
                  set by pipeline/normalize.py), line_items, adjustments, subtotal,
                  tax, tax_inclusive (tax printed as already included in the total), total,
                  source_pages
ExtractionResult  invoice (or null), uncertain_fields, unreadable
Label             ground truth: Invoice plus optional is_duplicate_of (doc_id), expected_flag
                  (None = not labelled) and corrected_fields (from label_app.py)
ProcessedDoc      one document after the pipeline: status (verified / flagged / duplicate), the
                  invoice, reasons, normalized vendor, and the cost and latency of its model calls.
                  pipeline/run.py builds these; clean.py, aggregate.py and output.py read them.

Model-output fields may be null: the prompt says to return null for anything not printed, and
checks.py decides whether a missing value is a failure. Dates and currency codes are plain
strings for the same reason; checks.py validates them.
"""
from decimal import Decimal
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, BeforeValidator

CENT = Decimal("0.01")


def _to_cents(v: Optional[Decimal]) -> Optional[Decimal]:
    """Quantize to cents. A value with sub-cent digits is rejected, not rounded: printed money
    has at most two decimals, so 12.345 is a misread, and rejecting it makes it a retry."""
    if v is None:
        return None
    if not v.is_finite():
        raise ValueError("must be a finite number")
    cents = v.quantize(CENT)
    if cents != v:
        raise ValueError(f"{v} has more than 2 decimal places")
    return cents


Money = Annotated[Decimal, AfterValidator(_to_cents)]
# Not quantized: quantities and unit prices can have more than two decimals (0.5 kg at 3.125).
Quantity = Annotated[Decimal, BeforeValidator(lambda v: Decimal(1) if v is None else v)]


class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Quantity = Decimal(1)
    unit_price: Optional[Decimal] = None
    amount: Optional[Money] = None
    category: Optional[str] = None    # filled by categorize.py
    page: Optional[int] = None        # 1-based


class Adjustment(BaseModel):
    label: Optional[str] = None
    amount: Optional[Money] = None    # discounts are negative


class Invoice(BaseModel):
    doc_id: Optional[str] = None      # set by extract.py, not the model
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date_printed: Optional[str] = None
    invoice_date: Optional[str] = None
    currency_printed: Optional[str] = None  # symbol or code as printed, e.g. "$"
    currency: Optional[str] = None
    line_items: list[LineItem] = []
    adjustments: list[Adjustment] = []
    subtotal: Optional[Money] = None
    tax: Optional[Money] = None
    tax_inclusive: bool = False
    total: Optional[Money] = None
    source_pages: list[int] = []      # set by extract.py, not the model


class ExtractionResult(BaseModel):
    invoice: Optional[Invoice] = None
    uncertain_fields: list[str] = []
    unreadable: bool = False


class Label(Invoice):
    is_duplicate_of: Optional[str] = None  # doc_id of the original
    # True: the document's own printed math is wrong. None: not labelled (excluded from flag
    # precision/recall), which is different from False: checked, and the printed math is right.
    expected_flag: Optional[bool] = None
    corrected_fields: list[str] = []       # fields the human changed in label_app.py


class ProcessedDoc(BaseModel):
    """One document after the pipeline. Only status "verified" counts toward the spend totals."""
    doc_id: str
    source_path: str
    status: Literal["verified", "flagged", "duplicate"]
    invoice: Optional[Invoice] = None       # None only when nothing valid was extracted
    uncertain_fields: list[str] = []
    reasons: list[str] = []                 # why flagged, in words, e.g. "line 2: 4 x 3288.00 != 13125.00"
    vendor: Optional[str] = None            # normalized by clean.py; invoice.vendor_name stays as printed
    duplicate_of: Optional[str] = None      # doc_id of the original, set by clean.py
    model: Optional[str] = None             # the model whose extraction this is
    escalated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: Optional[Decimal] = None      # None when a model's prices are not configured
