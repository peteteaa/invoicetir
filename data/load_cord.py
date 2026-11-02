"""Optional: `python data/load_cord.py [--split test] [--limit N]`

Downloads the CORD receipts dataset from Hugging Face (naver-clova-ix/cord-v2, CC BY 4.0: real
scanned Indonesian receipts with ground-truth parses), maps its line items, subtotal, tax and
total to our schema, and writes images to data/docs/cord/ and labels to data/labels/. Fields CORD
does not have are skipped (left null, so the eval doesn't score them): vendor, invoice number,
date. Splits: train 800, validation 100, test 100 receipts (test is about 230 MB).

Mapping (CORD gt_parse -> Label):
- menu[].nm -> description; cnt -> quantity (1 if absent); unitprice -> unit_price;
  price -> amount (the printed line total). A priced sub-item (an add-on printed under its
  item) becomes its own line item; an unpriced one is appended to its item's description.
  A menu item's discountprice becomes a negative adjustment.
- sub_total.subtotal_price -> subtotal, tax_price -> tax, discount_price -> a negative
  adjustment, service_price -> a positive adjustment. "etc" fields are skipped (no meaning).
- total.total_price -> total. Cash, change and card amounts are payment, not spend.
- currency -> IDR: CORD doesn't label it, but every receipt in it is Indonesian.
- expected_flag stays None: CORD doesn't say whether a receipt's own math is wrong.

Numbers: CORD prints rupiah with "." or "," as thousands separators ("60.000", "Rp. 111,000").
A separator followed by exactly two digits at the end is a decimal point ("2.00"); every other
separator is a thousands separator.
"""
import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.checks import check_invoice  # noqa: E402
from schemas import Adjustment, Label, LineItem  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASET = "naver-clova-ix/cord-v2"
DOCS_DIR = ROOT / "data" / "docs" / "cord"
LABELS_DIR = ROOT / "data" / "labels"


def number(text) -> Optional[Decimal]:
    """'60.000' -> 60000, 'Rp. 111,000' -> 111000, '-14.000' -> -14000, '2.00' -> 2.00."""
    if isinstance(text, list):  # a value printed twice ("subtotal_price": ["46.636", "46.636"])
        text = text[0] if text else None
    if text is None:
        return None
    s = str(text).strip()
    negative = s.startswith("-") or s.startswith("(")
    s = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", s):
        return None
    m = re.fullmatch(r"(.*?)[.,](\d{2})", s)
    whole, cents = (m[1], m[2]) if m and m[1] else (s, None)
    digits = re.sub(r"\D", "", whole) or "0"
    value = Decimal(f"{digits}.{cents}") if cents else Decimal(digits)
    return -value if negative else value


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _money(text) -> Optional[Decimal]:
    value = number(text)
    return None if value is None else value.quantize(Decimal("0.01"))


def to_label(doc_id: str, parse: dict) -> Label:
    items, adjustments = [], []
    for m in _as_list(parse.get("menu")):
        item = LineItem(description=(m.get("nm") or "").strip() or None,
                        quantity=number(m.get("cnt")) or Decimal(1),
                        unit_price=number(m.get("unitprice")), amount=_money(m.get("price")), page=1)
        extra = []
        for sub in _as_list(m.get("sub")):
            if sub.get("price"):
                extra.append(LineItem(description=(sub.get("nm") or "").strip() or None,
                                      quantity=number(sub.get("cnt")) or Decimal(1),
                                      unit_price=number(sub.get("unitprice")), amount=_money(sub.get("price")), page=1))
            elif sub.get("nm"):
                item.description = f"{item.description or ''} {sub['nm'].strip()}".strip()
        items += [item, *extra]
        if m.get("discountprice"):
            adjustments.append(Adjustment(label=f"Discount: {item.description}", amount=-abs(_money(m["discountprice"]))))

    sub_total = parse.get("sub_total") or {}
    if isinstance(sub_total, list):  # a few receipts repeat the block; the first one is the bill
        sub_total = sub_total[0] if sub_total else {}
    if sub_total.get("discount_price"):
        adjustments.append(Adjustment(label="Discount", amount=-abs(_money(sub_total["discount_price"]))))
    if sub_total.get("service_price"):
        adjustments.append(Adjustment(label="Service charge", amount=_money(sub_total["service_price"])))

    total = parse.get("total") or {}
    if isinstance(total, list):
        total = total[0] if total else {}
    return Label(doc_id=doc_id, currency="IDR", line_items=items, adjustments=adjustments,
                 subtotal=_money(sub_total.get("subtotal_price")), tax=_money(sub_total.get("tax_price")),
                 total=_money(total.get("total_price")), source_pages=[1], expected_flag=None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download CORD receipts into data/docs/cord and data/labels.")
    ap.add_argument("--split", default="test", choices=["train", "validation", "test"])
    ap.add_argument("--limit", type=int, default=0, help="first N receipts (0 = the whole split)")
    ap.add_argument("--overwrite", action="store_true", help="replace labels that already exist")
    args = ap.parse_args(argv)

    from datasets import Image, load_dataset

    # data_files limits the download to the requested split; load_dataset(DATASET, split=...)
    # alone fetches and converts every split (about 2.3 GB) first.
    data = load_dataset(DATASET, data_files={args.split: f"data/{args.split}-*.parquet"},
                        split=args.split).cast_column("image", Image(decode=False))
    if args.limit:
        data = data.select(range(min(args.limit, len(data))))
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    written = skipped = 0
    math_ok, problems = 0, []
    for i, row in enumerate(data):
        doc_id = f"cord_{args.split}_{i:03d}"
        label_path = LABELS_DIR / f"{doc_id}.json"
        if label_path.exists() and not args.overwrite:
            skipped += 1   # may have been corrected by hand in label_app.py
            continue
        suffix = Path(row["image"].get("path") or "x.png").suffix.lower() or ".png"
        (DOCS_DIR / f"{doc_id}{suffix}").write_bytes(row["image"]["bytes"])
        parse = json.loads(row["ground_truth"])["gt_parse"]
        label = to_label(doc_id, parse)
        label_path.write_text(label.model_dump_json(indent=2) + "\n")
        written += 1
        failures = check_invoice(label.model_copy(update={"vendor_name": "x", "invoice_date": "2000-01-01"}))
        if failures:
            problems.append((doc_id, failures[0]))
        else:
            math_ok += 1

    print(f"{written} receipts written to {DOCS_DIR} and {LABELS_DIR}"
          + (f"; {skipped} existing labels kept (use --overwrite)" if skipped else ""))
    if written:
        print(f"{math_ok}/{written} have printed math our checks accept (ignoring the fields CORD lacks). "
              f"The rest are real receipts whose numbers don't add up our way, e.g.:")
        for doc_id, failure in problems[:5]:
            print(f"  {doc_id}: {failure}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
