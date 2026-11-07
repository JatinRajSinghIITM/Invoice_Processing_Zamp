"""
Getting results OUT of the system: the audit trail as CSV / Excel, and each
approved invoice as an accounting-system-style "bill" JSON.

The bill payload is the hand-off a real deployment would POST to an ERP or
accounting tool (Zoho Books, Tally, QuickBooks...) once an invoice is approved.
It is built only from fields this system actually extracted and decided, and
it carries who approved it (the rules engine or a named reviewer's decision)
so the receiving system keeps the audit trail. No real system is called here;
connecting one is just an HTTP call away from this payload.
"""
import io
import json

import pandas as pd

HISTORY_COLUMNS = {
    "invoice_id": "invoice_id", "filename": "filename", "processed_at": "processed_at",
    "vendor_name": "vendor", "invoice_number": "invoice_number", "invoice_date": "invoice_date",
    "po_number": "po_number", "total": "total", "po_amount": "po_amount",
    "decision_status": "system_decision", "human_decision": "reviewer_decision",
    "reviewer_note": "reviewer_note", "reviewed_at": "reviewed_at",
    "effective_status": "final_status", "confidence": "confidence", "decision_reasons": "reasons",
}


def history_frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in HISTORY_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return (df[list(HISTORY_COLUMNS)].rename(columns=HISTORY_COLUMNS)
            .sort_values("processed_at", ascending=False).reset_index(drop=True))


def csv_bytes(rows: list) -> bytes:
    return history_frame(rows).to_csv(index=False).encode("utf-8")


def _clean(value):
    """None for a missing value, including the NaN pandas puts in empty cells."""
    return None if value is None or (isinstance(value, float) and value != value) else value


def erp_bill(row: dict) -> dict | None:
    """The bill payload for one history row, or None unless its FINAL status
    (reviewer's decision if there is one, else the system's) is Approved."""
    row = {k: _clean(v) for k, v in row.items()}
    final = row.get("effective_status") or row.get("decision_status")
    if final != "Approved":
        return None
    try:
        full = json.loads(row.get("full_result_json") or "{}")
    except (TypeError, ValueError):
        full = {}
    ex = full.get("extracted") or {}
    vendor = full.get("vendor_match") or {}
    po = full.get("po_match") or {}
    by_reviewer = bool(row.get("human_decision"))
    return {
        "schema": "invoice-bill/v1",
        "source": {"invoice_id": row.get("invoice_id"), "filename": row.get("filename"),
                   "processed_at": row.get("processed_at")},
        "bill_number": ex.get("invoice_number") or row.get("invoice_number"),
        "bill_date": ex.get("invoice_date") or row.get("invoice_date"),
        "vendor": {"id": vendor.get("vendor_id"),
                   "name": vendor.get("matched_name") or row.get("vendor_name"),
                   "gstin": ex.get("gstin")},
        "purchase_order": po.get("po_number") or row.get("po_number"),
        "currency": ex.get("currency") or "INR",
        "amounts": {"subtotal": ex.get("subtotal"), "tax": ex.get("tax"),
                    "total": ex.get("total") if ex.get("total") is not None else row.get("total")},
        "approval": {
            "status": "Approved",
            "approved_by": "reviewer" if by_reviewer else "rules engine",
            "system_decision": row.get("decision_status"),
            "reviewer_note": row.get("reviewer_note") or None,
            "reviewed_at": row.get("reviewed_at") or None,
            "reasons": [r for r in str(row.get("decision_reasons") or "").split(" | ") if r],
        },
        "extraction": {"method": ex.get("extraction_method"), "confidence": ex.get("extraction_confidence"),
                       "ai_assisted_fields": ex.get("ai_fields") or []},
    }


def bill_json_bytes(row: dict) -> bytes | None:
    bill = erp_bill(row)
    return None if bill is None else json.dumps(bill, indent=2, ensure_ascii=False).encode("utf-8")


def excel_bytes(rows: list) -> bytes:
    """Two sheets: the full audit trail, and the approved bills ready to post."""
    bills = [b for b in (erp_bill(r) for r in rows) if b]
    bill_rows = [{
        "invoice_id": b["source"]["invoice_id"], "bill_number": b["bill_number"], "bill_date": b["bill_date"],
        "vendor_id": b["vendor"]["id"], "vendor": b["vendor"]["name"], "gstin": b["vendor"]["gstin"],
        "purchase_order": b["purchase_order"], "currency": b["currency"],
        "subtotal": b["amounts"]["subtotal"], "tax": b["amounts"]["tax"], "total": b["amounts"]["total"],
        "approved_by": b["approval"]["approved_by"],
    } for b in bills]
    bill_cols = ["invoice_id", "bill_number", "bill_date", "vendor_id", "vendor", "gstin", "purchase_order",
                 "currency", "subtotal", "tax", "total", "approved_by"]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        history_frame(rows).to_excel(xl, sheet_name="Audit trail", index=False)
        pd.DataFrame(bill_rows, columns=bill_cols).to_excel(xl, sheet_name="Approved bills", index=False)
        for ws in xl.book.worksheets:
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
    return buf.getvalue()
