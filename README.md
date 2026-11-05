# Invoice Processing — From PDF to Decision
**Zamp AI Solutions Analyst Case Study · PS-1**

An AI-assisted, rules-governed pipeline that takes a vendor invoice PDF, extracts
its structured fields, validates it against an approved-vendor master and a
purchase-order dataset, and produces an explainable **Approved / Needs Review /
Rejected** decision with a full audit trail.

## Quickstart

```bash
pip install -r requirements.txt
# Tesseract OCR binary must be installed separately for the scanned-PDF edge case:
#   macOS:  brew install tesseract poppler
#   Ubuntu: sudo apt-get install tesseract-ocr poppler-utils

python sample_invoices/generate_samples.py   # (re)generates the 9 sample test PDFs
streamlit run app.py
```

Open the URL Streamlit prints (usually `http://localhost:8501`). In the sidebar
you can inspect the vendor master / PO dataset. In the main panel, choose
**Use a sample invoice** and pick one from the dropdown — each has a one-line
description of what it's testing — then click **Run processing pipeline**.

Optional: to exercise the LLM-assist path (used only when deterministic
extraction confidence is low), set `ANTHROPIC_API_KEY` before launching.
Without it, the app runs fully offline/deterministically and still passes all
tests and edge cases — see *Design decisions* below for why this is safe.

### Running the test suite
```bash
pytest tests/ -v
```
19 unit + integration tests: the tolerance-band math and fuzzy matching in
isolation, 4 regression tests locked in from a real-world invoice that broke
the first version of extraction (see *Extraction hardening* below), and the
full pipeline against every sample invoice (happy path + edge cases).

## What Zamp is testing, and how this submission addresses it

| What's graded | Where it's addressed |
|---|---|
| Process actually runs end-to-end on real input | `app.py` + `core/pipeline.py` — nothing is mocked; every sample PDF is really parsed, really matched, really scored |
| Explainable decisions | Every `Decision` carries a `reasons` list built from the exact rule that fired — shown in both the live run view and the audit JSON |
| Edge cases handled deliberately, not with errors | See *Edge cases* below — each one produces a specific, reasoned outcome, never a crash or a generic error |
| Intuitive UI + live run view + dashboard | `app.py`: stage-by-stage live view, color-coded by status, plus a full dashboard tab (history table, decision breakdown chart, drill-down) |
| Judgment on AI vs. deterministic logic | AI (optional LLM assist) only touches *document understanding* on low-confidence extractions; all financial math and the final decision are plain Python, always reproducible |
| Robust to real-world input, not just synthetic samples | Extraction was hardened against an actual real invoice during development — see *Extraction hardening* below |

## Architecture

```
PDF in → [1] Accept → [2] Extract text (digital layer, OCR fallback if none)
       → [3] Parse fields (regex, deterministic) → [LLM assist only if confidence < 60%]
       → [4] Completeness check → [5] Vendor validation (fuzzy match vs. approved master)
       → [6] PO matching (exact → fuzzy → vendor+amount fallback, split-PO aware)
       → [7] Financial validation (tolerance bands, deterministic)
       → [8] Duplicate detection (vendor+invoice# and vendor+amount+date fingerprints)
       → [9] Decision engine (rule-based) → [10] Audit trail (SQLite) → Dashboard
```

**Modules** (`core/`):
- `models.py` — the dataclasses every stage passes between each other (the contract)
- `extraction.py` — stages 2-3: pdfplumber → OCR fallback → regex field parsing → optional LLM assist
- `matching.py` — stages 5-6: vendor fuzzy match, PO exact/fuzzy/fallback match, split-PO balance tracking
- `rules.py` — stages 7-9: tolerance bands, duplicate rules, the decision policy (fully documented in its docstring)
- `database.py` — SQLite audit trail + PO consumption ledger
- `pipeline.py` — orchestrates all stages as a generator, so the UI's live view and the CLI/test harness share one code path

**Why this stack:** Streamlit gives an intuitive UI + dashboard in hours, not
days, with the live-stage view essentially free. pdfplumber + pytesseract
cover both digital and scanned PDFs without a paid OCR API. rapidfuzz handles
vendor/PO typos cheaply. SQLite needs zero setup and the database file itself
*is* a shareable audit artifact. All of this swaps cleanly for Postgres/React/
a cloud OCR API in a production rebuild — this is the right choice for a
one-week, single-user, locally-run deliverable.

**UI design:** the interface is themed around the thing it's replacing — a
paper accounting ledger — rather than a generic dashboard look: a dark
ledger-green background, a single gold accent reserved for the one thing that
matters per screen (the running stage, the final verdict stamp), and each
pipeline stage rendered as a row on a timeline connected by a single line as
it clears, so the live run reads like watching entries post to a ledger one
by one. The theme lives in `.streamlit/config.toml` plus a CSS block at the
top of `app.py`; the verdict ("Approved"/"Needs Review"/"Rejected") is
rendered as a stamp, echoing a physical AP workflow.

## Decision policy (the actual business logic)

- **Rejected**: duplicate of an already-processed invoice · vendor exists but
  is marked inactive · invoice amount exceeds the PO's *remaining* balance by
  more than 15% (too large to be rounding/freight)
- **Needs Review**: any critical field missing · vendor not in the master at
  all (could be legitimately new) · no PO match found · amount 2–15% over the
  remaining PO balance · overall extraction confidence below 50%
- **Approved**: approved vendor, PO matched, amount within 2% of the PO's
  remaining balance, no duplicate, all critical fields present

Tolerance bands (`SOFT_TOLERANCE_PCT = 2%`, `HARD_REJECT_PCT = 15%`) are
constants at the top of `core/rules.py` — a real deployment would make these
configurable per vendor category, but hardcoding them keeps the logic legible
for this exercise.

**Split-PO handling**: the audit trail tracks cumulative *non-rejected*
invoiced amount per PO. A second invoice against the same PO is validated
against the *remaining* balance, not the full PO amount — so a PO
legitimately split across multiple invoices is approved correctly, while a
genuine duplicate/overcharge still gets caught.

## Extraction hardening (found by testing a real invoice, not just samples)

Synthetic test PDFs are clean by construction — they use whatever label and
date format the generator script chose. The first version of extraction
quietly assumed every invoice would match those exact formats. Running an
actual third-party invoice through the pipeline during development exposed
two real bugs, both now fixed and locked in with regression tests:

- **Wrong total.** The old "total" regex matched the first bare number after
  the word *Total*, with no currency symbol required. On the real invoice, a
  courier/shipping footer line ("Total: 2 Item(s) in this shipment") matched
  *before* the real amount did, so the system read the total as `2`. Fix:
  `core/extraction.py` now tries an ordered list of labeled patterns (`Grand
  Total`, `Total`, `Amount Payable`, `Amount`), **each of which requires a
  currency symbol** (₹/Rs/INR/$) — a bare number is never trusted as money.
  If no labeled total matches at all, the amount is *derived* from
  subtotal + tax instead of left blank, and that derivation is flagged in the
  confidence score so it never looks as certain as a directly-read total.
- **Missing date.** The old date parser only accepted a short list of exact
  numeric formats (`DD-MM-YYYY`, etc.). The real invoice wrote its date as
  "October 03, 2026" — a perfectly normal format no numeric regex covers.
  Fix: `_parse_date()` now falls back to `python-dateutil`'s fuzzy parser
  after the strict formats fail, so spelled-out and otherwise unusual (but
  unambiguous) date formats still resolve.

Both bugs are the reason the extraction-confidence score exists at all: a
missing or inferred field doesn't crash the pipeline or silently guess — it
lowers confidence, which routes the invoice to **Needs Review** instead of a
wrong **Approved**. The 4 regression tests in `tests/test_pipeline.py`
(`test_spelled_out_date_is_parsed`, `test_amount_label_used_when_no_total_label_present`,
`test_non_currency_total_line_is_not_mistaken_for_the_amount`,
`test_total_derived_from_subtotal_and_tax_when_no_label_found`) use the exact
text patterns from that real invoice, so this class of bug can't silently
come back.

## Edge cases built and tested

| # | File | Scenario | Expected result |
|---|---|---|---|
| 1 | `02_edge_duplicate_coral.pdf` | Same invoice uploaded twice | 1st run **Approved**, 2nd run **Rejected** (duplicate) |
| 2 | `03_edge_amount_mismatch_vertex.pdf` | Invoice 8% over matched PO | **Needs Review** (above soft tolerance, below hard reject) |
| 3 | `04_edge_scanned_apex.pdf` | No digital text layer, noisy scan | OCR fallback triggers, still extracts correctly → **Approved** |
| 4 | `05_edge_unknown_vendor.pdf` | Vendor not in approved master | **Needs Review** |

**Bonus scenarios** (built + unit-tested, beyond the required 2-4):
- `06_bonus_split_po_part*.pdf` — PO split across two invoices, both **Approved** via remaining-balance tracking
- `07_bonus_inactive_vendor_meridian.pdf` — vendor exists but inactive → hard **Rejected**
- `08_bonus_missing_fields_sterling.pdf` — invoice # and PO ref both missing, PO still found via vendor+amount fallback → **Needs Review** with a precise "what's missing" reason

Every scenario's expected outcome is codified as a pytest assertion in
`tests/test_pipeline.py`, not just eyeballed in the UI.

## Known limitations / what I'd build next

- Tolerance bands are global constants, not per-vendor-category
- Vendor/PO matching uses a single fuzzy-match pass; a production system
  would want a human-in-the-loop queue for borderline fuzzy matches (85-92%)
  rather than a hard cutoff
- LLM assist is single-shot with no retry/validation loop; a production
  version would validate the LLM's JSON against a schema and retry on failure
- No multi-line-item / bundled-line-item parsing — the pipeline reads totals,
  not itemized reconciliation
- SQLite is fine for a single-user demo; concurrent AP-team usage would need
  Postgres and row-level locking around PO balance updates
- Total/date extraction now covers the real-world formats seen so far
  (multiple total labels, spelled-out dates, derived totals), but it's still
  pattern-based — a genuinely novel layout (e.g. a total with no currency
  symbol anywhere on the page) will correctly degrade to lower confidence /
  Needs Review rather than silently guess, but a human still has to resolve it

## Demo video script (5 minutes)

1. **(30s)** One-sentence framing: what the problem is, what you built, the
   three-way decision output.
2. **(60s)** Happy path: run `01_happy_path_nimbus.pdf` live, narrate each
   stage as it appears (extraction → vendor match → PO match → financial
   check → decision), point at the reasoning text.
3. **(90s)** Edge case 1 — duplicate: run `02_edge_duplicate_coral.pdf`, show
   Approved; run it again, show it flip to Rejected, point at the exact
   "duplicate of invoice X" reason.
4. **(60s)** Edge case 2 — your choice of amount mismatch or scanned/OCR;
   narrate *why* the system treats it differently from a hard reject (explain
   the tolerance bands).
5. **(45s)** Dashboard tab: show the history table, decision breakdown chart,
   and that every decision has a drill-down audit record.
6. **(15s)** Close: mention the deterministic-vs-AI design choice and one
   thing you'd build next (from *Known limitations* above).

Keep test inputs ready in `sample_invoices/` beforehand — don't generate them
live on camera.

See `EXPLAINER.md` for a plain-language walkthrough of the whole system and
a condensed talking-points version of this script — written specifically as
demo-video prep.
