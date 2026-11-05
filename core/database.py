"""
SQLite audit trail. Two tables:
  - processing_history: one row per invoice run (what the dashboard lists)
  - po_consumption: running ledger of how much of each PO has been invoiced
    so far by *non-rejected* invoices — this is what makes split-PO handling
    and duplicate-amount checks possible without re-scanning every invoice
    on every run.

SQLite (not Postgres/etc.) is the right call here: single-user demo app,
zero setup, the file itself IS the audit trail artifact you can hand someone.
"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent.parent / "data" / "processing_history.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processing_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id TEXT UNIQUE,
            filename TEXT,
            processed_at TEXT,
            vendor_name TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            po_number TEXT,
            total REAL,
            po_amount REAL,
            decision_status TEXT,
            decision_reasons TEXT,
            confidence REAL,
            full_result_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS po_consumption (
            po_number TEXT,
            invoice_id TEXT,
            amount REAL,
            decision_status TEXT,
            recorded_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def reset_db():
    """Wipe history — used by the sample-data generator / tests so repeated
    runs start clean. Never called from the Streamlit UI itself."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


def save_result(result) -> None:
    conn = get_connection()
    row = result.to_db_row()
    conn.execute("""
        INSERT OR REPLACE INTO processing_history
        (invoice_id, filename, processed_at, vendor_name, invoice_number, invoice_date,
         po_number, total, po_amount, decision_status, decision_reasons, confidence, full_result_json)
        VALUES (:invoice_id, :filename, :processed_at, :vendor_name, :invoice_number, :invoice_date,
                :po_number, :total, :po_amount, :decision_status, :decision_reasons, :confidence, :full_json)
    """, {**row, "invoice_date": result.extracted.invoice_date, "full_json": _serialize(result)})

    # Only non-rejected invoices consume PO balance — a rejected duplicate/fraud
    # attempt should never eat into the real PO's remaining amount.
    if result.po_match.matched and result.decision.status != "Rejected":
        conn.execute("""
            INSERT INTO po_consumption (po_number, invoice_id, amount, decision_status, recorded_at)
            VALUES (?, ?, ?, ?, ?)
        """, (result.po_match.po_number, result.invoice_id, result.extracted.total or 0,
              result.decision.status, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def get_po_consumed(po_number: str, exclude_invoice_id: str = None) -> float:
    conn = get_connection()
    q = "SELECT COALESCE(SUM(amount), 0) as total FROM po_consumption WHERE po_number = ?"
    params = [po_number]
    if exclude_invoice_id:
        q += " AND invoice_id != ?"
        params.append(exclude_invoice_id)
    row = conn.execute(q, params).fetchone()
    conn.close()
    return row["total"] or 0.0


def find_potential_duplicates(vendor_name: str, invoice_number: str, total: float, invoice_date: str):
    """Returns prior history rows that look like the same invoice."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM processing_history
        WHERE (invoice_number = ? AND vendor_name = ?)
           OR (vendor_name = ? AND ABS(total - ?) < 0.01 AND invoice_date = ?)
    """, (invoice_number, vendor_name, vendor_name, total or -1, invoice_date)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_history():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM processing_history ORDER BY processed_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _serialize(result) -> str:
    def default(o):
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)
    return json.dumps(result, default=default)
