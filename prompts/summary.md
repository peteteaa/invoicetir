You are given a batch of invoices and receipts. Report the total spend across all of them,
broken down by category and currency. Return a single JSON object and nothing else.

Categories (put every line item in exactly one):
{categories}

Also use these buckets, which are not line-item categories:
- tax: printed tax, unless the document says tax is already included in the prices
- adjustments: invoice-level discounts (negative), service charges, shipping, tips

Rules:
- Each document counts once. Read every line item's printed amount (the pre-tax / net line
  total when both net and gross are printed) and add it to its category.
- Amounts are plain numbers with two decimals, no currency symbols or thousands separators.
- Never convert between currencies: report each currency separately (ISO 4217 code; `$` on a
  US-style document is USD).

Output shape:
```json
{"spend": [{"currency": "USD", "category": "office", "amount": 1234.56}]}
```
