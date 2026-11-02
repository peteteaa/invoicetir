"""pipeline/run.py. No mocks: bad-file handling runs anywhere (it never reaches the model); the
end-to-end test calls Fireworks and skips without FIREWORKS_API_KEY or data/docs. About $0.01."""
import shutil
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

import config
from pipeline import run

from sample_docs import DOCS  # noqa: E402


def test_unreadable_file_is_flagged_not_raised(tmp_path):
    bad = tmp_path / "scan.pdf"
    bad.write_bytes(b"not a pdf at all")
    doc = run.process(bad)
    assert doc.status == "flagged" and doc.invoice is None
    assert doc.reasons[0].startswith("could not load the file")
    assert doc.cost_usd == Decimal(0)


def test_unsupported_file_type_is_flagged(tmp_path):
    notes = tmp_path / "notes.docx"
    notes.write_bytes(b"x")
    assert "unsupported file type" in run.process(notes).reasons[0]


def test_find_files_rejects_two_files_with_the_same_doc_id(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    (tmp_path / "a" / "inv.pdf").write_bytes(b"x")
    (tmp_path / "b" / "inv.png").write_bytes(b"x")
    with pytest.raises(ValueError, match="doc_id 'inv'"):
        run.find_files([tmp_path])


@pytest.mark.skipif(not config.FIREWORKS_API_KEY or not DOCS.is_dir(), reason="needs FIREWORKS_API_KEY and data/docs")
def test_end_to_end_small_batch(tmp_path):
    batch = tmp_path / "in"
    batch.mkdir()
    shutil.copy(DOCS / "invoice_22.pdf", batch)
    shutil.copy(DOCS / "invoice_22.pdf", batch / "invoice_22_resent.pdf")   # same invoice sent twice
    shutil.copy(DOCS / "invoice_106.pdf", batch)
    (batch / "broken.pdf").write_bytes(b"garbage")

    result = run.run([batch], out_dir=tmp_path / "out", persist_state=False)
    status = {d.doc_id: d.status for d in result.docs}
    assert status == {"broken": "flagged", "invoice_106": "verified", "invoice_22": "verified",
                      "invoice_22_resent": "duplicate"}

    # Spend counts each verified invoice once, to the cent: 618.32 + 23099.25
    assert result.spend.summary.amount.tolist() == [Decimal("23717.57")]
    assert all(i.category for d in result.docs if d.status == "verified" for i in d.invoice.line_items)
    assert result.cost_usd is not None and result.cost_usd > 0

    sheets = pd.read_excel(result.files["xlsx"], sheet_name=None)
    assert sheets["Flagged"].doc_id.tolist() == ["broken"]
    assert sheets["Duplicates"].duplicate_of.tolist() == ["invoice_22"]
    assert result.files["sankey"].read_text().count("<div") > 0
