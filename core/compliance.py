"""
India GST and currency checks on the supplier's invoice.

Everything here is deterministic (a checksum, a lookup table and string
comparisons), in keeping with the rest of the decision logic: a model may help
READ a document, but whether an invoice looks compliant is arithmetic.

What is checked, and why it is safe to check:
  - GSTIN format and checksum. A GSTIN is 15 characters; the last one is a
    check character computed from the first 14, so a mistyped or invented
    number almost always fails. (A passing checksum does NOT prove the number is
    registered — that needs the GST portal — so this only ever catches errors.)
  - GSTIN state code against the table of Indian state codes.
  - Tax type against the supply: a supplier in the same state as the place of
    supply charges CGST + SGST; a supplier in another state charges IGST.
    Charging the wrong one is a common invoice error that blocks input tax
    credit for the buyer.
  - CGST and SGST totals should be equal (each is half of the GST).
  - Currency. The PO dataset is in rupees; an invoice in another currency can't
    be compared against it without a conversion this system does not guess.

Each problem becomes a plain-English flag that the decision engine turns into a
"Needs Review" reason. Nothing here ever rejects an invoice on its own.
"""
import re
from dataclasses import dataclass, field

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

STATE_NAMES = {
    1: "Jammu & Kashmir", 2: "Himachal Pradesh", 3: "Punjab", 4: "Chandigarh", 5: "Uttarakhand",
    6: "Haryana", 7: "Delhi", 8: "Rajasthan", 9: "Uttar Pradesh", 10: "Bihar", 11: "Sikkim",
    12: "Arunachal Pradesh", 13: "Nagaland", 14: "Manipur", 15: "Mizoram", 16: "Tripura",
    17: "Meghalaya", 18: "Assam", 19: "West Bengal", 20: "Jharkhand", 21: "Odisha",
    22: "Chhattisgarh", 23: "Madhya Pradesh", 24: "Gujarat", 26: "Dadra & Nagar Haveli and Daman & Diu",
    27: "Maharashtra", 28: "Andhra Pradesh (old)", 29: "Karnataka", 30: "Goa", 31: "Lakshadweep",
    32: "Kerala", 33: "Tamil Nadu", 34: "Puducherry", 35: "Andaman & Nicobar Islands",
    36: "Telangana", 37: "Andhra Pradesh", 38: "Ladakh", 97: "Other Territory", 99: "Centre Jurisdiction",
}
_CODE_BY_NAME = {name.upper(): code for code, name in STATE_NAMES.items()}

_FORMAT = re.compile(r"^(\d{2})[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
# A GSTIN printed next to a "GST" / "GSTIN" label. (?<![A-Za-z]) keeps CGST/SGST/IGST out.
# 14-16 characters are captured so a mistyped, too-short or too-long number is still caught and flagged.
_LABELLED = re.compile(r"(?<![A-Za-z])GST(?:IN)?(?:\s*(?:No\.?|Number|Reg(?:istration)?\.?))?\s*[:\-]?\s*([0-9A-Za-z]{14,16})\b", re.I)
_SUPPLY_CODE = re.compile(r"State\s*Code\s*[:\-]?\s*(\d{2})", re.I)
_SUPPLY_NAME = re.compile(r"Place\s*of\s*Supply\s*[:\-]?\s*([A-Za-z &]+?)\s*(?:\n|$)", re.I)
_NUM = r"([\d,]+\.?\d*)"
_GRAND_ROW = re.compile(rf"Grand\s*Total\s+\d+\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}", re.I)


def gstin_checksum_ok(gstin: str) -> bool:
    """The GSTIN check character: weights alternate 1,2 over the first 14
    characters, each product is folded base-36, and the check character is
    whatever brings the running sum to a multiple of 36."""
    if len(gstin) != 15 or any(c not in _ALPHABET for c in gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        v = _ALPHABET.index(ch) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return _ALPHABET[(36 - total % 36) % 36] == gstin[14]


@dataclass
class GstReport:
    gstin: str | None = None
    flags: list = field(default_factory=list)     # a human must look at these
    notes: list = field(default_factory=list)     # informational, shown in the stage detail


def _amount(s: str) -> float:
    return float(s.replace(",", ""))


def analyse_gst(text: str) -> GstReport:
    rep = GstReport()
    m = _LABELLED.search(text)
    if not m:
        return rep
    gstin = m.group(1).upper()
    rep.gstin = gstin

    fmt = _FORMAT.match(gstin)
    if not fmt:
        rep.flags.append(f"GSTIN '{gstin}' is not in a valid GSTIN format (15 characters: state code, PAN, entity, Z, check digit).")
        return rep
    state = int(fmt.group(1))
    if state not in STATE_NAMES:
        rep.flags.append(f"GSTIN '{gstin}' starts with {state:02d}, which is not an Indian GST state code.")
        return rep
    if not gstin_checksum_ok(gstin):
        rep.flags.append(f"GSTIN '{gstin}' fails the GST check-character test — likely a typo or an invented number.")
        return rep
    rep.notes.append(f"GSTIN {gstin} passes the format and checksum test ({STATE_NAMES[state]})")

    has_igst = bool(re.search(r"\bIGST\b", text))
    has_cgst = bool(re.search(r"\bCGST\b", text))
    has_sgst = bool(re.search(r"\bSGST\b", text))

    supply = None
    sm = _SUPPLY_CODE.search(text)
    if sm:
        supply = int(sm.group(1))
    else:
        nm = _SUPPLY_NAME.search(text)
        if nm:
            supply = _CODE_BY_NAME.get(nm.group(1).strip().upper())

    if supply is not None and (has_igst or (has_cgst and has_sgst)):
        same_state = supply == state
        if same_state and has_igst and not has_cgst:
            rep.flags.append(f"Supplier state ({state:02d}) equals the place of supply ({supply:02d}), so CGST + SGST should be charged, but the invoice charges IGST.")
        elif not same_state and (has_cgst or has_sgst) and not has_igst:
            rep.flags.append(f"Supplier state ({state:02d}) differs from the place of supply ({supply:02d}), so IGST should be charged, but the invoice charges CGST/SGST.")
        else:
            rep.notes.append("tax type (" + ("CGST + SGST" if same_state else "IGST") + ") matches the supplier and supply states")

    if has_cgst and has_sgst:
        row = _GRAND_ROW.search(text)
        if row:
            cgst, sgst = _amount(row.group(2)), _amount(row.group(3))
            if abs(cgst - sgst) > 0.02:
                rep.flags.append(f"CGST total ₹{cgst:,.2f} and SGST total ₹{sgst:,.2f} differ; they should be equal.")
    return rep


_INR = re.compile(r"₹|\bRs\b\.?|\bINR\b", re.I)
_FOREIGN = {
    "USD": re.compile(r"\$|\bUSD\b"),
    "EUR": re.compile(r"€|\bEUR\b"),
    "GBP": re.compile(r"£|\bGBP\b"),
}


def detect_currency(text: str) -> str:
    """INR unless the document uses only another currency's markers."""
    if _INR.search(text):
        return "INR"
    counts = {code: len(rx.findall(text)) for code, rx in _FOREIGN.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else "INR"


def check_invoice(text: str) -> tuple[str | None, str, list, list]:
    """Returns (gstin, currency, flags, notes)."""
    rep = analyse_gst(text)
    currency = detect_currency(text)
    flags = list(rep.flags)
    notes = list(rep.notes)
    if currency != "INR":
        flags.append(f"Invoice is in {currency} but the purchase-order data is in INR. No currency conversion is applied, "
                     f"so the amount cannot be validated against the PO.")
    return rep.gstin, currency, flags, notes
