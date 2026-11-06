"""
Core data structures shared across the pipeline.

Keeping these as plain dataclasses (not pydantic/ORM models) is a deliberate
choice: the pipeline is a one-week take-home, not a production service, and
dataclasses keep every stage's input/output contract visible and dependency-free.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class ExtractedInvoice:
    """What stage 3 (field parsing) produces. Every field is Optional because
    a messy real-world invoice may simply not contain it — the rules engine,
    not the extractor, decides whether a missing field is fatal."""
    vendor_name_raw: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None          # ISO yyyy-mm-dd if parsed
    po_number: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    line_items: list = field(default_factory=list)

    # Provenance / confidence — used by the rules engine and shown in the UI
    extraction_method: str = "unknown"           # "digital_text" | "ocr" | "llm_assist"
    extraction_confidence: float = 0.0            # 0..1, heuristic
    raw_text_excerpt: str = ""
    notes: list = field(default_factory=list)     # extraction caveats worth showing a human

    def missing_fields(self) -> list:
        required = {
            "vendor_name_raw": "Vendor name",
            "invoice_number": "Invoice number",
            "invoice_date": "Invoice date",
            "total": "Total amount",
        }
        return [label for key, label in required.items() if getattr(self, key) in (None, "", 0)]


@dataclass
class VendorMatch:
    matched: bool = False
    vendor_id: Optional[str] = None
    matched_name: Optional[str] = None
    match_score: float = 0.0                      # 0..100 (rapidfuzz scale)
    is_approved: bool = False


@dataclass
class POMatch:
    matched: bool = False
    po_number: Optional[str] = None
    po_amount: Optional[float] = None
    match_method: str = "none"                     # "exact" | "fuzzy" | "vendor+amount" | "none"
    match_score: float = 0.0
    previously_invoiced_amount: float = 0.0         # sum of prior approved/pending invoices against this PO
    remaining_po_balance: Optional[float] = None


@dataclass
class DuplicateCheck:
    is_duplicate: bool = False
    reason: Optional[str] = None
    matched_invoice_id: Optional[str] = None


@dataclass
class StageResult:
    """One row in the 'live run view' — every pipeline stage emits one of these
    so the UI can render progress and reasoning identically for every run."""
    stage: str
    status: str            # "ok" | "warning" | "fail"
    summary: str
    detail: str = ""


@dataclass
class Decision:
    status: str             # "Approved" | "Needs Review" | "Rejected"
    reasons: list = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class ProcessingResult:
    invoice_id: str
    filename: str
    processed_at: str
    extracted: ExtractedInvoice
    vendor_match: VendorMatch
    po_match: POMatch
    duplicate_check: DuplicateCheck
    decision: Decision
    stages: list
    file_hash: str = ""      # sha256 of the PDF bytes; lets a re-run of the same file be recognised

    def to_db_row(self) -> dict:
        return {
            "file_hash": self.file_hash,
            "invoice_id": self.invoice_id,
            "filename": self.filename,
            "processed_at": self.processed_at,
            "vendor_name": self.extracted.vendor_name_raw,
            "invoice_number": self.extracted.invoice_number,
            "invoice_date": self.extracted.invoice_date,
            "po_number": self.extracted.po_number,
            "total": self.extracted.total,
            "po_amount": self.po_match.po_amount,
            "decision_status": self.decision.status,
            "decision_reasons": " | ".join(self.decision.reasons),
            "confidence": self.decision.confidence,
        }
