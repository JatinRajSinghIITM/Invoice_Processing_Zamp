"""
Stage 2+3+4 (text extraction, OCR fallback, field parsing, optional LLM assist).

Design decision: field parsing is regex/heuristic-first, not LLM-first.
- It's deterministic and explainable (you can point at the exact regex that
  matched, which matters a lot in an AP audit context).
- It's free and instant, so the happy path never depends on an API key.
- An optional LLM pass only kicks in when the deterministic pass is NOT
  confident (garbled OCR text, unrecognised layout) — exactly where the
  assignment brief says AI earns its place: "AI should help with
  unstructured document understanding," not do the arithmetic.

The model pass lives in core/ai_assist.py, behind a key, a spending cap and a
check that every value it returns actually appears in the document. If no key
is configured, extraction simply stays deterministic and
reports lower confidence on hard documents — which flows into the rules
engine as "Needs Review" rather than a silent wrong guess. That's a feature:
an uncertain system that says so beats one that confidently hallucinates a
total.
"""
import os
import re
import io
from datetime import datetime

import pdfplumber

from core.models import ExtractedInvoice
from core import compliance

MIN_DIGITAL_TEXT_CHARS = 40  # below this, we assume the PDF is a scanned image


def extract_raw_text(pdf_path: str) -> tuple[str, str]:
    """Returns (text, method) where method is 'digital_text' or 'ocr'."""
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            text_parts.append(t)
    digital_text = "\n".join(text_parts).strip()

    if len(digital_text) >= MIN_DIGITAL_TEXT_CHARS:
        return digital_text, "digital_text"

    # Fall back to OCR for scanned/low-text PDFs
    ocr_text = _run_ocr(pdf_path)
    return ocr_text, "ocr"


def _run_ocr(pdf_path: str) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(pdf_path, dpi=300)
        pages_text = [pytesseract.image_to_string(_preprocess_for_ocr(img)) for img in images]
        return "\n".join(pages_text).strip()
    except Exception:
        return ""  # OCR failure -> empty text -> rules engine will flag missing fields


def _preprocess_for_ocr(img):
    """Basic denoise + binarize pass. Scanned/photographed invoices are
    rarely clean — a cheap threshold before OCR meaningfully improves
    Tesseract's accuracy on speckle noise and uneven scan backgrounds."""
    from PIL import ImageFilter
    gray = img.convert("L")
    gray = gray.filter(ImageFilter.MedianFilter(size=3))  # knock out salt-and-pepper noise
    # Simple fixed threshold binarization
    bw = gray.point(lambda p: 255 if p > 150 else 0)
    return bw


_PATTERNS = {
    "invoice_number": re.compile(r"Invoice\s*(?:Number|No\.?|#)\s*[:\-]\s*([A-Za-z0-9\-\/]+)", re.I),
    # Date capture is deliberately loose (to end of line) because real invoices
    # write dates many ways ("October 03, 2026", "03-Oct-2026", etc.) —
    # _parse_date below does the actual format-handling, trying exact formats
    # first and falling back to dateutil's flexible parser.
    "invoice_date": re.compile(r"Invoice\s*Date\s*[:\-]\s*([^\n|]+)", re.I),
    "po_number": re.compile(r"P\.?O\.?\s*(?:Number|No\.?|Ref(?:erence)?)\s*[:\-]\s*([A-Za-z0-9\-\/]+)", re.I),
    # Currency symbol is REQUIRED for subtotal and tax, for the same reason as
    # the total: "GST: 07ARJPM0849F1ZQ" (a registration number) was once read
    # as a tax amount of 7.0. The negative lookahead skips a tax-INCLUSIVE
    # subtotal ("Subtotal ₹9,100.00 (incl. tax)"), which must never be added to
    # tax again when deriving a total.
    "subtotal": re.compile(
        r"Sub\s*-?\s*total\s*[:\-]?\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)(?![\d,.]|\s*\(\s*incl)", re.I),
    "tax": re.compile(r"(?:Tax|GST|VAT)\b[^:\n]*[:\-]\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I),
}

# Total-amount extraction is its own function (not a single regex) because
# real invoices label the money field many different ways ("Total",
# "Grand Total", "Amount", "Amount Payable"...). A currency symbol is
# REQUIRED for every candidate on purpose: an earlier version allowed a bare
# number after "Total:", and on a real courier/e-commerce invoice it matched
# an unrelated "Total: 2" (item count) instead of the actual amount. Requiring
# a currency marker trades a little recall (a total with no currency symbol
# at all won't be caught) for a lot of precision (never silently grabbing the
# wrong number) — consistent with this system's "say unsure rather than guess
# wrong" philosophy. Tried in priority order; first match wins.
_TOTAL_CANDIDATE_PATTERNS = [
    re.compile(r"^\s*Grand\s*Total\s*[:\-]\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I | re.M),
    re.compile(r"^\s*Total\s*[:\-]\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I | re.M),
    re.compile(r"(?:Amount\s*Payable|Net\s*Payable|Amount\s*Due)\s*[:\-]\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"\bAmount\s*[:\-]\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I),
    # Table-style layouts (e.g. e-commerce invoices) where pdf text extraction
    # puts the final amount on its own line BEFORE the word "Total", marked
    # tax-inclusive: "₹8,645.00 (incl." A line that merely starts with a
    # currency amount and says "(incl" is the net payable; discount lines start
    # with "-₹" and the subtotal line starts with a word, so neither matches.
    re.compile(r"^\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.\d{2})\s*\(\s*incl", re.I | re.M),
]


def _extract_total(text: str, subtotal: float | None, tax: float | None) -> tuple[float | None, str]:
    """Returns (amount, source_label). source_label is surfaced in the audit
    trail so it's always clear whether the total was read directly or derived."""
    for pattern in _TOTAL_CANDIDATE_PATTERNS:
        m = pattern.search(text)
        if m:
            amount = _parse_amount(m.group(1))
            if amount is not None:
                return amount, "matched"
    # No labeled total found at all — if we have both subtotal and tax,
    # derive it rather than leaving the invoice unprocessable. Flagged as
    # "derived" so confidence scoring can weight it appropriately.
    if subtotal is not None and tax is not None:
        return round(subtotal + tax, 2), "derived"
    return None, "none"


def _parse_amount(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b")


def _infer_date_order(text: str) -> str | None:
    """Numeric dates like 10-05-2026 are ambiguous (5 Oct or 10 May?). If ANY
    numeric date elsewhere in the document is unambiguous (a day above 12),
    that settles the writer's convention for the whole document. Returns
    'dmy', 'mdy', or None when nothing in the document settles it."""
    for a, b, _ in _NUMERIC_DATE.findall(text):
        a, b = int(a), int(b)
        if a > 12 >= b:
            return "dmy"
        if b > 12 >= a:
            return "mdy"
    return None


def _parse_date_ex(s: str, order: str | None = None) -> tuple[str | None, bool]:
    """Returns (iso_date_or_None, ambiguous). `ambiguous` is True only when a
    numeric date could be read two ways AND nothing in the document settled
    it; we then default to DD-MM (Indian convention) but say so."""
    if not s:
        return None, False
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d"), False
        except ValueError:
            return None, False

    m = _NUMERIC_DATE.search(s)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ambiguous = False
        if a > 12:
            day, month = a, b
        elif b > 12:
            day, month = b, a
        elif a == b:
            day, month = a, b
        elif order == "mdy":
            month, day = a, b
        elif order == "dmy":
            day, month = a, b
        else:
            day, month, ambiguous = a, b, True
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d"), ambiguous
        except ValueError:
            return None, False

    # Flexible fallback for real-world formats like "October 03, 2026",
    # "3 Oct 2026", or a trailing word dateutil can safely ignore.
    try:
        from dateutil import parser as dateparser
        dt = dateparser.parse(s, fuzzy=True, dayfirst=False)
        return dt.strftime("%Y-%m-%d"), False
    except Exception:
        return None, False


def _parse_date(s: str, order: str | None = None) -> str | None:
    return _parse_date_ex(s, order)[0]


# Lines that LABEL a block rather than name the vendor. Real invoices often
# put "From" / "Sold by" above the company name; taking that label as the
# vendor ("From") broke vendor matching on a real e-commerce invoice.
_NOT_A_VENDOR_LINE = {
    "INVOICE", "TAX INVOICE", "BILL", "ORIGINAL", "ORIGINAL FOR RECIPIENT", "DUPLICATE",
    "FROM", "BILL FROM", "SOLD BY", "SELLER", "SUPPLIER", "VENDOR", "BILLED BY",
}


def _guess_vendor_name(text: str) -> str | None:
    """Heuristic: the vendor letterhead is almost always the first
    non-empty line that isn't a generic header word like 'INVOICE' or a
    block label like 'From'."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().rstrip(":").strip() in _NOT_A_VENDOR_LINE:
            continue
        return line
    return None


def parse_fields(text: str, method: str) -> ExtractedInvoice:
    inv = ExtractedInvoice(extraction_method=method, raw_text_excerpt=text[:600])

    inv.vendor_name_raw = _guess_vendor_name(text)

    m = _PATTERNS["invoice_number"].search(text)
    inv.invoice_number = m.group(1).strip() if m else None

    m = _PATTERNS["invoice_date"].search(text)
    date_ambiguous = False
    if m:
        inv.invoice_date, date_ambiguous = _parse_date_ex(m.group(1), _infer_date_order(text))
        if date_ambiguous:
            inv.notes.append(
                f"Invoice date '{m.group(1).strip()}' can be read as DD-MM or MM-DD and nothing in the "
                f"document settles which; read as DD-MM-YYYY ({inv.invoice_date}). Verify before relying on it.")

    m = _PATTERNS["po_number"].search(text)
    inv.po_number = m.group(1).strip() if m else None

    m = _PATTERNS["subtotal"].search(text)
    inv.subtotal = _parse_amount(m.group(1)) if m else None

    m = _PATTERNS["tax"].search(text)
    inv.tax = _parse_amount(m.group(1)) if m else None

    inv.total, total_source = _extract_total(text, inv.subtotal, inv.tax)

    inv.gstin, inv.currency, inv.compliance_flags, inv.compliance_notes = compliance.check_invoice(text)

    inv.extraction_confidence = _score_confidence(inv, method, total_source, date_ambiguous)
    return inv


def _score_confidence(inv: ExtractedInvoice, method: str, total_source: str = "matched",
                      date_ambiguous: bool = False) -> float:
    critical = [inv.vendor_name_raw, inv.invoice_number, inv.invoice_date, inv.total]
    found = sum(1 for f in critical if f not in (None, "", 0))
    score = found / len(critical)

    # Arithmetic consistency is a strong extra signal: if subtotal+tax≈total,
    # we trust the numbers were read correctly, not just pattern-matched.
    if inv.subtotal and inv.tax and inv.total:
        if abs((inv.subtotal + inv.tax) - inv.total) < 1.0:
            score = min(1.0, score + 0.15)

    if total_source == "derived":
        # We computed total from subtotal+tax rather than reading a labeled
        # total directly — correct, but one inference removed from the source
        # document, so it shouldn't score as high as a direct read.
        score *= 0.9

    if date_ambiguous:
        score *= 0.95  # one field was a judgment call between two readings

    if method == "ocr":
        score *= 0.85  # OCR text is noisier; discount confidence accordingly

    return round(score, 2)


def extract_invoice(pdf_path: str) -> ExtractedInvoice:
    raw_text, method = extract_raw_text(pdf_path)
    inv = parse_fields(raw_text, method)
    from core import ai_assist          # imported here: ai_assist itself imports this module
    ai_assist.try_assist(inv, raw_text)
    return inv
