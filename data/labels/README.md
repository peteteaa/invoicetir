# Labels

Ground truth for the eval: `<doc_id>.json`, one `Label` each (see `schemas.py`).
`prefill/` holds `ESCALATION_MODEL`'s pre-fill for each document (see `label_app.py`).

## The first 30 (2026-11-02)

- **Sample:** 30 documents drawn at random from the 991 Mendeley invoices,
  `random.Random(0).sample(sorted doc_ids, 30)`.
- **Who labeled them:** Claude (Opus 5.5), not a person, and not one of the compared models.
  The values were parsed from each PDF's exact text layer, not retyped. Every label passes
  `checks.py`, and labels were compared against the page images for a few documents.
  A human spot-checks some of them in `label_app.py`.
- **Dates:** every document in the dataset prints month first (`MM/DD/YYYY`): across all 991,
  the first number is never above 12 and the second often is.
- **Currency:** `$` on every document, with US addresses: USD.
- **Line amounts** are the "Net worth" column. Subtotal, tax and total come from the Total row.
- **Categories:** `office` = computers and furniture or furnishings (tables, rugs, cushions);
  everything else in this dataset (game consoles, clothing, shoes, books, wine glasses and racks,
  bedding, a home-brew kit) = `other`.
- **expected_flag** is false for all 30: the printed math is right on every one.
- **corrected_fields** compare each label with the pre-fill: `ESCALATION_MODEL` (kimi-k3) got
  every extracted field right on all 30, and differed only on 20 line-item categories in
  7 documents (game consoles it called office).
