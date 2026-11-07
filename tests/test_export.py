"""Tests for the CSV / Excel / bill-JSON exports."""
import io
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import database, export
from core.pipeline import run_pipeline
from core.workspace import new_session_workspace

SAMPLES = ROOT / "sample_invoices"


def run(ws, filename):
    return [i for i in run_pipeline(str(SAMPLES / filename), filename, ws=ws) if hasattr(i, "decision")][0]


def test_bill_payload_for_an_approved_invoice():
    ws = new_session_workspace()
    r = run(ws, "01_happy_path_nimbus.pdf")
    bill = export.erp_bill(database.get_run(r.invoice_id, ws))
    assert bill["bill_number"] == "INV-2026-0931"
    assert bill["vendor"]["id"] == "V-1001" and bill["vendor"]["name"] == "Nimbus Office Supplies Pvt Ltd"
    assert bill["purchase_order"] == "PO-5001" and bill["amounts"]["total"] == 48500.0
    assert bill["currency"] == "INR"
    assert bill["approval"]["approved_by"] == "rules engine"
    assert json.loads(export.bill_json_bytes(database.get_run(r.invoice_id, ws)))["bill_number"] == "INV-2026-0931"


def test_no_bill_for_a_rejected_or_pending_invoice():
    ws = new_session_workspace()
    run(ws, "02_edge_duplicate_coral.pdf")
    rejected = run(ws, "02_edge_duplicate_coral.pdf")
    pending = run(ws, "05_edge_unknown_vendor.pdf")
    assert export.erp_bill(database.get_run(rejected.invoice_id, ws)) is None
    assert export.erp_bill(database.get_run(pending.invoice_id, ws)) is None
    assert export.bill_json_bytes(database.get_run(pending.invoice_id, ws)) is None


def test_a_reviewer_approval_makes_a_bill_and_names_the_reviewer():
    ws = new_session_workspace()
    r = run(ws, "05_edge_unknown_vendor.pdf")
    database.record_review(r.invoice_id, "Approved", "Confirmed with procurement", ws)
    bill = export.erp_bill(database.get_run(r.invoice_id, ws))
    assert bill["approval"]["approved_by"] == "reviewer"
    assert bill["approval"]["system_decision"] == "Needs Review"
    assert bill["approval"]["reviewer_note"] == "Confirmed with procurement"


def test_csv_has_both_decisions_and_every_run():
    ws = new_session_workspace()
    run(ws, "01_happy_path_nimbus.pdf")
    r = run(ws, "05_edge_unknown_vendor.pdf")
    database.record_review(r.invoice_id, "Rejected", "wrong vendor", ws)
    df = pd.read_csv(io.BytesIO(export.csv_bytes(database.get_all_history(ws))))
    assert len(df) == 2
    row = df[df.filename == "05_edge_unknown_vendor.pdf"].iloc[0]
    assert row.system_decision == "Needs Review" and row.reviewer_decision == "Rejected"
    assert row.final_status == "Rejected"


def test_excel_workbook_has_audit_trail_and_approved_bills_sheets():
    ws = new_session_workspace()
    run(ws, "01_happy_path_nimbus.pdf")
    run(ws, "05_edge_unknown_vendor.pdf")
    xl = pd.ExcelFile(io.BytesIO(export.excel_bytes(database.get_all_history(ws))))
    assert xl.sheet_names == ["Audit trail", "Approved bills"]
    assert len(pd.read_excel(xl, "Audit trail")) == 2
    bills = pd.read_excel(xl, "Approved bills")
    assert len(bills) == 1 and bills.iloc[0]["purchase_order"] == "PO-5001"


def test_excel_with_no_approved_invoices_still_opens():
    ws = new_session_workspace()
    run(ws, "05_edge_unknown_vendor.pdf")
    xl = pd.ExcelFile(io.BytesIO(export.excel_bytes(database.get_all_history(ws))))
    assert len(pd.read_excel(xl, "Approved bills")) == 0


def test_a_dataframe_row_with_nan_cells_still_gives_valid_json():
    """The dashboard hands the bill builder a pandas row, where empty cells are NaN."""
    ws = new_session_workspace()
    r = run(ws, "01_happy_path_nimbus.pdf")
    df = pd.DataFrame(database.get_all_history(ws))
    assert df.human_decision.isna().all()
    raw = export.bill_json_bytes(df.iloc[0].to_dict()).decode()
    bill = json.loads(raw)                      # NaN would make this invalid JSON
    assert "NaN" not in raw and bill["approval"]["approved_by"] == "rules engine"
    assert bill["approval"]["reviewer_note"] is None
