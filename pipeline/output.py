"""Writes results to out/.

xlsx sheets: Summary, By category, By vendor, By month, Line items (every row keeps doc_id and
page), Flagged (with reasons). Plotly Sankey total -> category -> vendor, saved as HTML.

Also a Duplicates sheet (doc_id, duplicate_of, reason), so everything left out of the totals is
listed somewhere. Amounts are written to Excel as Decimal, formatted with two decimals.

The Sankey is one figure per currency (amounts are never converted). Vendors beyond the top
TOP_VENDORS by spend are grouped into "Other vendors". A Sankey can't draw a negative flow, so
net-negative buckets (an invoice-level discount larger than the tips and charges) are left out
of the figure only; the spreadsheet has them. Plotly needs floats, used for drawing only.
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import yaml

import config
from pipeline.aggregate import Spend, aggregate
from schemas import ProcessedDoc

OUT_DIR = config.ROOT / "out"
TOP_VENDORS = 12
MONEY_FORMAT = "#,##0.00"

# Validated categorical palette (light surface), assigned in categories.yaml order so a
# category keeps its color in every chart. Tax, adjustments and vendors are neutral.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
NEUTRAL = "#a3a29c"
BUCKETS = {"tax", "adjustments", "rounding"}  # from aggregate.py; not spend categories
SURFACE, TEXT = "#fcfcfb", "#0b0b0b"


def _sum(values) -> Decimal:
    return sum(values, Decimal(0))


def _colors() -> dict[str, str]:
    names = yaml.safe_load((config.ROOT / "categories.yaml").read_text())
    return dict(zip(names, SERIES))


# --- spreadsheet --------------------------------------------------------------------------

def _flagged(docs: list[ProcessedDoc]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"doc_id": d.doc_id, "source": d.source_path,
          "vendor": d.invoice.vendor_name if d.invoice else None,
          "total": d.invoice.total if d.invoice else None,
          "reasons": "; ".join(d.reasons), "uncertain fields": ", ".join(d.uncertain_fields)}
         for d in docs if d.status == "flagged"],
        columns=["doc_id", "source", "vendor", "total", "reasons", "uncertain fields"])


def _duplicates(docs: list[ProcessedDoc]) -> pd.DataFrame:
    return pd.DataFrame([{"doc_id": d.doc_id, "duplicate_of": d.duplicate_of, "reason": "; ".join(d.reasons)}
                         for d in docs if d.status == "duplicate"],
                        columns=["doc_id", "duplicate_of", "reason"])


def _format(sheet) -> None:
    """Money columns with two decimals; column widths that fit their contents."""
    for column in sheet.columns:
        header = column[0].value
        if header in ("amount", "total", "tax", "adjustments"):
            for cell in column[1:]:
                cell.number_format = MONEY_FORMAT
        width = max(len(str(c.value)) for c in column if c.value is not None)
        sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)


def write_xlsx(docs: list[ProcessedDoc], spend: Spend, target=OUT_DIR / "spend.xlsx"):
    """target is a path or a file-like object (e.g. BytesIO for a download button)."""
    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
    counts = pd.DataFrame({"documents": ["verified", "flagged", "duplicate"],
                           "count": [sum(d.status == s for d in docs) for s in ("verified", "flagged", "duplicate")]})
    sheets = {"By category": spend.by_category, "By vendor": spend.by_vendor, "By month": spend.by_month,
              "Line items": spend.rows, "Flagged": _flagged(docs), "Duplicates": _duplicates(docs)}
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        spend.summary.to_excel(writer, sheet_name="Summary", index=False)
        counts.to_excel(writer, sheet_name="Summary", index=False, startrow=len(spend.summary) + 2)
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
        for sheet in writer.sheets.values():
            _format(sheet)
    return target


# --- Sankey -------------------------------------------------------------------------------

def sankey(spend: Spend, currency: str) -> go.Figure:
    """Total -> category -> vendor for one currency. Tax, adjustments and rounding end at the
    category column: linking them to every vendor would only add clutter."""
    rows = spend.rows[spend.rows.currency == currency]
    vendor_totals = rows[~rows.category.isin(BUCKETS)].groupby("vendor")["amount"].agg(_sum)
    top = set(vendor_totals.sort_values(ascending=False).index[:TOP_VENDORS])
    others = len(vendor_totals) - len(top)
    vendor = rows.vendor.where(rows.vendor.isin(top), f"Other vendors ({others})")
    flows = rows.assign(vendor=vendor).groupby(["category", "vendor"], as_index=False)["amount"].agg(_sum)
    flows = flows[flows.amount > 0]
    by_category = flows.groupby("category")["amount"].agg(_sum).sort_values(ascending=False)
    vendor_flows = flows[~flows.category.isin(BUCKETS)]
    by_vendor = vendor_flows.groupby("vendor")["amount"].agg(_sum).sort_values(ascending=False)
    total = _sum(by_category)

    colors = _colors()
    labels = [f"Total  {total:,.2f}"]
    node_colors = [NEUTRAL]
    index = {}
    for kind, series in (("category", by_category), ("vendor", by_vendor)):
        for name, amount in series.items():
            index[kind, name] = len(labels)
            labels.append(f"{name}  {amount:,.2f}")
            node_colors.append(colors.get(name, NEUTRAL) if kind == "category" else NEUTRAL)

    # (source node, target node, amount, category whose color the link takes)
    links = [(0, index["category", name], amount, name) for name, amount in by_category.items()]
    links += [(index["category", f.category], index["vendor", f.vendor], f.amount, f.category)
              for f in vendor_flows.itertuples()]

    fig = go.Figure(go.Sankey(
        valueformat=",.2f", valuesuffix=f" {currency}",
        node={"label": labels, "color": node_colors, "pad": 14, "thickness": 10, "line": {"width": 0},
              "hovertemplate": "%{label}<extra></extra>"},
        link={"source": [l[0] for l in links], "target": [l[1] for l in links],
              "value": [float(l[2]) for l in links],
              "color": [colors.get(l[3], NEUTRAL) + "59" for l in links],  # 35% opacity
              "hovertemplate": "%{source.label} → %{target.label}: %{value}<extra></extra>"}))
    fig.update_layout(title=f"Spend in {currency}: category → vendor", font={"color": TEXT, "size": 12},
                      paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, margin={"l": 16, "r": 16, "t": 48, "b": 16},
                      height=max(420, 28 * len(by_vendor)))
    return fig


def write_sankey(spend: Spend, path: Path = OUT_DIR / "sankey.html") -> Path:
    """One figure per currency in a single HTML file (plotly.js embedded, so it works offline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    figures = [sankey(spend, c).to_html(full_html=False, include_plotlyjs=(i == 0))
               for i, c in enumerate(spend.summary.currency)]
    body = "\n".join(figures) or "<p>No verified invoices.</p>"
    path.write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>Spend Sankey</title></head>"
                    f"<body style='background:{SURFACE};margin:16px'>{body}</body></html>")
    return path


def write(docs: list[ProcessedDoc], out_dir: Path = OUT_DIR) -> dict[str, Path]:
    """Aggregate and write spend.xlsx and sankey.html to out_dir."""
    spend = aggregate(docs)
    return {"xlsx": write_xlsx(docs, spend, out_dir / "spend.xlsx"),
            "sankey": write_sankey(spend, out_dir / "sankey.html")}
