"""
Unit + integration tests.

Unit tests isolate one module (extraction regex, tolerance math, fuzzy
matching). Integration tests run the full pipeline against the generated
sample PDFs and assert the expected final decision — this is also living
documentation of what each edge case is supposed to do.

Run: pytest tests/ -v   (from the project root, with sample_invoices/
already generated)
"""
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import database, rules, matching, extraction
from core.models import ExtractedInvoice, VendorMatch, POMatch
from core.pipeline import run_pipeline

SAMPLES = ROOT / "sample_invoices"


@pytest.fixture(autouse=True)
def clean_db():
    database.reset_db()
    yield


def run_and_get_result(filename):
    result = None
    for item in run_pipeline(str(SAMPLES / filename), filename):
        if hasattr(item, "decision"):
            result = item
    return result


# ---------------------------------------------------------------- Unit tests

def test_amount_within_tolerance_approves():
    extracted = ExtractedInvoice(total=100.0)
    po = POMatch(matched=True, po_number="PO-X", po_amount=100.0,
                 previously_invoiced_amount=0, remaining_po_balance=100.0)
    severity, _ = rules.validate_amount(extracted, po)
    assert severity == "ok"


def test_amount_moderate_overage_is_review():
    extracted = ExtractedInvoice(total=108.0)  # 8% over
    po = POMatch(matched=True, po_number="PO-X", po_amount=100.0,
                 previously_invoiced_amount=0, remaining_po_balance=100.0)
    severity, _ = rules.validate_amount(extracted, po)
    assert severity == "review"


def test_amount_severe_overage_is_reject():
    extracted = ExtractedInvoice(total=130.0)  # 30% over
    po = POMatch(matched=True, po_number="PO-X", po_amount=100.0,
                 previously_invoiced_amount=0, remaining_po_balance=100.0)
    severity, _ = rules.validate_amount(extracted, po)
    assert severity == "reject"


def test_decide_approves_clean_invoice():
    extracted = ExtractedInvoice(vendor_name_raw="X", invoice_number="I1", invoice_date="2026-01-01",
                                  total=100.0, extraction_confidence=1.0)
    vm = VendorMatch(matched=True, is_approved=True, vendor_id="V-1", matched_name="X")
    po = POMatch(matched=True, po_number="PO-1", po_amount=100.0, remaining_po_balance=100.0)
    from core.models import DuplicateCheck
    decision = rules.decide(extracted, vm, po, DuplicateCheck())
    assert decision.status == "Approved"


def test_decide_rejects_inactive_vendor():
    extracted = ExtractedInvoice(vendor_name_raw="X", invoice_number="I1", invoice_date="2026-01-01",
                                  total=100.0, extraction_confidence=1.0)
    vm = VendorMatch(matched=True, is_approved=False, vendor_id="V-1", matched_name="X")
    po = POMatch(matched=True, po_number="PO-1", po_amount=100.0, remaining_po_balance=100.0)
    from core.models import DuplicateCheck
    decision = rules.decide(extracted, vm, po, DuplicateCheck())
    assert decision.status == "Rejected"


def test_vendor_fuzzy_match_handles_minor_typo():
    vm = matching.match_vendor("Nimbus Office Supplies Pvt. Ltd")  # trailing period variant
    assert vm.matched
    assert vm.vendor_id == "V-1001"


def test_vendor_no_match_for_unknown_name():
    vm = matching.match_vendor("Totally Unknown Vendor XYZ Corp")
    assert not vm.matched


# --- Regression tests for a real-world invoice format found during manual
# testing: spelled-out dates, "Amount:" instead of "Total:", and a line
# ("Total: 2 Item(s)...") that looks like a total label but isn't money. ---

def test_spelled_out_date_is_parsed():
    inv = extraction.parse_fields("Invoice Date: October 03, 2026\n", "digital_text")
    assert inv.invoice_date == "2026-10-03"


def test_amount_label_used_when_no_total_label_present():
    text = "Payment Mode: Prepaid\nAmount: Rs. 32108.0\n"
    inv = extraction.parse_fields(text, "digital_text")
    assert inv.total == 32108.0


def test_non_currency_total_line_is_not_mistaken_for_the_amount():
    """A courier/e-commerce footer like 'Total: 2 Item(s)' must NOT be read
    as the invoice amount just because it starts with 'Total:' — it has no
    currency symbol, so the real amount (behind 'Amount:') should win."""
    text = "Amount: Rs. 32108.0\nTotal: 2 Item(s) in this shipment\n"
    inv = extraction.parse_fields(text, "digital_text")
    assert inv.total == 32108.0


def test_total_derived_from_subtotal_and_tax_when_no_label_found():
    text = "Subtotal: Rs. 1000.00\nTax: Rs. 180.00\n"
    inv = extraction.parse_fields(text, "digital_text")
    assert inv.total == 1180.00


# --- Regression tests from a second round of real invoices (an e-commerce
# "tax invoice" layout): a block label read as the vendor, a GST registration
# number read as a tax amount, the total sitting before the word "Total", and
# numeric dates that are ambiguous between DD-MM and MM-DD. ---

def test_from_label_is_not_taken_as_vendor_name():
    inv = extraction.parse_fields("TAX INVOICE\nFrom\nINTACT NUTRITION\nDelhi 110031\n", "digital_text")
    assert inv.vendor_name_raw == "INTACT NUTRITION"


def test_gst_registration_number_is_not_read_as_tax_amount():
    inv = extraction.parse_fields("GST: 07ARJPM0849F1ZQ\n", "digital_text")
    assert inv.tax is None


def test_total_from_tax_inclusive_table_layout():
    text = ("Subtotal \u20b99,100.00 (incl. tax)\nShipping Free Shipping\n-\u20b9455.00 (incl. tax) via\n"
            "Prepaid Discount\nFee\n-\u20b9455.00\n\u20b98,645.00 (incl.\nTotal\ntax \u20b9433.33)\n")
    inv = extraction.parse_fields(text, "digital_text")
    assert inv.total == 8645.00


def test_numeric_date_order_is_inferred_from_another_date_in_the_document():
    inv = extraction.parse_fields("Invoice date: 10-05-2026\nDue date: 10-25-2026\n", "digital_text")
    assert inv.invoice_date == "2026-10-05"
    assert not inv.notes


def test_ambiguous_numeric_date_is_flagged_not_silently_guessed():
    inv = extraction.parse_fields("Invoice date: 10-05-2026\n", "digital_text")
    assert inv.invoice_date == "2026-05-10"   # DD-MM default, but...
    assert inv.notes                           # ...it says so


# ----------------------------------------------------------- Integration tests

def test_happy_path_approves():
    result = run_and_get_result("01_happy_path_nimbus.pdf")
    assert result.decision.status == "Approved"
    assert result.extracted.total == 48500.00


def test_duplicate_invoice_rejected_on_resubmit():
    first = run_and_get_result("02_edge_duplicate_coral.pdf")
    assert first.decision.status == "Approved"
    second = run_and_get_result("02_edge_duplicate_coral.pdf")
    assert second.decision.status == "Rejected"
    assert "Duplicate" in " ".join(second.decision.reasons)


def test_moderate_amount_mismatch_needs_review():
    result = run_and_get_result("03_edge_amount_mismatch_vertex.pdf")
    assert result.decision.status == "Needs Review"


def test_scanned_invoice_uses_ocr_and_extracts_correctly():
    result = run_and_get_result("04_edge_scanned_apex.pdf")
    assert result.extracted.extraction_method.startswith("ocr")
    assert result.extracted.total == 32000.00
    assert result.decision.status == "Approved"


def test_unknown_vendor_needs_review():
    result = run_and_get_result("05_edge_unknown_vendor.pdf")
    assert result.decision.status == "Needs Review"
    assert any("not found in approved vendor database" in r for r in result.decision.reasons)


def test_split_po_both_invoices_approve():
    first = run_and_get_result("06_bonus_split_po_part1_brightpath.pdf")
    second = run_and_get_result("06_bonus_split_po_part2_brightpath.pdf")
    assert first.decision.status == "Approved"
    assert second.decision.status == "Approved"
    assert second.po_match.previously_invoiced_amount == 75000.00


def test_inactive_vendor_rejected():
    result = run_and_get_result("07_bonus_inactive_vendor_meridian.pdf")
    assert result.decision.status == "Rejected"
    assert any("not currently approved" in r for r in result.decision.reasons)


def test_missing_invoice_number_needs_review():
    result = run_and_get_result("08_bonus_missing_fields_sterling.pdf")
    assert result.decision.status == "Needs Review"
    assert "Invoice number" in result.extracted.missing_fields()
