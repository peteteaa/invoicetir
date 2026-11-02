"""Vendor normalization and duplicate detection.

Fuzzy-matches vendor names (rapidfuzz) against the registry in data/state/vendors.json; scores in
the configurable ambiguous band are decided by the model (prompts/vendor_match.md).
Duplicates: same normalized vendor + invoice number, or same vendor + total with dates within 3 days.
"""
