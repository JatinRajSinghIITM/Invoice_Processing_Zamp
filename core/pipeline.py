"""
Orchestrates stages 1-11 end-to-end and yields a StageResult after each one.

Written as a generator (not a single function returning the final result) so
the Streamlit UI can render the "live run view" — each stage appearing as it
executes — by simply iterating this generator and rendering each yielded
StageResult as it arrives, instead of faking a progress bar around a black box.
"""
import hashlib
import uuid
from datetime import datetime, timezone

from core.models import StageResult, ProcessingResult
from core import extraction, matching, rules, database, ai_assist, notify
from core.workspace import DEFAULT_WORKSPACE


def _file_hash(pdf_path: str) -> str:
    with open(pdf_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_pipeline(pdf_path: str, filename: str, ws=None, use_ai: bool = True):
    """Generator: yields StageResult objects as each stage completes, and
    finally yields the ProcessingResult as the last item. `ws` is the
    visitor's private Workspace (None = the repo's shared data/ folder);
    `use_ai=False` keeps this run fully deterministic."""
    ws = ws or DEFAULT_WORKSPACE
    database.init_db(ws)
    invoice_id = f"INV-RUN-{uuid.uuid4().hex[:8]}"
    stages = []
    file_hash = _file_hash(pdf_path)
    # Same file already waiting for review = this is a re-run, not a duplicate.
    replaced = database.supersede_pending_runs(file_hash, ws)

    def emit(stage, status, summary, detail=""):
        sr = StageResult(stage=stage, status=status, summary=summary, detail=detail)
        stages.append(sr)
        return sr

    yield emit("1. Accept invoice", "ok", f"Received '{filename}'",
               f"Re-run of the same file: replaces earlier run {', '.join(replaced)}, which was still awaiting review"
               if replaced else "")

    # Stage 2+3: text extraction + field parsing (+ optional LLM assist for low confidence)
    raw_text, method = extraction.extract_raw_text(pdf_path)
    yield emit("2. Extract document text", "ok" if method == "digital_text" else "warning",
               f"Read via {'digital text layer' if method == 'digital_text' else 'OCR (scanned document detected)'}",
               f"{len(raw_text)} characters recovered")

    extracted = extraction.parse_fields(raw_text, method)
    pre_ai_confidence = extracted.extraction_confidence
    ai = ai_assist.try_assist(extracted, raw_text, session_key=str(ws.root), enabled=use_ai)
    detail = _field_summary(extracted)
    if extracted.notes:
        detail += " | Note: " + " ".join(extracted.notes)
    if ai.status == "used":
        yield emit("3. Parse structured fields", "ok",
                   f"Deterministic parse was low-confidence ({pre_ai_confidence:.0%}); AI assist read "
                   f"{len(ai.filled)} field(s), each verified against the document text. "
                   f"Confidence now {extracted.extraction_confidence:.0%}",
                   detail + " | " + ai.message)
    else:
        status = "ok" if extracted.extraction_confidence >= 0.6 and not extracted.notes else "warning"
        if ai.triggered:
            detail += " | " + ai.message
            status = "warning"
        yield emit("3. Parse structured fields", status,
                   f"Extracted fields deterministically (confidence {extracted.extraction_confidence:.0%})",
                   detail)

    missing = extracted.missing_fields()
    if missing:
        yield emit("4. Field completeness check", "warning",
                    f"Missing: {', '.join(missing)}", "Will route to Needs Review unless resolved elsewhere")
    else:
        yield emit("4. Field completeness check", "ok", "All critical fields present")

    # Stage 5: GST and currency compliance (deterministic; see core/compliance.py)
    if extracted.compliance_flags:
        yield emit("5. GST & currency checks", "warning",
                   f"{len(extracted.compliance_flags)} compliance issue(s) found",
                   " | ".join(extracted.compliance_flags))
    elif extracted.compliance_notes:
        yield emit("5. GST & currency checks", "ok", "No compliance issues found",
                   " | ".join(extracted.compliance_notes))
    else:
        yield emit("5. GST & currency checks", "ok", "No GSTIN printed on this invoice; nothing to verify",
                   f"Currency: {extracted.currency}")

    # Stage 6: vendor validation
    vendor_match = matching.match_vendor(extracted.vendor_name_raw, ws=ws)
    if vendor_match.matched and vendor_match.is_approved:
        yield emit("6. Validate vendor", "ok",
                    f"Matched '{extracted.vendor_name_raw}' → {vendor_match.matched_name} (approved, {vendor_match.match_score:.0f}% match)")
    elif vendor_match.matched and not vendor_match.is_approved:
        yield emit("6. Validate vendor", "fail",
                    f"Matched {vendor_match.matched_name} but vendor is INACTIVE / not approved")
    else:
        yield emit("6. Validate vendor", "warning",
                    f"No approved vendor match found for '{extracted.vendor_name_raw}'")

    # Stage 7: PO matching
    po_match = matching.match_purchase_order(extracted.po_number, vendor_match, extracted.total,
                                              exclude_invoice_id=invoice_id, ws=ws)
    if po_match.matched:
        detail = f"Matched via {po_match.match_method} ({po_match.match_score:.0f}%)"
        if po_match.previously_invoiced_amount > 0:
            detail += f" | ₹{po_match.previously_invoiced_amount:,.2f} already invoiced against this PO — treating as split PO"
        yield emit("7. Match purchase order", "ok",
                    f"Matched to {po_match.po_number} (₹{po_match.po_amount:,.2f})", detail)
    else:
        yield emit("7. Match purchase order", "warning",
                    f"No PO match found for reference '{extracted.po_number or '(none extracted)'}'")

    # Stage 8: financial validation
    severity, msg = rules.validate_amount(extracted, po_match)
    yield emit("8. Validate financials", {"ok": "ok", "review": "warning", "reject": "fail"}[severity], msg)

    # Stage 9: duplicate detection
    dup_check = rules.check_duplicate(extracted, invoice_id, ws=ws)
    if dup_check.is_duplicate:
        yield emit("9. Duplicate detection", "fail", "Duplicate invoice detected", dup_check.reason)
    elif dup_check.possible:
        yield emit("9. Duplicate detection", "warning", "Possible duplicate - needs a person to confirm", dup_check.reason)
    else:
        yield emit("9. Duplicate detection", "ok", "No duplicate found in processing history")

    # Stage 10: decision
    decision = rules.decide(extracted, vendor_match, po_match, dup_check)
    status_icon = {"Approved": "ok", "Needs Review": "warning", "Rejected": "fail"}[decision.status]
    yield emit("10. Final decision", status_icon, decision.status, " | ".join(decision.reasons))

    # Stage 11: persist audit trail
    result = ProcessingResult(
        invoice_id=invoice_id,
        filename=filename,
        processed_at=datetime.now(timezone.utc).isoformat(),
        extracted=extracted,
        vendor_match=vendor_match,
        po_match=po_match,
        duplicate_check=dup_check,
        decision=decision,
        stages=stages,
        file_hash=file_hash,
    )
    database.save_result(result, ws)
    alerted = notify.alert_needs_review(result)
    yield emit("11. Store audit trail", "ok", f"Saved as {invoice_id}",
               "Reviewer alert sent to the configured webhook" if alerted else "")

    yield result


def _field_summary(e) -> str:
    parts = []
    parts.append(f"Vendor: {e.vendor_name_raw or '—'}")
    parts.append(f"Invoice#: {e.invoice_number or '—'}")
    parts.append(f"Date: {e.invoice_date or '—'}")
    parts.append(f"PO: {e.po_number or '—'}")
    parts.append(f"Total: ₹{e.total:,.2f}" if e.total else "Total: —")
    return " | ".join(parts)
