"""Pydantic models. All money is Decimal quantized to cents, never float.

LineItem          description, quantity (default 1), unit_price, amount, category (filled later),
                  page (the page the line appears on, for traceability)
Adjustment        label, amount: invoice-level discounts (negative), service charges, shipping, tips
Invoice           doc_id, vendor_name (exactly as printed), invoice_number (optional),
                  invoice_date (ISO 8601), currency (ISO 4217), line_items, adjustments, subtotal,
                  tax, tax_inclusive (tax printed as already included in the total), total,
                  source_pages
ExtractionResult  invoice (or null), uncertain_fields, unreadable
Label             ground truth: Invoice plus optional is_duplicate_of (doc_id) and expected_flag
"""
