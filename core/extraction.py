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

If no LLM API key is configured, extraction simply stays deterministic and
reports lower confidence on hard documents — which flows into the rules
engine as "Needs Review" rather than a silent wrong guess. That's a feature:
an uncertain system that says so beats one that confidently hallucinates a
total.
"""
import os
import re
import io
import shutil
from datetime import datetime

import pdfplumber

from core.models import ExtractedInvoice

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


def _find_tesseract_cmd() -> str | None:
    """Locate tesseract.exe: TESSERACT_CMD env var -> PATH -> common install dirs.
    On Windows the UB-Mannheim installer often doesn't add itself to PATH."""
    env = os.environ.get("TESSERACT_CMD")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    return next((c for c in candidates if os.path.isfile(c)), None)


def _render_pages(pdf_path: str, dpi: int = 300) -> list:
    """Rasterise each page to a PIL image via pdfplumber's bundled pypdfium2
    renderer — no external Poppler install needed (Poppler's Windows builds
    are fragile, and this keeps OCR down to a single system dependency)."""
    with pdfplumber.open(pdf_path) as pdf:
        return [page.to_image(resolution=dpi).original.copy() for page in pdf.pages]


def _run_ocr(pdf_path: str) -> str:
    try:
        import pytesseract
        tess = _find_tesseract_cmd()
        if tess:
            pytesseract.pytesseract.tesseract_cmd = tess
        images = _render_pages(pdf_path)
        pages_text = [pytesseract.image_to_string(_preprocess_for_ocr(img)) for img in images]
        return _normalize_ocr_text("\n".join(pages_text).strip())
    except Exception:
        return ""  # OCR failure -> empty text -> rules engine will flag missing fields


def _preprocess_for_ocr(img):
    """Grayscale + auto-contrast only. An earlier version also applied a
    median filter and a fixed-threshold binarization, but on blurred scans
    that erased thin glyphs — colons, hyphens and decimal points — which are
    exactly the characters the field regexes depend on. Tesseract 5's own
    internal binarization handles speckle noise better than a fixed cutoff."""
    from PIL import ImageOps
    return ImageOps.autocontrast(img.convert("L"))


# Common Tesseract misreads of the "Rs" currency marker on noisy scans
# (R -> P/A/F, s -> a/e). Only rewritten when immediately followed by a number,
# and only on OCR text, so it can't touch ordinary words on digital PDFs.
_OCR_RS_MISREAD = re.compile(r"(?<![A-Za-z])(?:Ps|As|Fs|Ra|Re|Pe|Rs)[.,]?(?=\s*\d)")


def _normalize_ocr_text(text: str) -> str:
    return _OCR_RS_MISREAD.sub("Rs", text)


_PATTERNS = {
    "invoice_number": re.compile(r"Invoice\s*(?:Number|No\.?|#)\s*[:\-]\s*([A-Za-z0-9\-\/]+)", re.I),
    # Date capture is deliberately loose (to end of line) because real invoices
    # write dates many ways ("October 03, 2026", "03-Oct-2026", etc.) —
    # _parse_date below does the actual format-handling, trying exact formats
    # first and falling back to dateutil's flexible parser.
    "invoice_date": re.compile(r"Invoice\s*Date\s*[:\-]\s*([^\n|]+)", re.I),
    "po_number": re.compile(r"P\.?O\.?\s*(?:Number|No\.?|Ref(?:erence)?)\s*[:\-]\s*([A-Za-z0-9\-\/]+)", re.I),
    "subtotal": re.compile(r"Sub\s*-?\s*total\s*[:\-]?\s*(?:Rs\.?|INR|\$|₹)?\s*([\d,]+\.?\d*)", re.I),
    "tax": re.compile(r"(?:Tax|GST|VAT)\b[^:\n]*[:\-]\s*(?:Rs\.?|INR|\$|₹)?\s*([\d,]+\.?\d*)", re.I),
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
    # Colon is optional (OCR often drops it); the REQUIRED currency marker is
    # what keeps "Total: 2 Item(s)" from matching, not the colon.
    re.compile(r"^\s*Grand\s*Total\s*[:\-]?\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I | re.M),
    re.compile(r"^\s*Total\s*[:\-]?\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I | re.M),
    re.compile(r"(?:Amount\s*Payable|Net\s*Payable|Amount\s*Due)\s*[:\-]?\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"\bAmount\s*[:\-]?\s*(?:Rs\.?|INR|\$|₹)\s*([\d,]+\.?\d*)", re.I),
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


def _parse_date(s: str) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Flexible fallback for real-world formats like "October 03, 2026",
    # "3 Oct 2026", or a trailing word dateutil can safely ignore.
    try:
        from dateutil import parser as dateparser
        dt = dateparser.parse(s, fuzzy=True, dayfirst=False)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _guess_vendor_name(text: str) -> str | None:
    """Heuristic: the vendor letterhead is almost always the first
    non-empty line that isn't a generic header word like 'INVOICE'."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper() in {"INVOICE", "TAX INVOICE", "BILL"}:
            continue
        return line
    return None


def parse_fields(text: str, method: str) -> ExtractedInvoice:
    inv = ExtractedInvoice(extraction_method=method, raw_text_excerpt=text[:600])

    inv.vendor_name_raw = _guess_vendor_name(text)

    m = _PATTERNS["invoice_number"].search(text)
    inv.invoice_number = m.group(1).strip().rstrip("-./") if m else None

    m = _PATTERNS["invoice_date"].search(text)
    inv.invoice_date = _parse_date(m.group(1)) if m else None

    m = _PATTERNS["po_number"].search(text)
    inv.po_number = m.group(1).strip().rstrip("-./") if m else None

    m = _PATTERNS["subtotal"].search(text)
    inv.subtotal = _parse_amount(m.group(1)) if m else None

    m = _PATTERNS["tax"].search(text)
    inv.tax = _parse_amount(m.group(1)) if m else None

    inv.total, total_source = _extract_total(text, inv.subtotal, inv.tax)

    inv.extraction_confidence = _score_confidence(inv, method, total_source)
    return inv


def _score_confidence(inv: ExtractedInvoice, method: str, total_source: str = "matched") -> float:
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

    if method == "ocr":
        score *= 0.85  # OCR text is noisier; discount confidence accordingly

    return round(score, 2)


LLM_CONFIDENCE_THRESHOLD = 0.6


def llm_assist_if_available(inv: ExtractedInvoice, raw_text: str) -> ExtractedInvoice:
    """Optional second pass. Only triggers when deterministic confidence is
    low AND a key is configured — see module docstring for why this is
    gated rather than always-on."""
    if inv.extraction_confidence >= LLM_CONFIDENCE_THRESHOLD:
        return inv

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return inv  # no key configured -> stay deterministic, let rules engine route to review

    try:
        filled = _call_llm_extractor(raw_text)
    except Exception:
        return inv  # any API/network failure -> degrade gracefully, never crash the pipeline

    # Only fill gaps the regex pass missed; never silently overwrite a
    # deterministic field we already trust.
    for field_name in ("vendor_name_raw", "invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total"):
        if getattr(inv, field_name) in (None, "", 0) and filled.get(field_name):
            setattr(inv, field_name, filled[field_name])

    inv.extraction_method = f"{inv.extraction_method}+llm_assist"
    inv.extraction_confidence = min(1.0, inv.extraction_confidence + 0.25)
    return inv


def _call_llm_extractor(raw_text: str) -> dict:
    """Thin, provider-agnostic wrapper. Only Anthropic is wired by default
    since this app ships with the anthropic SDK available; OpenAI/Gemini can
    be dropped in the same shape if that's the key you have."""
    import anthropic
    client = anthropic.Anthropic()
    prompt = f"""Extract these fields from the invoice text below as JSON only
(no prose): vendor_name_raw, invoice_number, invoice_date (YYYY-MM-DD),
po_number, subtotal (number), tax (number), total (number). Use null for
anything not present. Do not guess numbers that aren't in the text.

INVOICE TEXT:
{raw_text[:3000]}
"""
    resp = client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    import json
    content = resp.content[0].text
    return json.loads(content)


def extract_invoice(pdf_path: str) -> ExtractedInvoice:
    raw_text, method = extract_raw_text(pdf_path)
    inv = parse_fields(raw_text, method)
    inv = llm_assist_if_available(inv, raw_text)
    return inv
