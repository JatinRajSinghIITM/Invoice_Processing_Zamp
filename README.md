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
35 unit + integration tests: the tolerance-band math and fuzzy matching in
isolation, 9 regression tests locked in from real-world invoices that broke
earlier versions of extraction (see *Extraction hardening* below), the full
pipeline against every sample invoice (happy path + edge cases), and a
second file covering per-visitor isolation, bring-your-own vendor/PO data,
re-runs, and the human review override (see *Review queue* below).

## What Zamp is testing, and how this submission addresses it

| What's graded | Where it's addressed |
|---|---|
| Process actually runs end-to-end on real input | `app.py` + `core/pipeline.py` — nothing is mocked; every sample PDF is really parsed, really matched, really scored |
| Explainable decisions | Every `Decision` carries a `reasons` list built from the exact rule that fired — shown in both the live run view and the audit JSON |
| Edge cases handled deliberately, not with errors | See *Edge cases* below — each one produces a specific, reasoned outcome, never a crash or a generic error |
| Intuitive UI + live run view + dashboard | `app.py`: stage-by-stage live view, color-coded by status, plus a full dashboard tab (history table, decision breakdown chart, drill-down) |
| Judgment on AI vs. deterministic logic | AI (optional LLM assist) only touches *document understanding* on low-confidence extractions; all financial math and the final decision are plain Python, always reproducible |
| Robust to real-world input, not just synthetic samples | Extraction was hardened against real third-party invoices during development — see *Extraction hardening* below |
| A decision that doesn't dead-end | **Needs Review** is a working queue: a person approves or rejects it, the system's own call and the human's call are both kept in the audit trail — see *Review queue* below |
| Safe to share as a live link | Every visitor gets a private workspace (own history, own vendor/PO data), so strangers testing the link never see or break each other's runs — see *Per-visitor workspaces* below |

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
- `database.py` — SQLite audit trail + PO consumption ledger + reviewer overrides
- `workspace.py` — per-visitor workspace (own SQLite file, own vendor/PO CSVs, own uploads folder)
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

A second round, using seven real invoices from two vendors, found four more
and added regression tests for each:

- **Vendor read as the word "From"** when an invoice labels the block
  "From:" / "Sold by" — label-only lines are now skipped.
- **Tax read as 7.0** from a GST registration number — tax now needs a
  currency symbol like every other money field.
- **No total found** on a layout that prints `₹8,645.00 (incl. tax)` on its
  own line — added a tax-inclusive total pattern, and subtotal now ignores
  that line.
- **Ambiguous dates** like `05/10/2026` (5 Oct or 10 May?). The order is
  inferred from other dates in the same document (e.g. a "13 Oct" nearby
  proves day-first); if nothing settles it, DD-MM is assumed, the parse stage
  shows a visible warning note, and confidence is reduced — the invoice is
  never silently guessed.

Both bugs from the first round are the reason the extraction-confidence score exists at all: a
missing or inferred field doesn't crash the pipeline or silently guess — it
lowers confidence, which routes the invoice to **Needs Review** instead of a
wrong **Approved**. The regression tests in `tests/test_pipeline.py`
(`test_spelled_out_date_is_parsed`, `test_amount_label_used_when_no_total_label_present`,
`test_non_currency_total_line_is_not_mistaken_for_the_amount`,
`test_total_derived_from_subtotal_and_tax_when_no_label_found`) use the exact
text patterns from that real invoice, so this class of bug can't silently
come back.

## Per-visitor workspaces

A public link means strangers use the same running app. With one shared
database, visitor B's first upload of a sample invoice would be rejected as a
"duplicate" of visitor A's run — a bug no amount of correct logic can fix.
So each browser session gets its own workspace folder under the system temp
directory (`core/workspace.py`): a private SQLite audit trail, a private copy
of `vendors.csv` / `purchase_orders.csv` seeded from `data/`, and a private
uploads folder. Nothing a visitor does — processing, adding vendors,
resetting — is visible to anyone else. Stale workspaces are purged after 12
hours. The CLI and tests default to the repo's `data/` folder, so they behave
as before.

## Bring your own vendors and POs

The sidebar has **Add a vendor** and **Add a purchase order** forms, so a
reviewer can test the system with their own invoices: a vendor that was
"unknown" becomes known, and the same file re-run flows to **Approved**.
Input is validated (blank / duplicate vendor name, duplicate PO number,
unknown vendor id, non-positive amount all give a plain error). Re-running the
*same file* while its earlier run is still waiting for review replaces that
run instead of being flagged as its own duplicate; re-submitting an already
Approved invoice is still correctly caught as a duplicate.

## Review queue (human-in-the-loop)

**Needs Review** used to be a dead end. Now the Dashboard has a *Review queue*
listing every invoice waiting on a person, with the reasons the system gave,
and an Approve / Reject control with an optional note (also available right
under the result on the Process tab). Design choices:

- The system's decision is **never overwritten**. A reviewer's call is stored
  beside it (`human_decision`, `reviewer_note`, `reviewed_at`), and the
  dashboard shows both columns — the audit trail answers "what did the system
  decide, and what did a person later decide?"
- Dashboard counts and charts use the *effective* status (human decision if
  present, else the system's).
- Rejecting in review releases the PO balance the invoice was holding;
  a human-rejected invoice no longer blocks a legitimate resubmission.
- Only invoices the system marked Needs Review can be reviewed, and only once.

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
  would send borderline fuzzy matches (85-92%) to the review queue rather
  than use a hard cutoff. The review queue itself exists, but only for
  invoices the system marked Needs Review
- Workspaces live in the host's temp folder, so on a free cloud host they
  disappear when the app restarts — fine for a demo, not an AP system of record
- One invoice at a time; no batch upload or CSV export of the audit trail
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
4. **(60s)** Edge case 2 — unknown vendor → **Needs Review**; then approve it
   from the review panel with a note (or add the vendor + PO in the sidebar
   and re-run to Approved). Narrate that the system's call is preserved.
5. **(45s)** Dashboard tab: show the review queue, history table with both the
   system and reviewer decisions, decision breakdown chart, and the drill-down
   audit record.
6. **(15s)** Close: mention the deterministic-vs-AI design choice and one
   thing you'd build next (from *Known limitations* above).

Keep test inputs ready in `sample_invoices/` beforehand — don't generate them
live on camera.

See `EXPLAINER.md` for a plain-language walkthrough of the whole system and
a condensed talking-points version of this script — written specifically as
demo-video prep.
