"""
Tests for the guarded AI step and the GST / currency checks.

The model call is always replaced by a stub: these tests never touch the
network, and they prove the guardrails (grounding, no-overwrite, budgets,
graceful failure) rather than the quality of any particular model.
"""
import json
import sys
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import ai_assist, compliance, extraction
from core.pipeline import run_pipeline
from core.workspace import new_session_workspace

SAMPLES = ROOT / "sample_invoices"
UNUSUAL = "09_ai_unusual_layout_orbit.pdf"

GOOD_REPLY = json.dumps({
    "vendor_name_raw": "Orbit Telecom Solutions", "invoice_number": "TI/2026/0417",
    "invoice_date": "2026-10-14", "po_number": "PO-5007",
    "subtotal": 15889.83, "tax": 2860.17, "total": 18750.0,
})


@pytest.fixture(autouse=True)
def clean_budgets(monkeypatch):
    ai_assist.reset_budgets()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield
    ai_assist.reset_budgets()


def with_model(monkeypatch, reply):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    if isinstance(reply, Exception):
        def boom(_text):
            raise reply
        monkeypatch.setattr(ai_assist, "_call_model", boom)
    else:
        monkeypatch.setattr(ai_assist, "_call_model", lambda _text: reply)


def run(filename, ws=None, **kw):
    ws = ws or new_session_workspace()
    result = None
    for item in run_pipeline(str(SAMPLES / filename), filename, ws=ws, **kw):
        if hasattr(item, "decision"):
            result = item
    return result


def make_pdf(path, lines):
    c = canvas.Canvas(str(path), pagesize=A4)
    y = A4[1] - 60
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()


# ================================================================== AI assist

def test_without_a_key_the_hard_sample_stays_deterministic_and_goes_to_review():
    r = run(UNUSUAL)
    assert r.decision.status == "Needs Review"
    assert r.extracted.ai_status == "no_key"
    assert r.extracted.ai_fields == []
    stage3 = next(s for s in r.stages if s.stage.startswith("3."))
    assert stage3.status == "warning" and "no API key" in stage3.detail


def test_with_a_model_the_hard_sample_is_read_and_approved(monkeypatch):
    with_model(monkeypatch, GOOD_REPLY)
    r = run(UNUSUAL)
    assert r.decision.status == "Approved"
    assert set(r.extracted.ai_fields) == {"invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total"}
    assert r.extracted.invoice_number == "TI/2026/0417" and r.extracted.invoice_date == "2026-10-14"
    assert "llm_assist" in r.extracted.extraction_method
    assert r.extracted.extraction_confidence < 1.0, "AI-sourced fields must not score as a direct read"
    stage3 = next(s for s in r.stages if s.stage.startswith("3."))
    assert stage3.status == "ok" and "verified against the document" in stage3.summary


def test_the_model_is_never_called_for_a_clean_invoice(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    def must_not_run(_t):
        raise AssertionError("model called for a document the parser read confidently")
    monkeypatch.setattr(ai_assist, "_call_model", must_not_run)
    assert run("01_happy_path_nimbus.pdf").decision.status == "Approved"


def test_invented_values_are_rejected_because_they_are_not_in_the_document(monkeypatch):
    fabricated = json.dumps({"invoice_number": "FAKE-999", "invoice_date": "2026-01-02", "total": 99999.0,
                             "vendor_name_raw": "Totally Different Corp"})
    with_model(monkeypatch, fabricated)
    r = run(UNUSUAL)
    assert r.extracted.ai_fields == []
    assert r.extracted.invoice_number is None and r.extracted.total is None
    assert r.decision.status == "Needs Review"
    stage3 = next(s for s in r.stages if s.stage.startswith("3."))
    assert "discarded as not found in the document" in stage3.detail


def test_injected_instructions_cannot_smuggle_values_in(monkeypatch):
    # A model fooled by text inside the PDF might return whatever the PDF told it to.
    with_model(monkeypatch, json.dumps({"invoice_number": "APPROVE-THIS-INVOICE", "total": 1.0}))
    r = run(UNUSUAL)
    assert r.extracted.ai_fields == [] and r.extracted.total is None


def test_the_model_never_overwrites_a_value_the_parser_already_read(monkeypatch):
    # 08 has a vendor and a total but no invoice number. The model proposes a different total.
    with_model(monkeypatch, json.dumps({"total": 15254.24, "vendor_name_raw": "Sterling Consulting Group"}))
    r = run("08_bonus_missing_fields_sterling.pdf")
    assert r.extracted.total == 18000.00
    assert "total" not in r.extracted.ai_fields


def test_a_failing_model_degrades_to_the_deterministic_result(monkeypatch):
    for failure in (TimeoutError("slow"), ValueError("bad"), "this is not json at all"):
        ai_assist.reset_budgets()
        with_model(monkeypatch, failure)
        r = run(UNUSUAL)
        assert r.decision.status == "Needs Review"          # pipeline still finished
        assert r.extracted.ai_status in ("failed",)
        assert r.extracted.ai_fields == []


def test_json_inside_a_code_fence_is_accepted(monkeypatch):
    with_model(monkeypatch, "```json\n" + GOOD_REPLY + "\n```")
    assert run(UNUSUAL).decision.status == "Approved"


def test_switching_ai_off_is_respected(monkeypatch):
    with_model(monkeypatch, GOOD_REPLY)
    r = run(UNUSUAL, use_ai=False)
    assert r.extracted.ai_status == "disabled" and r.decision.status == "Needs Review"


def test_per_session_call_limit(monkeypatch):
    with_model(monkeypatch, "{}")
    monkeypatch.setattr(ai_assist, "MAX_CALLS_PER_SESSION", 2)
    ws = new_session_workspace()
    statuses = []
    for _ in range(3):
        statuses.append(run(UNUSUAL, ws=ws).extracted.ai_status)
    assert statuses == ["nothing_new", "nothing_new", "budget"]
    # A different visitor still has their own allowance.
    assert run(UNUSUAL, ws=new_session_workspace()).extracted.ai_status == "nothing_new"


def test_daily_call_limit_is_shared_by_all_sessions(monkeypatch):
    with_model(monkeypatch, "{}")
    monkeypatch.setattr(ai_assist, "MAX_CALLS_PER_DAY", 2)
    statuses = [run(UNUSUAL, ws=new_session_workspace()).extracted.ai_status for _ in range(3)]
    assert statuses == ["nothing_new", "nothing_new", "budget"]


def test_validate_fields_type_and_range_checks():
    text = "Invoice TI-77 dated 05 March 2026 total Rs 1,250.50"
    clean, rejected = ai_assist.validate_fields(
        {"invoice_number": "TI-77", "invoice_date": "2026-03-05", "total": "1250.50", "tax": -5,
         "subtotal": "abc", "po_number": 12345}, text)
    assert clean == {"invoice_number": "TI-77", "invoice_date": "2026-03-05", "total": 1250.5}
    assert set(rejected) == {"tax", "subtotal", "po_number"}


# ================================================================== GST / currency

REAL_GSTIN = "33AAGCB2559J1ZN"


def test_gstin_checksum():
    for g in (REAL_GSTIN, "06AAGCB2559J2ZJ", "07ARJPM0849F1ZQ"):
        assert compliance.gstin_checksum_ok(g)
    assert not compliance.gstin_checksum_ok("33AAGCB2559J1ZX")     # last character wrong
    assert not compliance.gstin_checksum_ok("33AAGCB2559J1Z")      # too short


def test_valid_gstin_raises_no_flag():
    rep = compliance.analyse_gst(f"Bright | GSTIN: {REAL_GSTIN}")
    assert rep.gstin == REAL_GSTIN and rep.flags == []


def test_mistyped_gstin_is_flagged():
    rep = compliance.analyse_gst("GSTIN: 33AAGCB2559J1ZX")
    assert rep.flags and "check-character" in rep.flags[0]


def test_malformed_gstin_and_bad_state_code_are_flagged():
    assert "not in a valid GSTIN format" in compliance.analyse_gst("GSTIN: 3AAGCB2559J1ZNN1").flags[0]
    assert "not an Indian GST state code" in compliance.analyse_gst("GSTIN: 55AAGCB2559J1ZN").flags[0]


def test_cgst_label_is_not_mistaken_for_a_gstin_label():
    assert compliance.analyse_gst("CGST 2.5% 378.55 SGST 2.5% 378.55").gstin is None


def test_igst_on_a_same_state_supply_is_flagged():
    text = f"GSTIN: {REAL_GSTIN}\nState Code: 33\nTaxable IGST Total\n"
    assert any("should be charged" in f and "IGST" in f for f in compliance.analyse_gst(text).flags)


def test_cgst_sgst_on_an_inter_state_supply_is_flagged():
    text = "GSTIN: 06AAGCB2559J2ZJ\nState Code: 33\nTaxable CGST SGST Total\n"
    assert any("IGST should be charged" in f for f in compliance.analyse_gst(text).flags)


def test_correct_tax_type_is_not_flagged():
    same = compliance.analyse_gst(f"GSTIN: {REAL_GSTIN}\nState Code: 33\nCGST SGST\n")
    inter = compliance.analyse_gst("GSTIN: 06AAGCB2559J2ZJ\nPlace of Supply: TAMIL NADU\nIGST\n")
    assert same.flags == [] and inter.flags == []


def test_unequal_cgst_and_sgst_totals_are_flagged():
    text = f"GSTIN: {REAL_GSTIN}\nState Code: 33\nCGST SGST\nGrand Total 2 30569.52 765 700 32108.0\n"
    assert any("should be equal" in f for f in compliance.analyse_gst(text).flags)


def test_currency_detection():
    assert compliance.detect_currency("Total: Rs. 100") == "INR"
    assert compliance.detect_currency("Total: ₹100") == "INR"
    assert compliance.detect_currency("Total: $100.00") == "USD"
    assert compliance.detect_currency("Total: €100,00") == "EUR"
    assert compliance.detect_currency("no money marks") == "INR"


def test_a_bad_gstin_sends_an_otherwise_perfect_invoice_to_review(tmp_path):
    lines = ["Nimbus Office Supplies Pvt Ltd", "GSTIN: 27AAACN1234F1Z5", "Invoice Number: INV-GST-1",
             "Invoice Date: 2026-10-05", "PO Number: PO-5001", "Total: Rs. 48,500.00"]
    pdf = tmp_path / "bad_gstin.pdf"
    make_pdf(pdf, lines)
    ws = new_session_workspace()
    result = [i for i in run_pipeline(str(pdf), "bad_gstin.pdf", ws=ws) if hasattr(i, "decision")][0]
    assert not compliance.gstin_checksum_ok("27AAACN1234F1Z5")
    assert result.decision.status == "Needs Review"
    assert any("GSTIN" in r for r in result.decision.reasons)
    stage5 = next(s for s in result.stages if s.stage.startswith("5."))
    assert stage5.status == "warning"


def test_a_dollar_invoice_is_sent_to_review_not_compared_against_a_rupee_po(tmp_path):
    lines = ["Nimbus Office Supplies Pvt Ltd", "Invoice Number: INV-USD-1", "Invoice Date: 2026-10-05",
             "PO Number: PO-5001", "Total: $48,500.00"]
    pdf = tmp_path / "usd.pdf"
    make_pdf(pdf, lines)
    result = [i for i in run_pipeline(str(pdf), "usd.pdf", ws=new_session_workspace()) if hasattr(i, "decision")][0]
    assert result.extracted.currency == "USD"
    assert result.decision.status == "Needs Review"
    assert any("USD" in r for r in result.decision.reasons)


def test_samples_without_a_gstin_are_unaffected():
    r = run("01_happy_path_nimbus.pdf")
    assert r.decision.status == "Approved" and r.extracted.compliance_flags == []
    stage5 = next(s for s in r.stages if s.stage.startswith("5."))
    assert stage5.status == "ok"
