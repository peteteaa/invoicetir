"""Turn printed dates and currencies into ISO values, deterministically.

The model copies what is printed (invoice_date_printed, currency_printed) and also gives its
own ISO reading. Python decides.

Currency: an ISO code or an unambiguous symbol (EUR, €, £, ₩, Rp...) maps directly. A shared
symbol like `$` or `¥` keeps the model's code if it is one the symbol can mean (else the
symbol's most common code) and is marked uncertain. Anything else (nothing printed, or a local
abbreviation such as "Ksh" or "грн") keeps the model's code and is marked uncertain. With no
code at all, config.DEFAULT_CURRENCY (if set) is used, also marked uncertain.

Dates:
- an unambiguous printed date (year first, a month name, or a day above 12) is parsed directly;
- an ambiguous numeric date like 07/10/2020 uses config.AMBIGUOUS_DATE_ORDER ("auto": month
  first for USD documents, day first otherwise; or "MDY" / "DMY") and is marked uncertain;
- a format we can't parse keeps the model's reading and is marked uncertain.
"""
import re
from datetime import date
from typing import Optional

import config
from pipeline.checks import CURRENCIES

SYMBOLS = {  # unambiguous symbols and prefixes
    "€": "EUR", "£": "GBP", "₩": "KRW", "₹": "INR", "₱": "PHP", "₫": "VND", "฿": "THB",
    "₺": "TRY", "₪": "ILS", "₦": "NGN", "₴": "UAH", "₽": "RUB", "zł": "PLN", "rp": "IDR",
    "us$": "USD", "c$": "CAD", "ca$": "CAD", "a$": "AUD", "au$": "AUD", "nz$": "NZD",
    "hk$": "HKD", "s$": "SGD", "r$": "BRL", "mx$": "MXN",
}
SHARED = {  # symbol -> (codes it can mean, most common)
    "$": ({"USD", "CAD", "AUD", "NZD", "HKD", "SGD", "MXN", "TWD"}, "USD"),
    "¥": ({"JPY", "CNY"}, "JPY"),
    "kr": ({"SEK", "NOK", "DKK", "ISK"}, "SEK"),
}


def currency(printed, model_code):
    """Return (ISO code or None, uncertain)."""
    s = (printed or "").strip()
    code = (model_code or "").strip().upper() or None
    if s.upper() in CURRENCIES:  # a printed code; "Ksh" or "Lei" are words, not codes
        return s.upper(), False
    if s.lower() in SYMBOLS:
        return SYMBOLS[s.lower()], False
    if s.lower() in SHARED:
        options, common = SHARED[s.lower()]
        return (code if code in options else common), True
    if not s and code is None and config.DEFAULT_CURRENCY:
        return config.DEFAULT_CURRENCY, True  # nothing printed or read: the configured default
    return code, True  # nothing printed, or a symbol we don't know: keep the model's reading


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


def _mark(uncertain_fields: list[str], name: str, uncertain: bool) -> None:
    if uncertain and name not in uncertain_fields:
        uncertain_fields.append(name)
    elif not uncertain and name in uncertain_fields:
        uncertain_fields.remove(name)


def resolve(invoice, uncertain_fields: list[str]) -> None:
    """Set invoice.currency and invoice.invoice_date from what is printed, in place."""
    invoice.currency, unsure = currency(invoice.currency_printed, invoice.currency)
    if invoice.currency is None and config.DEFAULT_CURRENCY:   # nothing printed, nothing read
        invoice.currency, unsure = config.DEFAULT_CURRENCY, True
    _mark(uncertain_fields, "currency", unsure)

    iso, ambiguous = to_iso(invoice.invoice_date_printed, invoice.currency)
    if iso:
        invoice.invoice_date = iso
        _mark(uncertain_fields, "invoice_date", ambiguous)
    elif invoice.invoice_date_printed:
        _mark(uncertain_fields, "invoice_date", True)  # unrecognised format: keep the model's reading
