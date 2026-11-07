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

python sample_invoices/generate_samples.py   # (re)generates the 10 sample test PDFs
streamlit run app.py
```

Open the URL Streamlit prints (usually `http://localhost:8501`). In the sidebar
you can inspect the vendor master / PO dataset. In the main panel, choose
**Use a sample invoice** and pick one from the dropdown — each has a one-line
description of what it's testing — then click **Run processing pipeline**.

Optional: to turn on the AI step (see *AI assist* below), set
`ANTHROPIC_API_KEY` before launching (locally, an environment variable; on
Streamlit Cloud, a secret). Without it the app runs fully deterministically
and still passes all tests and edge cases.

### Running the test suite
```bash
pytest tests/ -v
```
86 unit + integration tests: the tolerance-band math and fuzzy matching in
isolation, 9 regression tests locked in from real-world invoices that broke
earlier versions of extraction (see *Extraction hardening* below), the full
pipeline against every sample invoice (happy path + edge cases), per-visitor
isolation, bring-your-own vendor/PO data, re-runs and the human review
override (see *Review queue*), the guarded AI step with a stubbed model (no
network, no key needed), the GST/currency checks, the exports, the
reviewer alerts (network stubbed), and the REST API (skipped if FastAPI is not installed).

## What Zamp is testing, and how this submission addresses it

| What's graded | Where it's addressed |
|---|---|
| Process actually runs end-to-end on real input | `app.py` + `core/pipeline.py` — nothing is mocked; every sample PDF is really parsed, really matched, really scored |
| Explainable decisions | Every `Decision` carries a `reasons` list built from the exact rule that fired — shown in both the live run view and the audit JSON |
| Edge cases handled deliberately, not with errors | See *Edge cases* below — each one produces a specific, reasoned outcome, never a crash or a generic error |
| Intuitive UI + live run view + dashboard | `app.py`: stage-by-stage live view, color-coded by status, plus a full dashboard tab (history table, decision breakdown chart, drill-down) |
| Judgment on AI vs. deterministic logic | AI only *reads* hard documents, behind a key, a spending cap and a check that every value it returns appears in the document; all financial math and the final decision are plain Python, always reproducible — see *AI assist* |
| Robust to real-world input, not just synthetic samples | Extraction was hardened against real third-party invoices during development — see *Extraction hardening* below |
| A decision that doesn't dead-end | **Needs Review** is a working queue: a person approves or rejects it, the system's own call and the human's call are both kept in the audit trail — see *Review queue* below |
| Safe to share as a live link | Every visitor gets a private workspace (own history, own vendor/PO data), so strangers testing the link never see or break each other's runs — see *Per-visitor workspaces* below |

## Architecture

```
PDF in → [1] Accept → [2] Extract text (digital layer, OCR fallback if none)
       → [3] Parse fields (regex, deterministic) → [AI assist only if low confidence / key field missing]
       → [4] Completeness check → [5] GST & currency checks (GSTIN checksum, tax type, currency)
       → [6] Vendor validation (fuzzy match vs. approved master)
       → [7] PO matching (exact → fuzzy → vendor+amount fallback, split-PO aware)
       → [8] Financial validation (tolerance bands, deterministic)
       → [9] Duplicate detection (vendor+invoice# and vendor+amount+date fingerprints)
       → [10] Decision engine (rule-based) → [11] Audit trail (SQLite) → Dashboard / exports
```

**Modules** (`core/`):
- `models.py` — the dataclasses every stage passes between each other (the contract)
- `extraction.py` — stages 2-3: pdfplumber → OCR fallback → regex field parsing → optional LLM assist
- `matching.py` — stages 5-6: vendor fuzzy match, PO exact/fuzzy/fallback match, split-PO balance tracking
- `compliance.py` — stage 5: GSTIN format + checksum, tax type vs. supply states, CGST = SGST, currency
- `ai_assist.py` — the guarded AI step (key, spending caps, grounding check, graceful failure)
- `export.py` — audit trail as CSV / Excel, and approved invoices as accounting-style "bill" JSON
- `notify.py` — optional webhook alert when an invoice needs review
- `api.py` (project root) — REST API over the same pipeline
- `rules.py` — stages 8-10: tolerance bands, duplicate rules, the decision policy (fully documented in its docstring)
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

## AI assist (reading hard documents, never deciding)

The pattern parser reads clean invoices on its own. The optional AI step
exists for the rest: a layout whose labels it doesn't know, or a messy scan.
It runs only when parse confidence is below 60% or a critical field is
missing, so a clean invoice never touches the network. Because the app is on
a public link and the text it reads is untrusted, the step is fenced in:

1. **Key and caps.** The key comes from `ANTHROPIC_API_KEY` (Streamlit Cloud
   secrets are exposed as environment variables). No key means no AI, and the
   sidebar and stage view say so. There is a per-session and a per-day call
   limit (`ZAMP_LLM_MAX_PER_SESSION`, `ZAMP_LLM_MAX_PER_DAY`).
2. **Gaps only.** The model can fill a field the parser left empty; it can
   never overwrite a value the parser already read.
3. **Grounding.** Every value must appear in the document's own text: a
   number must be one of the numbers printed on the page, a name or invoice
   number must be a substring, a date must be printable in a common format
   found in the text. A value the model invented, or one a prompt-injected
   instruction in the PDF tried to smuggle in, is discarded and reported.
4. **Strict parsing and graceful failure.** JSON only, typed and range-checked;
   a timeout, API error or malformed reply degrades to the deterministic
   result. The pipeline never crashes because the model was unavailable.
5. **Honest confidence.** AI-supplied fields are re-scored and discounted, and
   the run view lists exactly which fields the AI read.
6. **The decision is still plain Python.** The AI never sees the PO data and
   never approves, rejects or flags anything.

`sample_invoices/09_ai_unusual_layout_orbit.pdf` is built to show it: its
labels ("Document Ref.", "Dated", "Please remit") defeat the patterns, so
without a key it goes to **Needs Review** listing what is missing; with a key
the AI reads the fields, each is verified against the page, and it is
**Approved**. The tests prove all of this with a stubbed model.

## GST and currency checks (stage 5)

Deterministic checks on the supplier's invoice, added after testing real
Indian invoices that print GSTINs and CGST/SGST/IGST columns:

- **GSTIN format and check character.** The 15th character is computed from
  the first 14, so a mistyped or invented GSTIN almost always fails. (A pass
  does not prove registration; that needs the GST portal.)
- **State code** must be a real Indian state code.
- **Tax type vs. supply.** Supplier and place of supply in the same state
  means CGST + SGST; different states mean IGST. The real invoices include
  one of each (Tamil Nadu to Tamil Nadu, and Haryana to Tamil Nadu).
- **CGST total equals SGST total.**
- **Currency.** The PO data is in rupees; an invoice in USD/EUR/GBP is routed
  to review rather than compared against a rupee PO with a guessed conversion.

A failed check becomes a plain-English **Needs Review** reason; none of them
rejects an invoice by itself. All seven real invoices pass with no false flags.

## Exports and accounting hand-off

The dashboard has **Download audit trail (CSV)** and **Download Excel** (two
sheets: the full trail, and approved bills ready to post). Every approved
invoice, whether approved by the rules or by a reviewer, can be downloaded
as a `bill` JSON: vendor id and GSTIN, bill number and date, PO, amounts,
and who approved it. That payload is the hand-off a real deployment would
POST to an accounting system (Zoho Books, Tally, QuickBooks); no external
system is called here.

## Batch upload

Choose **Upload several PDFs (batch)** to process many invoices in one go. A
progress bar runs through them, one unreadable file never stops the rest, and
a summary table shows each file's decision and main reason. Anything that
needs a person lands in the Dashboard's review queue.

## REST API

`api.py` exposes the same pipeline as a REST service, so another system (an
email listener, an ERP, a workflow tool) can submit invoices without a person
using the UI. It calls the same core modules, so the API and the app can't
disagree.

```bash
pip install -r requirements-api.txt
uvicorn api:app --reload                 # interactive docs: http://localhost:8000/docs

curl -F "file=@sample_invoices/01_happy_path_nimbus.pdf" http://localhost:8000/v1/invoices
```

| Route | What it does |
|---|---|
| `POST /v1/invoices` | Upload a PDF; returns the decision, reasons, all 11 stages, extracted fields and (if approved) the bill JSON. `?use_ai=false` keeps it deterministic |
| `GET /v1/invoices`, `GET /v1/invoices/{id}` | History and one run, with the reviewer decision if any |
| `POST /v1/invoices/{id}/review` | A reviewer approves or rejects a Needs Review invoice |
| `POST /v1/vendors`, `POST /v1/purchase-orders` | Add reference data |
| `GET /health` | Liveness |

Safeguards: optional `API_KEY` (sent as `X-API-Key`), PDF-only with a 10 MB
cap, the client's file name is never used as a path, a corrupt PDF is a clean
422, and each `X-Client-Id` gets its own private audit trail and reference
data. It runs locally; the live Streamlit link serves the UI only, because
Streamlit Cloud hosts one app per repo.

## Reviewer alerts

Set `ALERT_WEBHOOK_URL` (a Slack-style incoming webhook; Teams and Discord have
compatible ones) and a short message is sent whenever an invoice lands in the
review queue: file name, vendor, total, the first reason and the invoice id.
Off by default. https only, short timeout, a failed send never affects the
pipeline, untrusted text is flattened and escaped (so a `<!channel>` hidden in
a PDF can't ping a workspace), and a daily cap (`ALERT_MAX_PER_DAY`) stops a
flood of uploads. The sidebar says when alerts are on.

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
- Multi-currency is detected and routed to review, not converted; line-item
  reconciliation and GST rate validation per HSN code are not attempted
- The AI step makes one call per document and is validated rather than
  retried; a production version would add retries with backoff and measure
  extraction accuracy against a labelled invoice set
- No real ERP connection: the bill JSON is the hand-off payload, not a live
  integration, and the REST API runs locally rather than on the live link.
  Email intake (forward an invoice to an inbox and have it processed) is the
  natural next step; it needs a mail account with OAuth and an always-on
  worker, which is beyond a one-file Streamlit deployment
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
   stage as it appears (extraction → GST check → vendor match → PO match →
   financial check → decision), point at the reasoning text.
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
