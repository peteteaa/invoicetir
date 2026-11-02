"""Streamlit labeling tool: `streamlit run label_app.py`

Shows a document from data/docs/ next to an editable form pre-filled by ESCALATION_MODEL's
extraction. A human verifies or corrects every field and saves ground truth to
data/labels/<doc_id>.json, tracking which fields were corrected.

The pre-fill (extraction + line-item categories, both by ESCALATION_MODEL) is cached in
data/labels/prefill/<doc_id>.json, so reopening a document costs nothing, and corrected_fields
can always be recomputed. corrected_fields compares values, not formatting ("4" = "4.00"); line
items are compared by position, so an inserted line also counts every line after it.

The form runs checks.py live on what you typed. If a check fails because the document itself
prints the math wrong (and you copied it exactly), set "printed math is wrong" (expected_flag).
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

import pandas as pd
import pymupdf
import streamlit as st
from pydantic import ValidationError

import config
from pipeline import categorize, extract, ingest
from pipeline.checks import check_invoice
from schemas import Invoice, Label

DOCS_DIR = config.ROOT / "data" / "docs"
LABELS_DIR = config.ROOT / "data" / "labels"
PREFILL_DIR = LABELS_DIR / "prefill"
DISPLAY_DPI = 110

FIELDS = ["vendor_name", "invoice_number", "invoice_date_printed", "invoice_date",
          "currency_printed", "currency", "subtotal", "tax", "total"]
LINE_COLUMNS = ["description", "quantity", "unit_price", "amount", "category", "page"]
ADJUSTMENT_COLUMNS = ["label", "amount"]
NOT_COMPARED = {"doc_id", "source_pages"}  # set by code, not read from the document
FLAG_CHOICES = {"not checked yet": None, "printed math is right": False, "printed math is wrong": True}


# --- pure helpers (tested in tests/test_label_app.py) ---------------------------------------

def _natural(path: Path):
    """invoice_2 before invoice_10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.stem)]


def documents(directory: Path = DOCS_DIR) -> dict[str, Path]:
    suffixes = ingest.PDF_SUFFIXES | ingest.IMAGE_SUFFIXES
    paths = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]
    return {p.stem: p for p in sorted(paths, key=_natural)}


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        not isinstance(value, str) and pd.isna(value))


def table_rows(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    """Editor rows as dicts: blanks become None, empty rows are dropped."""
    out = []
    for record in df.to_dict("records"):
        row = {c: None if _blank(record.get(c)) else record[c] for c in columns}
        row = {c: v.strip() if isinstance(v, str) else v for c, v in row.items()}
        if "page" in row and row["page"] is not None:
            row["page"] = int(row["page"])
        if any(v is not None for v in row.values()):
            out.append(row)
    return out


def _flat(invoice: Optional[dict]) -> dict:
    """{"total": ..., "line_items[0].amount": ...} with values as Python objects (Decimal).
    No invoice (a failed pre-fill) is an empty one, so defaults like tax_inclusive=False match."""
    out = {}
    for key, value in Invoice.model_validate(invoice or {}).model_dump().items():
        if key in NOT_COMPARED:
            continue
        if isinstance(value, list):
            for i, item in enumerate(value):
                out.update({f"{key}[{i}].{k}": v for k, v in item.items()})
        else:
            out[key] = value
    return out


def corrected_fields(prefilled: Optional[dict], label: dict) -> list[str]:
    """Fields whose value differs between the pre-fill and the label."""
    before, after = _flat(prefilled), _flat(label)
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


# --- model pre-fill -----------------------------------------------------------------------

def prefill(doc_id: str, path: Path) -> dict:
    """ESCALATION_MODEL's extraction and categories, cached in PREFILL_DIR (failures too, so a
    rerun never calls the model again; "Pre-fill again" deletes the cache)."""
    cache = PREFILL_DIR / f"{doc_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    attempt = extract.extract(ingest.load(path, doc_id), model=config.ESCALATION_MODEL, stage="label_prefill")
    result = attempt.result
    if result is not None and result.invoice is not None:
        categorize.categorize(result.invoice, model=config.ESCALATION_MODEL)
    data = {"model": config.ESCALATION_MODEL, "error": attempt.error,
            "unreadable": bool(result and result.unreadable),
            "uncertain_fields": result.uncertain_fields if result else [],
            "invoice": result.invoice.model_dump(mode="json") if result and result.invoice else None}
    PREFILL_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# --- UI -----------------------------------------------------------------------------------

@st.cache_data(max_entries=30)
def page_images(path: str) -> list[bytes]:
    p = Path(path)
    if p.suffix.lower() in ingest.IMAGE_SUFFIXES:
        return [ingest.load(p).pages[0].image_png]
    with pymupdf.open(p) as pdf:
        count = pdf.page_count
    return [ingest.render_page(p, i, DISPLAY_DPI) for i in range(1, count + 1)]


@st.cache_data(max_entries=30)
def text_layer(path: str) -> Optional[str]:
    doc = ingest.load(Path(path))
    return doc.text() if doc.kind == "text" else None


def _text(value) -> str:
    return "" if value is None else str(value)


def _editor_frame(items: list[dict], columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame([{c: item.get(c) if c in ("category", "page") else _text(item.get(c)) for c in columns}
                       for item in items], columns=columns)
    if "page" in df:
        df["page"] = df["page"].astype("Int64")
    return df


def sidebar(docs: dict[str, Path]) -> str:
    labeled = {p.stem for p in LABELS_DIR.glob("*.json")}
    ids = list(docs)
    if "next_doc" in st.session_state:  # set by Save; a widget's state can only change before it is drawn
        st.session_state.doc = st.session_state.pop("next_doc")
    st.sidebar.title("Label invoices")
    st.sidebar.progress(len(labeled & set(ids)) / max(len(ids), 1),
                        text=f"{len(labeled & set(ids))} of {len(ids)} labeled")
    doc_id = st.sidebar.selectbox("Document", ids, key="doc",
                                  format_func=lambda d: f"✓ {d}" if d in labeled else d)
    if st.sidebar.button("Next unlabeled →"):
        st.session_state.next_doc = next_unlabeled(ids, doc_id, labeled)
        st.rerun()
    st.sidebar.caption("Labeling in parallel? Each person takes a different range of documents.")
    return doc_id


def next_unlabeled(ids: list[str], current: str, labeled: set) -> str:
    start = ids.index(current) + 1
    return next((d for d in ids[start:] + ids[:start] if d not in labeled), current)


def main():
    st.set_page_config(page_title="Label invoices", layout="wide")
    if "saved_message" in st.session_state:  # set by Save, shown after the rerun
        st.toast(st.session_state.pop("saved_message"))
    docs = documents()
    if not docs:
        st.error(f"No documents in {DOCS_DIR}. See the Data section of the README.")
        st.stop()
    doc_id = sidebar(docs)
    path = docs[doc_id]
    saved_path = LABELS_DIR / f"{doc_id}.json"

    if not config.ESCALATION_MODEL or not config.FIREWORKS_API_KEY:
        st.error("Set FIREWORKS_API_KEY and ESCALATION_MODEL in .env (see .env.example).")
        st.stop()
    try:
        with st.spinner(f"Pre-filling with {config.ESCALATION_MODEL}..."):
            pre = prefill(doc_id, path)
    except Exception as e:  # API errors are not cached: the next rerun tries again
        st.error(f"Pre-fill failed: {type(e).__name__}: {e}")
        st.stop()
    saved = json.loads(saved_path.read_text()) if saved_path.exists() else None
    start = saved or pre["invoice"] or {}
    uncertain = set(pre["uncertain_fields"])
    # Widget keys per document and per starting values: switching documents, or reopening one
    # after saving, starts a fresh form (an editor's edits would otherwise be replayed on new rows).
    k = f"{doc_id}:{hashlib.md5(json.dumps(start, sort_keys=True).encode()).hexdigest()[:8]}:"
    images = page_images(str(path))

    left, right = st.columns([5, 6], gap="large")
    with left:
        st.subheader(doc_id)
        for i, png in enumerate(images, 1):
            st.image(png, caption=f"page {i}", width="stretch")
        layer = text_layer(str(path))
        if layer:
            with st.expander("Text layer (copy exact values from here)"):
                st.code(layer, language=None)

    with right:
        if saved:
            st.info("Already labeled: editing the saved label.")
        if pre["error"] or pre["invoice"] is None:
            st.warning(f"Pre-fill by {pre['model']} has no invoice: {pre['error'] or 'unreadable'}. Fill it in by hand.")
        if uncertain:
            st.caption(f"⚠ the model was unsure about: {', '.join(sorted(uncertain))}")

        values = {}
        cols = st.columns(3)
        for i, field in enumerate(FIELDS):
            mark = " ⚠" if field in uncertain else ""
            values[field] = cols[i % 3].text_input(field + mark, _text(start.get(field)), key=k + field)
        tax_inclusive = st.checkbox("tax_inclusive: the tax is already inside the prices",
                                    bool(start.get("tax_inclusive")), key=k + "tax_inclusive")

        st.markdown("**Line items**" + (" ⚠" if any(f.startswith("line_items") for f in uncertain) else ""))
        lines = st.data_editor(
            _editor_frame(start.get("line_items", []), LINE_COLUMNS), key=k + "lines", num_rows="dynamic",
            width="stretch", hide_index=True,
            column_config={"category": st.column_config.SelectboxColumn(options=list(categorize.load_categories())),
                           "page": st.column_config.NumberColumn(min_value=1, step=1)})
        st.markdown("**Adjustments** (discounts negative; service charges, shipping, tips)")
        adjustments = st.data_editor(_editor_frame(start.get("adjustments", []), ADJUSTMENT_COLUMNS),
                                     key=k + "adjustments", num_rows="dynamic", width="stretch",
                                     hide_index=True)

        others = [d for d in docs if d != doc_id]
        duplicate = st.selectbox("Duplicate of", [None] + others, key=k + "dup",
                                 index=others.index(start["is_duplicate_of"]) + 1
                                 if start.get("is_duplicate_of") in others else 0,
                                 format_func=lambda d: "not a duplicate" if d is None else d)
        flag_names = list(FLAG_CHOICES)
        flag = st.radio("Printed math (expected_flag)", flag_names, horizontal=True, key=k + "flag",
                        index=list(FLAG_CHOICES.values()).index(start.get("expected_flag")))

        data = {"doc_id": doc_id, "source_pages": list(range(1, len(images) + 1)),
                **{f: values[f].strip() or None for f in FIELDS}, "tax_inclusive": tax_inclusive,
                "line_items": table_rows(lines, LINE_COLUMNS),
                "adjustments": table_rows(adjustments, ADJUSTMENT_COLUMNS),
                "is_duplicate_of": duplicate, "expected_flag": FLAG_CHOICES[flag]}
        try:
            label = Label.model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                st.error(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}")
            st.stop()

        failures = check_invoice(label)
        if failures:
            st.warning("Checks fail:\n" + "\n".join(f"- {f}" for f in failures) +
                       "\n\nIf the document itself prints this wrong, choose \"printed math is wrong\".")
        else:
            st.success("Checks pass.")
        label.corrected_fields = corrected_fields(pre["invoice"], label.model_dump(mode="json"))
        st.caption(f"Corrected from the pre-fill: {', '.join(label.corrected_fields) or 'nothing'}")

        save, again = st.columns([1, 1])
        if save.button("Save and next →", type="primary", width="stretch"):
            LABELS_DIR.mkdir(parents=True, exist_ok=True)
            saved_path.write_text(label.model_dump_json(indent=2) + "\n")
            st.session_state.saved_message = f"Saved {saved_path.relative_to(config.ROOT)}"
            labeled = {p.stem for p in LABELS_DIR.glob("*.json")}
            st.session_state.next_doc = next_unlabeled(list(docs), doc_id, labeled)
            st.rerun()
        if again.button("Pre-fill again", width="stretch",
                        help="Delete the cached pre-fill and ask the model again"):
            (PREFILL_DIR / f"{doc_id}.json").unlink(missing_ok=True)
            st.rerun()


if __name__ == "__main__":
    main()
