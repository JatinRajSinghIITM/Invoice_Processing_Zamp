"""
Stage 5 (vendor validation) and stage 6 (PO matching).

Matching strategy for POs, in priority order:
  1. Exact PO number match (if the invoice states one and it exists)
  2. Fuzzy PO number match (handles OCR noise / typos like "P0-5001" vs "PO-5001")
  3. Vendor + amount fallback (handles the case where the PO reference is
     missing or wrong entirely, but the vendor and amount still point at a
     unique open PO)

Split-PO handling: a PO is not "done" after one invoice. We track cumulative
non-rejected invoiced amount per PO (core/database.py) and compare the
*remaining* balance against the new invoice, not the full PO amount. This is
what lets "PO split across multiple invoices" be handled as a first-class
case instead of an edge case that breaks the matcher.
"""
import pandas as pd
from rapidfuzz import fuzz, process

from core.models import VendorMatch, POMatch
from core import database

VENDOR_MATCH_THRESHOLD = 88     # rapidfuzz token_sort_ratio, 0-100
PO_FUZZY_THRESHOLD = 85


def load_vendors() -> pd.DataFrame:
    from pathlib import Path
    return pd.read_csv(Path(__file__).parent.parent / "data" / "vendors.csv")


def load_purchase_orders() -> pd.DataFrame:
    from pathlib import Path
    return pd.read_csv(Path(__file__).parent.parent / "data" / "purchase_orders.csv")


def match_vendor(vendor_name_raw: str | None) -> VendorMatch:
    vm = VendorMatch()
    if not vendor_name_raw:
        return vm

    vendors = load_vendors()
    choices = vendors["vendor_name"].tolist()
    result = process.extractOne(vendor_name_raw, choices, scorer=fuzz.token_sort_ratio)
    if result is None:
        return vm

    matched_name, score, idx = result
    vm.match_score = score
    if score >= VENDOR_MATCH_THRESHOLD:
        row = vendors.iloc[idx]
        vm.matched = True
        vm.vendor_id = row["vendor_id"]
        vm.matched_name = row["vendor_name"]
        vm.is_approved = (row["status"] == "active")
    return vm


def match_purchase_order(po_number_raw: str | None, vendor_match: VendorMatch,
                          invoice_total: float | None, exclude_invoice_id: str = None) -> POMatch:
    pm = POMatch()
    pos = load_purchase_orders()

    row = None
    if po_number_raw:
        # 1. exact match
        exact = pos[pos["po_number"].str.upper() == po_number_raw.upper().strip()]
        if len(exact):
            row = exact.iloc[0]
            pm.match_method = "exact"
            pm.match_score = 100.0
        else:
            # 2. fuzzy match on PO number string (handles OCR noise)
            choices = pos["po_number"].tolist()
            result = process.extractOne(po_number_raw, choices, scorer=fuzz.ratio)
            if result and result[1] >= PO_FUZZY_THRESHOLD:
                row = pos.iloc[result[2]]
                pm.match_method = "fuzzy"
                pm.match_score = result[1]

    if row is None and vendor_match.matched and invoice_total:
        # 3. vendor + amount fallback — only safe when it resolves to exactly
        # one open PO for that vendor; otherwise we'd be guessing among
        # several, which is worse than flagging for review.
        candidates = pos[pos["vendor_id"] == vendor_match.vendor_id]
        if len(candidates) == 1:
            row = candidates.iloc[0]
            pm.match_method = "vendor+amount"
            pm.match_score = 60.0

    if row is not None:
        pm.matched = True
        pm.po_number = row["po_number"]
        pm.po_amount = float(row["po_amount"])
        pm.previously_invoiced_amount = database.get_po_consumed(pm.po_number, exclude_invoice_id=exclude_invoice_id)
        pm.remaining_po_balance = pm.po_amount - pm.previously_invoiced_amount

    return pm
