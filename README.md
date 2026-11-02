# Invoice spend aggregation

Hackathon project. A small open vision model extracts structured data from invoices and
receipts. Deterministic Python checks the math and aggregates. The output is a spreadsheet and
a Sankey spend diagram. We compare it against a large-model baseline on accuracy, cost and
latency, using real documents only (no synthetic data).

> **Status:** scaffold only. Every module holds a docstring describing its job; nothing is
> implemented yet. Build order: schemas → checks → tests → extraction → labeling tool →
> rest of pipeline → eval → demo.

## Stack

Python 3.11+. The `openai` client pointed at Fireworks
(`base_url="https://api.fireworks.ai/inference/v1"`, key from `FIREWORKS_API_KEY`).
Libraries: pydantic, pymupdf, rapidfuzz, pandas, openpyxl, plotly, streamlit, datasets,
pytest, pyyaml.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # fill in the key, model IDs and prices
```

## Config (`config.py`, all from env vars)

| Variable | Meaning |
|---|---|
| `FIREWORKS_API_KEY` | Fireworks API key |
| `VISION_MODEL` | small vision model for scans and photos |
| `TEXT_MODEL` | cheap text model for PDFs with a text layer |
| `ESCALATION_MODEL` | large model for failed documents and the baseline |
| `<ROLE>_INPUT_PRICE`, `<ROLE>_OUTPUT_PRICE` | USD per million tokens, per model, for cost tracking |

Also `MAX_RETRIES = 2`, `ZOOM_ENABLED` (off by default) and the vendor fuzzy-match ambiguous
score band. Model IDs come from the Fireworks model library.

## Hard rules

- The model never does math. All money is `Decimal`, never float, quantized to cents.
- Every model response is validated with Pydantic; invalid JSON counts as a retry.
- Every model call is logged: doc_id, stage, model, input/output tokens, latency, cost.
- Simple, readable code. No unnecessary abstraction.

## Schema (`schemas.py`)

- **LineItem:** description, quantity (Decimal, default 1), unit_price, amount, category
  (optional, filled later), page.
- **Adjustment:** label, amount. Invoice-level discounts (negative), service charges,
  shipping, tips.
- **Invoice:** doc_id, vendor_name (exactly as printed), invoice_number (optional),
  invoice_date (ISO 8601), currency (ISO 4217), line_items, adjustments, subtotal, tax,
  tax_inclusive, total, source_pages.
- **ExtractionResult:** invoice (or null), uncertain_fields (field names the model is unsure
  about), unreadable (bool).
- **Label** (ground truth): an Invoice plus optional `is_duplicate_of` (doc_id) and
  `expected_flag` (true if the document's own printed math is wrong).

Model-output fields may be null, because the prompt says to return null for anything not
printed. The checks decide whether a missing value is a failure.

## Extraction prompt rules (`prompts/extract.md`)

- Copy printed values exactly. Never compute, infer or fill in a missing value.
- If a value is not on the document, return null and list it in `uncertain_fields`.
- Numbers as plain digits, no currency symbols or thousands separators.
- JSON only, matching the schema, in JSON mode.

## Pipeline (`pipeline/`)

1. **`ingest.py`:** load PDFs and images. A PDF with a usable text layer (PyMuPDF) goes to
   `TEXT_MODEL` as text; otherwise its pages are rendered to images for `VISION_MODEL`.
2. **`extract.py`:** call the model with the extraction prompt. A `zoom` tool (high-res crop
   of a page region) sits behind a feature flag, off by default.
3. **`checks.py`:** `check_invoice()` returns specific, human-readable failures, e.g.
   `line 3: 4 x 12.50 != 48.00`. Checks (tolerance 0.01):
   - quantity × unit_price = amount
   - sum of line amounts = subtotal
   - subtotal + adjustments + tax = total; if `tax_inclusive`, sum of line amounts +
     adjustments = total
   - if no subtotal is printed: sum of line amounts + adjustments + tax = total
   - valid date, valid currency code; a missing total, date, currency or vendor is a failure
4. **`repair.py`:** on failures, re-prompt the same model with the exact failures, up to
   `MAX_RETRIES` (invalid JSON uses the same budget). Then escalate once to
   `ESCALATION_MODEL` with every failure so far. If it still fails, flag for human review
   with the reason. Never guess. A document the small model calls unreadable goes straight
   to escalation.

   **The escalation model can also escalate to a human.** Passing the checks is not enough
   if `ESCALATION_MODEL` says it is unsure. Its extraction goes to human review, with the
   uncertain fields as the reason, when it:
   - marks the document `unreadable`, or
   - lists, in `uncertain_fields`, any field that changes the spend numbers or who they're
     attributed to: `vendor_name`, `total`, `subtotal`, `tax`, an adjustment, or a line
     item's `quantity`, `unit_price`, `amount` or `description`.

   Some uncertainty comes from fixed normalization rules, not from the model: a `$` that
   could be several currencies, or an ambiguous date like `07/10/2020`. It does not send a
   document to review on its own; it stays recorded in `uncertain_fields`, which is kept in
   the output. A flagged document is never counted in the totals until a person verifies it.
5. **`clean.py`:** normalize vendor names with rapidfuzz against a registry of seen vendors
   (`data/state/vendors.json`, canonical name → every name printed for it). Names are compared
   lowercased, without punctuation or legal suffixes (Inc, LLC, GmbH...), scored with
   `fuzz.ratio`. A score of `VENDOR_MATCH_HIGH` (95) or more is the same vendor. The model
   decides when the score is `VENDOR_MATCH_LOW` (85) or more, or when one name is the other
   plus more words ("Starbucks" / "Starbucks Coffee #1234"); unsure or unusable answer =
   different vendors. Anything else is a new vendor. Every answer adds the name to the
   registry, so a name is never asked about twice. (`WRatio` was tried first: on the 991
   sample invoices it merged 11 different companies, e.g. "Gates and Sons" into "Yates and
   Sons", and sent 80% of names to the model. `ratio` with this rule: 0 wrong merges.)
   Duplicates: same normalized vendor + invoice number (ignoring spaces and punctuation), or
   same vendor + total with dates within 3 days. Two invoices with different invoice numbers
   are never duplicates. The first one seen is the original; duplicates point to it.
6. **`categorize.py`:** categories and their descriptions live in `categories.yaml`
   (software, cloud, travel, meals, contractors, office, marketing, other). The model assigns
   a category per line item, reusing a vendor's prior category for similar descriptions.
7. **`aggregate.py`:** pandas spend by vendor, category and month, kept per currency (no
   conversion). Tax and adjustments are their own buckets so category totals add up exactly.
   A `rounding` bucket takes the cent or two that checks' tolerance allows between the parts
   and the printed total. Tax-inclusive tax is not added again. Duplicates and flagged
   invoices are excluded (only counted). Asserts that all totals equal the exact sum of
   verified invoices. `aggregate(docs)` returns a `Spend`: `rows` (one per line item, tax,
   adjustment, with doc_id and page), `by_category`, `by_vendor`, `by_month`, `summary`.
8. **`output.py`:** xlsx with sheets Summary, By category, By vendor, By month, Line items
   (every row keeps doc_id and page), Flagged (with reasons) and Duplicates. Plotly Sankey
   total → category → vendor, saved as HTML, one figure per currency; vendors past the top 12
   are grouped as "Other vendors", and tax and adjustments end at their own node.
   `output.write(docs)` writes `out/spend.xlsx` and `out/sankey.html`; `write_xlsx` also
   takes a `BytesIO` for a download button, and `sankey(spend, currency)` returns the figure.

`run.py` runs the stages in order and is used by the demo and the eval. `llm.py` is the only
module that calls Fireworks; it validates replies and writes the call log.

Assumes one invoice per document.

## Data

- Real documents go in `data/docs/`. Ground truth goes in `data/labels/<doc_id>.json`.
- **Invoice dataset:** download it from https://data.mendeley.com/datasets/tnj49gpmtz/1 and
  put the PDFs directly in `data/docs/` (`data/docs/invoice_1.pdf`, ...). `data/docs/` is
  gitignored, so the PDFs stay out of the repo.
- **`label_app.py`:** Streamlit labeling tool. Shows the document next to an editable form
  pre-filled by `ESCALATION_MODEL`'s extraction. A human verifies or corrects every field
  and saves to `data/labels/`. Tracks which fields were corrected.
- **`data/load_cord.py`** (optional): downloads the CORD receipts dataset from Hugging Face
  (verify the ID, likely `naver-clova-ix/cord-v2`), maps line items, subtotal, tax and total
  to our schema, and writes images and labels into `data/docs/` and `data/labels/`. Fields
  CORD doesn't have are skipped.

## Eval (`eval/run_eval.py`)

Runs only on documents that have labels.

| Config | What runs |
|---|---|
| `small_raw` | small model, no checks |
| `small_checks` | small model + checks + retries |
| `small_escalate` | small model + checks + retries + escalation |
| `large_per_doc` | large model per document, no checks |
| `large_all` | large model given all documents at once, asked for the spend summary directly |

Metrics (`eval/metrics.py`): field accuracy, line-item accuracy, dollar error per category,
duplicate recall and flag precision/recall (only where labels include them), cost per
document, p50/p95 latency. Writes `out/results.csv` and `out/accuracy_vs_cost.html`, one
point per config.

Caveats:
- Labels are pre-filled by `ESCALATION_MODEL`, which is also a compared config, so ground
  truth may lean toward it. The share of corrected fields is reported next to the results.
- `large_all` is split into batches if the documents exceed Fireworks' per-request image or
  context limits. It is scored only on dollar error per category, cost and latency.

## Demo (`app.py`)

Streamlit: upload files, run the pipeline, show the spend table, Sankey, flagged items with
reasons, cost and latency stats, and an xlsx download.

## Tests

pytest for `checks.py` and `aggregate.py` using small handcrafted JSON fixtures in
`tests/fixtures/` (not documents). `tests/test_live_extract.py` makes one real extraction
call and is skipped when `FIREWORKS_API_KEY` is unset.

## Commands (once implemented)

```bash
pytest
python -m pipeline.run data/docs              # whole pipeline -> out/spend.xlsx, out/sankey.html
python -m pipeline.run a.pdf b.jpg --fresh    # --fresh: don't read or update data/state/
streamlit run label_app.py
python data/load_cord.py
python -m eval.run_eval --config all
streamlit run app.py
```

## Done when

- `pytest` passes
- `streamlit run label_app.py` labels a document and saves ground truth
- `python -m eval.run_eval --config all` writes results.csv and the chart
- `streamlit run app.py` works end to end
- this README explains setup and every command

## File tree

```
config.py  schemas.py  llm.py  categories.yaml  .env.example
prompts/   extract.md  repair.md  categorize.md  vendor_match.md  summary.md
pipeline/  ingest.py  extract.py  checks.py  repair.py  clean.py  categorize.py
           aggregate.py  output.py  run.py
label_app.py  app.py
eval/      run_eval.py  metrics.py
data/      load_cord.py  docs/  labels/  state/
tests/     test_checks.py  test_aggregate.py  test_live_extract.py  fixtures/
out/       generated xlsx, Sankey, results.csv, chart
```

---

## Also in this repo: `agent.py`

A small terminal coding agent (tool-calling loop) on Fireworks' serverless API. It is separate
from the invoice pipeline.

```bash
.venv/bin/python agent.py --list-models
.venv/bin/python agent.py --model accounts/fireworks/models/<name>
.venv/bin/python agent.py -p "add a test for foo()" --yolo
```

Flags: `--model ID`, `--list-models`, `-p/--prompt TASK`, `--yolo`, `--max-steps N`
(default 30), `--cwd PATH`. Reading tools run automatically; `write_file`, `edit_file` and
`run_bash` ask `[y/N/a]` first. Logs go to `logs/<timestamp>.jsonl`.
