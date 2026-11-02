"""pipeline/normalize.py currency rules. Pure functions, no model."""
import pytest

import config
from pipeline import normalize


@pytest.mark.parametrize("printed, model_code, expected", [
    ("USD", None, ("USD", False)),
    ("eur", "USD", ("EUR", False)),            # a printed code wins over the model's reading
    ("€", None, ("EUR", False)),
    ("$", "CAD", ("CAD", True)),
    ("$", "EUR", ("USD", True)),               # not a code "$" can mean: its most common one
    ("Ksh", "KES", ("KES", True)),             # local abbreviations are not ISO codes
    ("Lei", "RON", ("RON", True)),
    ("грн", "UAH", ("UAH", True)),
    (None, "JPY", ("JPY", True)),
])
def test_currency(printed, model_code, expected):
    assert normalize.currency(printed, model_code) == expected


@pytest.mark.parametrize("printed, model_code, expected", [
    (None, None, ("IDR", True)),               # nothing printed or read: the default, uncertain
    ("", None, ("IDR", True)),
    (None, "SGD", ("SGD", True)),              # the model's reading still wins over the default
    ("$", None, ("USD", True)),                # a printed symbol still wins over the default
])
def test_default_currency(monkeypatch, printed, model_code, expected):
    monkeypatch.setattr(config, "DEFAULT_CURRENCY", "IDR")
    assert normalize.currency(printed, model_code) == expected


def test_no_default_currency_leaves_it_missing(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_CURRENCY", "")
    assert normalize.currency(None, None) == (None, True)
