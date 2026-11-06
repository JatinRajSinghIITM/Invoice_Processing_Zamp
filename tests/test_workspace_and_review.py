"""
Tests for per-visitor workspaces, bring-your-own vendor/PO data, re-runs of
a pending invoice, and human review overrides.

Every test uses its own fresh session workspace, so none of them touch the
shared data/ folder or each other.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import database, matching
from core.pipeline import run_pipeline
from core.workspace import new_session_workspace, Workspace

SAMPLES = ROOT / "sample_invoices"


@pytest.fixture
def ws():
    w = new_session_workspace()
    database.init_db(w)
    return w


def run(ws, filename):
    result = None
    for item in run_pipeline(str(SAMPLES / filename), filename, ws=ws):
        if hasattr(item, "decision"):
            result = item
    return result


# ------------------------------------------------------------- isolation

def test_two_visitors_do_not_see_each_others_runs():
    a, b = new_session_workspace(), new_session_workspace()
    assert run(a, "02_edge_duplicate_coral.pdf").decision.status == "Approved"
    # Visitor B's FIRST run of the same sample must also be Approved. With one
    # shared database it would be rejected as a duplicate of A's run.
    assert run(b, "02_edge_duplicate_coral.pdf").decision.status == "Approved"
    assert len(database.get_all_history(a)) == 1
    assert len(database.get_all_history(b)) == 1


def test_resetting_one_workspace_leaves_another_untouched():
    a, b = new_session_workspace(), new_session_workspace()
    run(a, "01_happy_path_nimbus.pdf")
    run(b, "01_happy_path_nimbus.pdf")
    database.reset_db(a)
    assert database.get_all_history(a) == []
    assert len(database.get_all_history(b)) == 1


def test_reference_data_edits_are_private_to_the_workspace():
    a, b = new_session_workspace(), new_session_workspace()
    matching.add_vendor("Only In A Traders", ws=a)
    assert "Only In A Traders" in matching.load_vendors(a)["vendor_name"].tolist()
    assert "Only In A Traders" not in matching.load_vendors(b)["vendor_name"].tolist()


# ------------------------------------------------- bring-your-own vendor / PO

def test_adding_vendor_and_po_turns_unknown_vendor_into_approved(ws):
    first = run(ws, "05_edge_unknown_vendor.pdf")
    assert first.decision.status == "Needs Review"

    vendor_id = matching.add_vendor(first.extracted.vendor_name_raw, ws=ws)
    matching.add_purchase_order("PO-9001", vendor_id, first.extracted.total, ws=ws)

    # Same file again: a re-run of something still pending, NOT a duplicate.
    second = run(ws, "05_edge_unknown_vendor.pdf")
    assert second.decision.status == "Approved"
    assert second.po_match.po_number == "PO-9001"
    history = database.get_all_history(ws)
    assert len(history) == 1, "the earlier pending run should be retired, not listed twice"
    assert history[0]["invoice_id"] == second.invoice_id


def test_resubmitting_an_approved_file_is_still_a_duplicate(ws):
    assert run(ws, "01_happy_path_nimbus.pdf").decision.status == "Approved"
    again = run(ws, "01_happy_path_nimbus.pdf")
    assert again.decision.status == "Rejected"


def test_add_vendor_and_po_validation(ws):
    with pytest.raises(ValueError):
        matching.add_vendor("   ", ws=ws)
    with pytest.raises(ValueError):
        matching.add_vendor("nimbus office supplies pvt ltd", ws=ws)      # case-insensitive duplicate
    vid = matching.add_vendor("Fresh Vendor Co", ws=ws)
    assert vid.startswith("V-")
    with pytest.raises(ValueError):
        matching.add_purchase_order("PO-5001", vid, 100, ws=ws)           # PO number taken
    with pytest.raises(ValueError):
        matching.add_purchase_order("PO-9100", "V-NOPE", 100, ws=ws)      # unknown vendor
    with pytest.raises(ValueError):
        matching.add_purchase_order("PO-9100", vid, -5, ws=ws)            # non-positive amount
    with pytest.raises(ValueError):
        matching.add_purchase_order("", vid, 100, ws=ws)


# ------------------------------------------------------------ review queue

def test_reviewer_can_approve_a_needs_review_invoice(ws):
    r = run(ws, "05_edge_unknown_vendor.pdf")
    assert r.decision.status == "Needs Review"
    database.record_review(r.invoice_id, "Approved", "Confirmed new vendor with procurement", ws)
    row = database.get_all_history(ws)[0]
    assert row["decision_status"] == "Needs Review"      # the system's own call is preserved
    assert row["human_decision"] == "Approved"
    assert row["effective_status"] == "Approved"
    assert row["reviewer_note"] == "Confirmed new vendor with procurement"
    assert row["reviewed_at"]


def test_rejecting_in_review_releases_the_po_balance(ws):
    r = run(ws, "03_edge_amount_mismatch_vertex.pdf")
    assert r.decision.status == "Needs Review"
    po = r.po_match.po_number
    assert database.get_po_consumed(po, ws=ws) == pytest.approx(r.extracted.total)
    database.record_review(r.invoice_id, "Rejected", "Overbilled", ws)
    assert database.get_po_consumed(po, ws=ws) == 0


def test_review_guards(ws):
    approved = run(ws, "01_happy_path_nimbus.pdf")
    with pytest.raises(ValueError):                       # only Needs Review can be reviewed
        database.record_review(approved.invoice_id, "Rejected", "", ws)
    pending = run(ws, "05_edge_unknown_vendor.pdf")
    with pytest.raises(ValueError):                       # only two valid outcomes
        database.record_review(pending.invoice_id, "Maybe", "", ws)
    with pytest.raises(ValueError):                       # unknown invoice
        database.record_review("INV-RUN-nope", "Approved", "", ws)
    database.record_review(pending.invoice_id, "Approved", "", ws)
    with pytest.raises(ValueError):                       # cannot be resolved twice
        database.record_review(pending.invoice_id, "Rejected", "", ws)


def test_human_rejected_invoice_does_not_block_a_resubmission(ws):
    r = run(ws, "05_edge_unknown_vendor.pdf")
    database.record_review(r.invoice_id, "Rejected", "wrong vendor", ws)
    # It was human-rejected, so submitting it again is not a duplicate of a live invoice.
    again = run(ws, "05_edge_unknown_vendor.pdf")
    assert not again.duplicate_check.is_duplicate


# ------------------------------------------------------------- migration

def test_old_database_without_review_columns_is_migrated(tmp_path):
    ws = Workspace(tmp_path)
    conn = sqlite3.connect(ws.db_path)
    conn.execute("""CREATE TABLE processing_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT UNIQUE, filename TEXT, processed_at TEXT,
        vendor_name TEXT, invoice_number TEXT, invoice_date TEXT, po_number TEXT, total REAL,
        po_amount REAL, decision_status TEXT, decision_reasons TEXT, confidence REAL, full_result_json TEXT)""")
    conn.execute("INSERT INTO processing_history (invoice_id, decision_status) VALUES ('OLD-1', 'Approved')")
    conn.commit()
    conn.close()

    database.init_db(ws)
    rows = database.get_all_history(ws)
    assert rows[0]["invoice_id"] == "OLD-1"
    assert rows[0]["effective_status"] == "Approved"
