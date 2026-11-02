"""check_invoice(invoice) -> list of specific, human-readable failures,
e.g. "line 3: 4 x 12.50 != 48.00".

Checks: quantity x unit_price = amount, sum of amounts = subtotal, subtotal + tax = total
(tolerance 0.01), valid date, valid currency code. Pure functions: no I/O, no model calls.
"""
