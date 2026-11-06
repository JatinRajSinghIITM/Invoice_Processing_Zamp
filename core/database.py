"""
SQLite audit trail. Two tables:
  - processing_history: one row per invoice run (what the dashboard lists)
  - po_consumption: running ledger of how much of each PO has been invoiced
    so far by *non-rejected* invoices — this is what makes split-PO handling
    and duplicate-amount checks possible without re-scanning every invoice
    on every run.

The system's own decision (`decision_status`) is never overwritten. A human
reviewer's override is stored beside it (`human_decision`, `reviewer_note`,
`reviewed_at`), so the audit trail always shows both what the system decided
and what a person later decided. `effective_status` is the human decision if
there is one, otherwise the system's.

Every function takes an optional Workspace (see core/workspace.py); with none
it uses the repo's data/ folder, which is what the CLI and tests rely on.

SQLite (not Postgres/etc.) is the right call here: single-user demo app,
zero setup, the file itself IS the audit trail artifact you can hand someone.
"""
import sqlite3
import json
from datetime import datetime, timezone

from core.workspace import Workspace, DEFAULT_WORKSPACE

_NEW_COLUMNS = {
    "file_hash": "TEXT",
    "human_decision": "TEXT",
    "reviewer_note": "TEXT",
    "reviewed_at": "TEXT",
    "superseded": "INTEGER DEFAULT 0",
}


def get_connection(ws: Workspace | None = None):
    ws = ws or DEFAULT_WORKSPACE
    ws.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ws.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(ws: Workspace | None = None):
    conn = get_connection(ws)
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
    # Older databases (created before review/supersede existed) get the new
    # columns added in place rather than being thrown away.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(processing_history)")}
    for col, ddl in _NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE processing_history ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()


def reset_db(ws: Workspace | None = None):
    """Wipe history — used by the sample-data generator / tests, and by the
    UI's reset button (which only ever resets the visitor's OWN workspace)."""
    ws = ws or DEFAULT_WORKSPACE
    if ws.db_path.exists():
        ws.db_path.unlink()
    init_db(ws)


def save_result(result, ws: Workspace | None = None) -> None:
    conn = get_connection(ws)
    row = result.to_db_row()
    conn.execute("""
        INSERT OR REPLACE INTO processing_history
        (invoice_id, filename, processed_at, vendor_name, invoice_number, invoice_date,
         po_number, total, po_amount, decision_status, decision_reasons, confidence,
         full_result_json, file_hash, superseded)
        VALUES (:invoice_id, :filename, :processed_at, :vendor_name, :invoice_number, :invoice_date,
                :po_number, :total, :po_amount, :decision_status, :decision_reasons, :confidence,
                :full_json, :file_hash, 0)
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


def supersede_pending_runs(file_hash: str, ws: Workspace | None = None) -> list:
    """Re-submitting the exact same file while its earlier run is still
    waiting for review is a RE-RUN (typically after fixing the vendor or PO
    data), not a duplicate invoice. The earlier pending run is retired so it
    neither blocks the new run as a "duplicate" nor keeps eating PO balance.
    An earlier run that was Approved or Rejected is untouched — resubmitting
    that is a genuine duplicate and is caught as one."""
    if not file_hash:
        return []
    conn = get_connection(ws)
    rows = conn.execute("""
        SELECT invoice_id FROM processing_history
        WHERE file_hash = ? AND decision_status = 'Needs Review'
          AND human_decision IS NULL AND COALESCE(superseded, 0) = 0
    """, (file_hash,)).fetchall()
    ids = [r["invoice_id"] for r in rows]
    for invoice_id in ids:
        conn.execute("UPDATE processing_history SET superseded = 1 WHERE invoice_id = ?", (invoice_id,))
        conn.execute("DELETE FROM po_consumption WHERE invoice_id = ?", (invoice_id,))
    conn.commit()
    conn.close()
    return ids


def record_review(invoice_id: str, decision: str, note: str = "", ws: Workspace | None = None) -> None:
    """A human reviewer resolves a Needs Review invoice as Approved or Rejected.
    Rejecting releases the PO balance the invoice was holding."""
    if decision not in ("Approved", "Rejected"):
        raise ValueError("Reviewer decision must be 'Approved' or 'Rejected'.")
    conn = get_connection(ws)
    row = conn.execute(
        "SELECT decision_status, human_decision FROM processing_history WHERE invoice_id = ?",
        (invoice_id,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Unknown invoice {invoice_id}.")
    if row["decision_status"] != "Needs Review":
        conn.close()
        raise ValueError("Only invoices the system marked Needs Review can be resolved by a reviewer.")
    if row["human_decision"]:
        conn.close()
        raise ValueError("This invoice has already been resolved by a reviewer.")
    conn.execute("""
        UPDATE processing_history
        SET human_decision = ?, reviewer_note = ?, reviewed_at = ?
        WHERE invoice_id = ?
    """, (decision, (note or "").strip(), datetime.now(timezone.utc).isoformat(), invoice_id))
    if decision == "Rejected":
        conn.execute("DELETE FROM po_consumption WHERE invoice_id = ?", (invoice_id,))
    conn.commit()
    conn.close()


def get_po_consumed(po_number: str, exclude_invoice_id: str = None, ws: Workspace | None = None) -> float:
    conn = get_connection(ws)
    q = "SELECT COALESCE(SUM(amount), 0) as total FROM po_consumption WHERE po_number = ?"
    params = [po_number]
    if exclude_invoice_id:
        q += " AND invoice_id != ?"
        params.append(exclude_invoice_id)
    row = conn.execute(q, params).fetchone()
    conn.close()
    return row["total"] or 0.0


def find_potential_duplicates(vendor_name: str, invoice_number: str, total: float, invoice_date: str,
                              ws: Workspace | None = None):
    """Returns prior (non-superseded) history rows that look like the same invoice."""
    conn = get_connection(ws)
    rows = conn.execute("""
        SELECT *, COALESCE(human_decision, decision_status) AS effective_status
        FROM processing_history
        WHERE COALESCE(superseded, 0) = 0
          AND ((invoice_number = ? AND vendor_name = ?)
            OR (vendor_name = ? AND ABS(total - ?) < 0.01 AND invoice_date = ?))
    """, (invoice_number, vendor_name, vendor_name, total or -1, invoice_date)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run(invoice_id: str, ws: Workspace | None = None):
    """One history row (including any reviewer decision) or None."""
    conn = get_connection(ws)
    row = conn.execute("""
        SELECT *, COALESCE(human_decision, decision_status) AS effective_status
        FROM processing_history WHERE invoice_id = ?
    """, (invoice_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_history(ws: Workspace | None = None):
    conn = get_connection(ws)
    rows = conn.execute("""
        SELECT *, COALESCE(human_decision, decision_status) AS effective_status
        FROM processing_history
        WHERE COALESCE(superseded, 0) = 0
        ORDER BY processed_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _serialize(result) -> str:
    def default(o):
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)
    return json.dumps(result, default=default)
