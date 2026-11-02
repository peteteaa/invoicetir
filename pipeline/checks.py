"""check_invoice(invoice) -> list of specific, human-readable failures,
e.g. "line 3: 4 x 12.50 != 48.00".

Checks (money tolerance 0.01):
- every line: quantity x unit_price = amount (when a unit price is printed); amount present
- sum of line amounts = subtotal
- subtotal + adjustments + tax = total
  - tax_inclusive: sum of line amounts + adjustments = total (tax is already inside the prices)
  - no subtotal printed: sum of line amounts + adjustments + tax = total
- invoice_date is a real ISO 8601 date (YYYY-MM-DD); currency is an ISO 4217 code
- in whole-unit currencies (IDR, VND, KRW), no total or line amount is below the smallest real
  amount: that is a thousands separator read as a decimal point
- the fields in config.REQUIRED_FIELDS (default vendor_name, invoice_date, currency, total) are
  present; there is at least one line item

A missing tax counts as 0 in the sums: many receipts print none. A wrong total still fails.
The failures are written for two readers: the model, when repair.py re-prompts it, and a person
reviewing a flagged invoice. Pure functions: no I/O, no model calls. Failures named in MISSING_FIELD mean a value is absent
(not printed, or not read); repair.py doesn't re-prompt for those, since re-reading can't print it.
"""
import re
from datetime import date
from decimal import Decimal
from typing import Optional

import config
from schemas import ExtractionResult, Invoice

TOLERANCE = Decimal("0.01")
# A failure saying a value is absent, as opposed to wrong or not adding up.
MISSING_FIELD = re.compile(r"^(vendor_name|invoice_date|currency|total) missing$|^line \d+: amount missing$"
                           r"|^adjustment \d+ \(.*\): amount missing$")

# Active ISO 4217 codes.
CURRENCIES = set("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN
BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS
GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW
KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD
NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD
SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED
VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWG
""".split())


def money(value: Decimal) -> str:
    """Format an amount with exactly two decimals, e.g. 48 -> "48.00"."""
    return f"{value.quantize(TOLERANCE):f}"


def plain(value: Decimal) -> str:
    """Format a quantity without trailing zeros, e.g. 4.00 -> "4", 0.50 -> "0.5"."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def price(value: Decimal) -> str:
    """Format a unit price: two decimals, or more if printed with more (0.0045)."""
    return money(value) if value == value.quantize(TOLERANCE) else plain(value)


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


# Currencies whose prices are whole units, with the smallest amount anything can cost. A total
# or line amount below it is a misread separator: "63.000" (63,000 rupiah) read as 63.00. The
# other checks can't see that when every number on the receipt is misread the same way.
SMALLEST_AMOUNT = {"IDR": Decimal(100), "VND": Decimal(100), "KRW": Decimal(10)}


def _implausibly_small(invoice: Invoice) -> list[str]:
    floor = SMALLEST_AMOUNT.get(invoice.currency or "")
    if floor is None:
        return []
    hint = (f"below the smallest {invoice.currency} amount ({floor}); a dot or comma followed by "
            f"three digits is a thousands separator, e.g. 63.000 is 63000")
    found = []
    if invoice.total is not None and 0 < abs(invoice.total) < floor:
        found.append(f"total {money(invoice.total)} {invoice.currency} is {hint}")
    for n, item in enumerate(invoice.line_items, 1):
        if item.amount is not None and 0 < abs(item.amount) < floor:
            found.append(f"line {n}: amount {money(item.amount)} {invoice.currency} is {hint}")
    return found


def _valid_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value  # exactly YYYY-MM-DD
    except ValueError:
        return False


def check_invoice(invoice: Invoice, required: Optional[list[str]] = None) -> list[str]:
    failures = []

    # Required fields (config.REQUIRED_FIELDS); a value that is present must always be valid.
    required = config.REQUIRED_FIELDS if required is None else required
    if not (invoice.vendor_name or "").strip() and "vendor_name" in required:
        failures.append("vendor_name missing")
    if invoice.invoice_date is None:
        if "invoice_date" in required:
            failures.append("invoice_date missing")
    elif not _valid_date(invoice.invoice_date):
        failures.append(f"invoice_date {invoice.invoice_date!r} is not a valid YYYY-MM-DD date")
    if invoice.currency is None:
        if "currency" in required:
            failures.append("currency missing")
    elif invoice.currency not in CURRENCIES:
        failures.append(f"currency {invoice.currency!r} is not an ISO 4217 code")
    if invoice.total is None and "total" in required:
        failures.append("total missing")
    if not invoice.line_items:
        failures.append("no line items")

    failures += _implausibly_small(invoice)

    # Each line.
    for n, item in enumerate(invoice.line_items, 1):
        if item.amount is None:
            failures.append(f"line {n}: amount missing")
            continue
        if item.unit_price is not None:
            expected = item.quantity * item.unit_price
            if not _close(expected, item.amount):
                failures.append(f"line {n}: {plain(item.quantity)} x {price(item.unit_price)} "
                                f"!= {money(item.amount)}")
    for n, adj in enumerate(invoice.adjustments, 1):
        if adj.amount is None:
            failures.append(f"adjustment {n} ({adj.label or 'no label'}): amount missing")

    # Sums. Only when every amount they depend on is present, so one missing value
    # produces one failure, not a cascade.
    amounts = [item.amount for item in invoice.line_items]
    adjustments = [a.amount for a in invoice.adjustments]
    if None in amounts or None in adjustments or not amounts:
        return failures
    lines_sum = sum(amounts, Decimal(0))
    adj_sum = sum(adjustments, Decimal(0))
    tax = invoice.tax or Decimal(0)

    if invoice.subtotal is not None and not _close(lines_sum, invoice.subtotal):
        failures.append(f"sum of line amounts {money(lines_sum)} != subtotal {money(invoice.subtotal)}")

    if invoice.total is not None:
        failures.extend(_total_check(invoice, lines_sum, adj_sum, tax))
    return failures


def _total_check(invoice: Invoice, lines_sum: Decimal, adj_sum: Decimal, tax: Decimal) -> list[str]:
    parts = []  # (label, value) terms that should add up to the total
    if invoice.tax_inclusive:
        parts.append(("sum of line amounts", lines_sum))
    elif invoice.subtotal is not None:
        parts.append(("subtotal", invoice.subtotal))
    else:
        parts.append(("sum of line amounts", lines_sum))
    if invoice.adjustments:
        parts.append(("adjustments", adj_sum))
    if not invoice.tax_inclusive and invoice.tax is not None:
        parts.append(("tax", tax))

    expected = sum((value for _, value in parts), Decimal(0))
    if _close(expected, invoice.total):
        return []
    terms = " + ".join(f"{label} {money(value)}" for label, value in parts)
    note = " (tax included in prices)" if invoice.tax_inclusive else ""
    return [f"{terms} = {money(expected)} != total {money(invoice.total)}{note}"]


def check_result(result: Optional[ExtractionResult]) -> list[str]:
    """Failures for a whole extraction, including an invalid or unreadable one."""
    if result is None:
        return ["no valid extraction"]
    if result.unreadable:
        return ["model reported the document as unreadable"]
    if result.invoice is None:
        return ["no invoice in the extraction"]
    return check_invoice(result.invoice)
