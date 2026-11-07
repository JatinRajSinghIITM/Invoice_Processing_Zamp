"""Tests for the REST API (skipped automatically if FastAPI is not installed)."""
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import api
from core import ai_assist, notify

SAMPLES = ROOT / "sample_invoices"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    ai_assist.reset_budgets()
    notify.reset_counter()
    c = TestClient(api.app)
    c.headers.update({"X-Client-Id": uuid.uuid4().hex})     # a fresh private workspace per test
    return c


def post_pdf(client, name, **kw):
    with open(SAMPLES / name, "rb") as f:
        return client.post("/v1/invoices", files={"file": (name, f, "application/pdf")}, **kw)


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_happy_path_returns_decision_reasons_stages_and_a_bill(client):
    r = post_pdf(client, "01_happy_path_nimbus.pdf")
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["status"] == "Approved" and body["decision"]["reasons"]
    assert body["extracted"]["total"] == 48500.0 and body["extracted"]["invoice_number"] == "INV-2026-0931"
    assert len(body["stages"]) == 11
    assert body["bill"]["purchase_order"] == "PO-5001" and body["bill"]["approval"]["approved_by"] == "rules engine"
    assert "file_hash" not in body


def test_duplicate_is_rejected_on_the_second_post(client):
    assert post_pdf(client, "02_edge_duplicate_coral.pdf").json()["decision"]["status"] == "Approved"
    second = post_pdf(client, "02_edge_duplicate_coral.pdf").json()
    assert second["decision"]["status"] == "Rejected" and second["bill"] is None


def test_clients_are_isolated(client):
    other = TestClient(api.app)
    other.headers.update({"X-Client-Id": uuid.uuid4().hex})
    assert post_pdf(client, "02_edge_duplicate_coral.pdf").json()["decision"]["status"] == "Approved"
    assert post_pdf(other, "02_edge_duplicate_coral.pdf").json()["decision"]["status"] == "Approved"
    assert len(client.get("/v1/invoices").json()) == 1


def test_list_and_get(client):
    first = post_pdf(client, "01_happy_path_nimbus.pdf").json()
    rows = client.get("/v1/invoices").json()
    assert [r["invoice_id"] for r in rows] == [first["invoice_id"]]
    one = client.get(f"/v1/invoices/{first['invoice_id']}").json()
    assert one["effective_status"] == "Approved" and one["full_result"]["decision"]["status"] == "Approved"
    assert client.get("/v1/invoices/INV-RUN-nope").status_code == 404


def test_review_flow_over_the_api(client):
    pending = post_pdf(client, "05_edge_unknown_vendor.pdf").json()
    assert pending["decision"]["status"] == "Needs Review" and pending["bill"] is None
    r = client.post(f"/v1/invoices/{pending['invoice_id']}/review",
                    json={"decision": "Approved", "note": "Confirmed with procurement"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision_status"] == "Needs Review" and body["human_decision"] == "Approved"
    assert body["bill"]["approval"]["approved_by"] == "reviewer"
    # resolving twice is a conflict; a bad decision value is a validation error
    assert client.post(f"/v1/invoices/{pending['invoice_id']}/review", json={"decision": "Rejected"}).status_code == 409
    assert client.post(f"/v1/invoices/{pending['invoice_id']}/review", json={"decision": "Maybe"}).status_code == 422


def test_bring_your_own_vendor_and_po_then_reprocess(client):
    first = post_pdf(client, "05_edge_unknown_vendor.pdf").json()
    name, total = first["extracted"]["vendor_name_raw"], first["extracted"]["total"]
    v = client.post("/v1/vendors", json={"name": name})
    assert v.status_code == 201
    po = client.post("/v1/purchase-orders", json={"po_number": "PO-9001", "vendor_id": v.json()["vendor_id"], "amount": total})
    assert po.status_code == 201
    assert post_pdf(client, "05_edge_unknown_vendor.pdf").json()["decision"]["status"] == "Approved"
    assert client.post("/v1/vendors", json={"name": name}).status_code == 409          # duplicate
    assert client.post("/v1/purchase-orders", json={"po_number": "PO-1", "vendor_id": "V-NOPE", "amount": 5}).status_code == 409
    assert client.post("/v1/purchase-orders", json={"po_number": "PO-2", "vendor_id": "V-1001", "amount": -5}).status_code == 422


def test_non_pdf_and_oversize_uploads_are_refused(client):
    r = client.post("/v1/invoices", files={"file": ("x.pdf", b"this is not a pdf", "application/pdf")})
    assert r.status_code == 415
    big = b"%PDF" + b"0" * (api.MAX_UPLOAD_BYTES + 10)
    assert client.post("/v1/invoices", files={"file": ("big.pdf", big, "application/pdf")}).status_code == 413


def test_a_corrupt_pdf_is_a_clean_422_not_a_crash(client):
    r = client.post("/v1/invoices", files={"file": ("broken.pdf", b"%PDF-1.4 garbage garbage", "application/pdf")})
    assert r.status_code == 422


def test_api_key_is_enforced_when_set(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    assert client.get("/v1/invoices").status_code == 401
    assert client.get("/v1/invoices", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/invoices", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200                       # health stays open


def test_client_file_name_is_never_used_as_a_path(client):
    with open(SAMPLES / "01_happy_path_nimbus.pdf", "rb") as f:
        r = client.post("/v1/invoices", files={"file": ("../../etc/evil.pdf", f, "application/pdf")})
    assert r.status_code == 200 and r.json()["filename"] == "evil.pdf"


def test_use_ai_flag_is_passed_through(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(ai_assist, "_call_model", lambda t: "{}")
    off = post_pdf(client, "09_ai_unusual_layout_orbit.pdf", params={"use_ai": "false"}).json()
    assert off["extracted"]["ai_status"] == "disabled"
