"""pytest for store.py and the server.py endpoints that don't call a model, using handcrafted
JSON fixtures in tests/fixtures/ (not documents). Every test uses its own store directory."""
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
import store
from schemas import Invoice, ProcessedDoc

FIXTURES = Path(__file__).parent / "fixtures"
D = Decimal


def invoice(name: str, **changes) -> Invoice:
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    data.update(changes)
    for item in data["line_items"]:
        item["category"] = "office"
    return Invoice.model_validate(data)


def put(directory, doc_id, status="verified", name="clean_invoice", **changes) -> store.StoredDoc:
    stored = store.StoredDoc(doc=ProcessedDoc(doc_id=doc_id, source_path=f"{doc_id}.pdf", status=status,
                                              invoice=invoice(name, doc_id=doc_id, **changes),
                                              vendor="Jacobs, Russell and Perry"))
    store.save(stored, directory)
    return stored


def test_effective_without_review_is_the_pipeline_version(tmp_path):
    put(tmp_path, "a", status="flagged")
    assert store.effective(store.get("a", tmp_path)).status == "flagged"


def test_approve_counts_a_flagged_document_and_keeps_the_failures(tmp_path):
    put(tmp_path, "math", status="flagged", name="printed_math_wrong")
    store.review("math", "approve", directory=tmp_path)
    doc = store.effective(store.get("math", tmp_path))
    assert doc.status == "verified"
    assert doc.reasons and all(r.startswith("approved by reviewer despite:") for r in doc.reasons)


def test_approve_with_edits_records_changed_fields_and_never_touches_the_pipeline_version(tmp_path):
    put(tmp_path, "a", status="flagged")
    edited = invoice("clean_invoice", doc_id="a", total="23099.26")
    stored = store.review("a", "approve", edited, directory=tmp_path)
    assert stored.review.changed_fields == ["total"]
    assert stored.doc.invoice.total == D("23099.25")
    assert store.effective(stored).invoice.total == D("23099.26")


def test_approve_needs_total_currency_and_a_valid_date(tmp_path):
    put(tmp_path, "a", status="flagged")
    with pytest.raises(ValueError, match="total, invoice_date"):
        store.review("a", "approve", invoice("clean_invoice", total=None, invoice_date="12/08/2013"),
                     directory=tmp_path)
    assert store.get("a", tmp_path).review is None


def test_exclude_and_not_duplicate(tmp_path):
    put(tmp_path, "a")
    put(tmp_path, "b", status="duplicate", duplicate_of="a")
    with pytest.raises(ValueError):
        store.review("a", "not_duplicate", directory=tmp_path)
    store.review("a", "exclude", note="personal purchase", directory=tmp_path)
    store.review("b", "not_duplicate", directory=tmp_path)
    a, b = (store.effective(s) for s in store.load_all(tmp_path))
    assert a.status == "flagged" and a.reasons[-1] == "excluded by reviewer: personal purchase"
    assert b.status == "verified" and b.duplicate_of is None


def test_clear_review_restores_the_pipeline_status(tmp_path):
    put(tmp_path, "a", status="flagged")
    store.review("a", "approve", directory=tmp_path)
    assert store.effective(store.clear_review("a", tmp_path)).status == "flagged"


def test_paths_inside_the_repo_are_saved_relative(tmp_path):
    stored = put(tmp_path, "a")
    stored.doc.source_path = str(store.config.ROOT / "data" / "docs" / "a.pdf")
    store.save(stored, tmp_path)
    doc = store.get("a", tmp_path).doc
    assert doc.source_path == "data/docs/a.pdf"
    assert store.source(doc) == store.config.ROOT / "data" / "docs" / "a.pdf"


def test_an_absolute_path_from_another_clone_falls_back_to_this_repo():
    doc = ProcessedDoc(doc_id="a", status="verified",
                       source_path="/Users/someone/elsewhere/fireworksai/data/docs/uploads/a.pdf")
    assert store.source(doc) == store.config.ROOT / "data" / "docs" / "uploads" / "a.pdf"


def test_load_all_sorts_doc_ids_naturally(tmp_path):
    for doc_id in ["invoice_10", "invoice_2", "invoice_1"]:
        put(tmp_path, doc_id)
    assert [s.doc.doc_id for s in store.load_all(tmp_path)] == ["invoice_1", "invoice_2", "invoice_10"]


# --- server.py ----------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(server, "DOCS_DIR", tmp_path / "files")
    monkeypatch.setattr(server.config, "FIREWORKS_API_KEY", "")
    # keep data/state/ untouched: approving normalizes the vendor and may fill in categories
    monkeypatch.setattr(server.clean, "load_registry", lambda: {})
    monkeypatch.setattr(server.clean, "save_registry", lambda registry: None)
    monkeypatch.setattr(server.categorize, "load_memory", lambda: {})
    monkeypatch.setattr(server.categorize, "save_memory", lambda memory: None)
    return TestClient(server.app)


def test_spend_counts_approvals_and_money_is_never_a_float(client, tmp_path):
    put(tmp_path / "docs", "a")
    put(tmp_path / "docs", "math", status="flagged", name="printed_math_wrong")
    before = client.get("/api/spend").json()
    assert before["summary"]["amount"] == "23099.25" and before["month"] == "2013-08"

    r = client.post("/api/docs/math/review", json={"action": "approve"})
    assert r.status_code == 200 and r.json()["row"]["status"] == "verified"
    after = client.get("/api/spend", params={"month": "all"}).json()
    assert after["summary"]["amount"] == str(D("23099.25") + D("149.03"))
    assert all(isinstance(row["amount"], str) for row in after["by_category"] + after["by_vendor"])


def test_spend_defaults_to_the_latest_month_with_invoices(client, tmp_path):
    put(tmp_path / "docs", "old")
    put(tmp_path / "docs", "new", invoice_number="2", invoice_date="2020-10-15")
    s = client.get("/api/spend", params={"month": "2026-11"}).json()
    assert s["month"] == "2020-10" and s["months"] == ["2013-08", "2020-10"]
    assert s["previous"] == {"month": "2020-09", "amount": "0"}


def test_check_endpoint_runs_checks_without_saving(client, tmp_path):
    put(tmp_path / "docs", "a")
    r = client.post("/api/check", json={"invoice": invoice("printed_math_wrong").model_dump(mode="json")})
    assert r.json()["failures"] and r.json()["missing"] == []
    assert store.get("a").review is None


def test_processing_needs_an_api_key(client):
    assert client.post("/api/process", json={}).status_code == 400
    assert client.post("/api/upload", files=[("files", ("x.pdf", b"x"))]).status_code == 400


def test_approve_needs_an_amount_on_every_line_or_the_totals_would_fail(tmp_path):
    put(tmp_path, "a", status="flagged")
    edited = invoice("clean_invoice")
    edited.line_items[1].amount = None
    with pytest.raises(ValueError, match="line 2 amount"):
        store.review("a", "approve", edited, directory=tmp_path)
    with pytest.raises(ValueError, match="invoice_date"):
        store.review("a", "approve", invoice("clean_invoice", invoice_date="20130812"), directory=tmp_path)


def test_pipeline_save_keeps_a_review_made_while_the_job_ran(tmp_path):
    put(tmp_path, "a", status="flagged")
    loaded_by_job = store.load_all(tmp_path)[0]            # the job loads every document...
    store.review("a", "approve", directory=tmp_path)       # ...a reviewer approves one meanwhile...
    loaded_by_job.doc.vendor = "Jacobs Russell & Perry"    # ...and the job saves its changes
    store.save_pipeline_version(loaded_by_job.doc, tmp_path)
    stored = store.get("a", tmp_path)
    assert stored.review is not None and stored.review.action == "approve"
    assert stored.doc.vendor == "Jacobs Russell & Perry"
