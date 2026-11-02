"""Turn the date as printed into ISO 8601, deterministically.

The model copies the printed date (invoice_date_printed) and also gives its own ISO reading.
Python decides:
- an unambiguous printed date (year first, a month name, or a day above 12) is parsed directly;
- an ambiguous numeric date like 07/10/2020 uses config.AMBIGUOUS_DATE_ORDER ("auto": month
  first for USD documents, day first otherwise; or "MDY" / "DMY") and is marked uncertain;
- a format we can't parse keeps the model's reading and is marked uncertain.
"""
import re
from datetime import date
from typing import Optional

import config

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _make(y: int, m: int, d: int) -> Optional[str]:
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _order(currency: Optional[str]) -> str:
    setting = config.AMBIGUOUS_DATE_ORDER.upper()
    if setting in ("MDY", "DMY"):
        return setting
    return "MDY" if (currency or "").upper() == "USD" else "DMY"


def to_iso(printed: Optional[str], currency: Optional[str] = None) -> tuple[Optional[str], bool]:
    """Return (ISO date or None, ambiguous). None means the format wasn't recognised."""
    if not printed:
        return None, False
    s = printed.strip()

    m = re.fullmatch(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", s)            # 2020-07-10
    if m:
        return _make(int(m[1]), int(m[2]), int(m[3])), False

    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})", s)       # 07/10/2020
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b <= 12:
            return _make(y, b, a), False
        if b > 12 and a <= 12:
            return _make(y, a, b), False
        if a == b:
            return _make(y, a, b), False
        mdy = _order(currency) == "MDY"
        return _make(y, a, b) if mdy else _make(y, b, a), True

    words = re.findall(r"[A-Za-z]+|\d+", s)                                 # 10 Jul 2020, July 10, 2020
    month = next((MONTHS[w[:3].lower()] for w in words if w[:3].lower() in MONTHS), None)
    nums = [int(w) for w in words if w.isdigit()]
    years = [n for n in nums if n > 31]
    days = [n for n in nums if n <= 31]
    if month and len(years) == 1 and len(days) == 1:
        return _make(years[0], month, days[0]), False
    return None, False


def resolve(invoice, uncertain_fields: list[str]) -> None:
    """Set invoice.invoice_date from invoice_date_printed, in place; update uncertain_fields."""
    iso, ambiguous = to_iso(invoice.invoice_date_printed, invoice.currency)
    if iso:
        invoice.invoice_date = iso
        if ambiguous:
            if "invoice_date" not in uncertain_fields:
                uncertain_fields.append("invoice_date")
        elif "invoice_date" in uncertain_fields:
            uncertain_fields.remove("invoice_date")
    elif invoice.invoice_date_printed and "invoice_date" not in uncertain_fields:
        uncertain_fields.append("invoice_date")  # unrecognised format: keep the model's reading
