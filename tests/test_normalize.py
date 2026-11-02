"""pipeline/normalize.py currency rules. Pure functions, no model."""
import pytest

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
