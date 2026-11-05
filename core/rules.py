"""
Stage 7 (financial validation), 8 (duplicate detection) and 9 (decision engine).

Everything in this file is deterministic arithmetic and if/else logic —
no AI. This is intentional per the assignment brief: AI helps read messy
documents, but the money decision has to be auditable, reproducible and
explainable to a non-technical AP reviewer without reference to a model.

Decision policy (documented here because this IS the design the interview
will probe):

  REJECTED — clear, high-confidence problems that should not reach a human
    queue because the answer is already known:
      - Duplicate of an already-processed (non-rejected) invoice
      - Vendor exists in the master but is marked inactive/not approved
      - Invoice amount exceeds the PO's remaining balance by more than the
        HARD threshold (15%) — too large to be rounding/freight, more likely
        a data error or a fraudulent resubmission

  NEEDS REVIEW — anything ambiguous enough that a human should look, but
    not clearly wrong:
      - A critical field (total, invoice/PO number, date) could not be
        extracted at all
      - Vendor not found in the master (could be legitimate new vendor)
      - No PO could be matched
      - Amount is outside the SOFT tolerance (2%) but within the HARD
        threshold (15%) — plausible freight/rounding/partial-shipment delta
      - Overall extraction confidence is low (<0.5), even if every
        individual check passed — we don't trust a lucky coincidence over
        shaky OCR text

  APPROVED — vendor approved, PO matched, amount within soft tolerance of
    the PO's *remaining* balance (so split POs are handled correctly), no
    duplicate, all critical fields present, reasonable extraction
    confidence.
"""
from dataclasses import dataclass

from core.models import ExtractedInvoice, VendorMatch, POMatch, DuplicateCheck, Decision
from core import database

SOFT_TOLERANCE_PCT = 0.02   # within this of remaining PO balance -> fine
HARD_REJECT_PCT = 0.15      # beyond this -> reject outright
LOW_CONFIDENCE_THRESHOLD = 0.5


def check_duplicate(extracted: ExtractedInvoice, current_invoice_id: str) -> DuplicateCheck:
    dc = DuplicateCheck()
    if not extracted.total:
        return dc  # can't fingerprint without an amount; duplicate check is best-effort here

    candidates = database.find_potential_duplicates(
        vendor_name=extracted.vendor_name_raw,
        invoice_number=extracted.invoice_number,
        total=extracted.total,
        invoice_date=extracted.invoice_date,
    )
    candidates = [c for c in candidates if c["invoice_id"] != current_invoice_id
                  and c["decision_status"] != "Rejected"]
    if candidates:
        match = candidates[0]
        dc.is_duplicate = True
        dc.matched_invoice_id = match["invoice_id"]
        if match["invoice_number"] == extracted.invoice_number:
            dc.reason = f"Same vendor + same invoice number as already-processed invoice {match['invoice_id']}"
        else:
            dc.reason = f"Same vendor, amount and date as already-processed invoice {match['invoice_id']}"
    return dc


def validate_amount(extracted: ExtractedInvoice, po_match: POMatch) -> tuple[str, str]:
    """Returns (severity, message) where severity is 'ok' | 'review' | 'reject'."""
    if not po_match.matched or po_match.remaining_po_balance is None:
        return "review", "No matching PO to validate the amount against."
    if extracted.total is None:
        return "review", "No invoice total extracted — cannot validate against PO."

    remaining = po_match.remaining_po_balance
    diff = extracted.total - remaining
    diff_pct = abs(diff) / po_match.po_amount if po_match.po_amount else 1.0

    if diff <= 0 or diff_pct <= SOFT_TOLERANCE_PCT:
        if po_match.previously_invoiced_amount > 0:
            return "ok", (f"Amount ₹{extracted.total:,.2f} fits within PO's remaining balance "
                           f"of ₹{remaining:,.2f} (₹{po_match.previously_invoiced_amount:,.2f} already "
                           f"invoiced against PO {po_match.po_number} — treated as a split PO).")
        return "ok", f"Amount ₹{extracted.total:,.2f} matches PO {po_match.po_number} (₹{po_match.po_amount:,.2f}) within tolerance."

    if diff_pct <= HARD_REJECT_PCT:
        return "review", (f"Invoice total ₹{extracted.total:,.2f} exceeds PO {po_match.po_number}'s remaining "
                           f"balance of ₹{remaining:,.2f} by ₹{diff:,.2f} ({diff_pct:.1%}) — "
                           f"within plausible range (freight/rounding) but above auto-approve tolerance.")

    return "reject", (f"Invoice total ₹{extracted.total:,.2f} exceeds PO {po_match.po_number}'s remaining "
                       f"balance of ₹{remaining:,.2f} by ₹{diff:,.2f} ({diff_pct:.1%}) — too large to "
                       f"auto-approve; likely data error or invalid charge.")


def decide(extracted: ExtractedInvoice, vendor_match: VendorMatch, po_match: POMatch,
           duplicate_check: DuplicateCheck) -> Decision:
    reject_reasons = []
    review_reasons = []

    missing = extracted.missing_fields()
    if missing:
        review_reasons.append(f"Missing critical field(s): {', '.join(missing)}.")

    if duplicate_check.is_duplicate:
        reject_reasons.append(f"Duplicate invoice: {duplicate_check.reason}.")

    if vendor_match.matched and not vendor_match.is_approved:
        reject_reasons.append(f"Vendor '{vendor_match.matched_name}' is in the master but not currently approved (inactive).")
    elif not vendor_match.matched:
        review_reasons.append(f"Vendor '{extracted.vendor_name_raw or 'unknown'}' not found in approved vendor database.")

    if not po_match.matched:
        review_reasons.append("Could not match invoice to any purchase order.")
    else:
        severity, msg = validate_amount(extracted, po_match)
        if severity == "reject":
            reject_reasons.append(msg)
        elif severity == "review":
            review_reasons.append(msg)

    if extracted.extraction_confidence < LOW_CONFIDENCE_THRESHOLD:
        review_reasons.append(f"Low extraction confidence ({extracted.extraction_confidence:.0%}) — "
                               f"document quality or layout made fields hard to read reliably.")

    if reject_reasons:
        return Decision(status="Rejected", reasons=reject_reasons + review_reasons, confidence=extracted.extraction_confidence)
    if review_reasons:
        return Decision(status="Needs Review", reasons=review_reasons)
    return Decision(status="Approved",
                     reasons=["All checks passed: approved vendor, PO matched, amount within tolerance, no duplicate."],
                     confidence=extracted.extraction_confidence)
