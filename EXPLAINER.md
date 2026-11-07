# What You Actually Built (explained like you're telling a friend)

Use this to get comfortable enough to talk about the project without
looking at notes. The technical README is for anyone who wants to read
the code; this is for *you*, to internalize the story.

---

## 1. The problem, in one paragraph

Imagine you work in Accounts Payable at a mid-size company. Every day,
vendor invoices land in your inbox as PDFs — some clean, some scanned on
a bad office printer, some missing a field, some vendors you've never
heard of. For each one, you have to: read it, find the matching purchase
order in a spreadsheet, check the numbers line up, make sure the vendor
is actually approved, make sure you haven't already paid this exact
invoice before, and then decide — pay it, or flag it for a human to
look closer. Do that 200 times a month and you understand why mistakes
happen on a tired Friday afternoon. That's the job you automated.

## 2. What you built, in one sentence

A program that takes an invoice PDF, reads it, checks it against your
company's own records, and hands back one of three verdicts —
**Approved**, **Needs Review**, or **Rejected** — along with a plain-English
explanation of *why*, every single time.

## 3. Walk through what happens to one invoice (the story version)

Say a PDF lands in the system. Here's its journey, stage by stage —
this is literally what the "live run view" in your app shows happening:

1. **"I got the file."** Nothing clever yet — just confirming the PDF
   arrived.
2. **"Let me read the text."** Most invoices have selectable text
   (like a Word doc). If the PDF is actually a scanned image with no
   real text — just a picture of an invoice — the program switches to
   OCR (optical character recognition: software that "looks" at the
   image and guesses what the letters are, the same tech phone scanner
   apps use).
3. **"Let me pull out the important bits."** Vendor name, invoice
   number, date, PO number, and the total amount. This is done with
   *pattern matching* — basically, "find a line that says 'Total:' and
   grab the number after it." It's not AI here on purpose (more on
   that in section 5).
4. **"Did I get everything I need?"** If something critical is
   missing — no date, no total — it flags that honestly instead of
   making something up.
5. **"Do we actually know this vendor?"** It checks the name against
   your company's approved vendor list. It's forgiving of typos
   (a vendor called "Nimbus Office Supplies Pvt Ltd" still matches if
   someone typed "Nimbus Office Supplies Pvt. Ltd.") but it won't
   invent a match for a name it's never seen.
6. **"Which purchase order is this for?"** It tries to match the
   invoice to a PO on file — first by exact PO number, then by a fuzzy
   guess if there's a typo, then (if no PO number was even given) by
   figuring out which PO makes sense given the vendor and the amount.
7. **"Does the money add up?"** This is pure arithmetic — is the
   invoice total close enough to what the PO says it should be? Not
   AI. Just subtraction and a percentage check.
8. **"Have we seen this exact invoice before?"** Checks its own
   history. If someone (accidentally or on purpose) submits the same
   invoice twice, it catches it.
9. **"Here's my verdict."** Approved / Needs Review / Rejected, with
   the actual reasons listed — never just a score with no explanation.
10. **"I'm writing this down."** Every run gets logged to a database
    (the audit trail) so there's a permanent record of what was
    decided and why — this is what the Dashboard tab shows you.

## 4. The three verdicts, explained like a bouncer at a club

- **Approved** = "Everyone checks out, you're on the list, the math is
  right, come on in." No human needed.
- **Needs Review** = "Something's a little off — I can't be sure, so
  I'm not going to decide this myself. A human should take one more
  look." (Missing info, unrecognized vendor, amount slightly off.)
- **Rejected** = "No. This is wrong in a way I'm confident about."
  (Same invoice submitted twice, or the vendor is specifically marked
  as not-currently-approved, or the amount is wildly off.)

The key design idea: **the system is honest about what it doesn't
know.** It would be easy to make it always say "Approved" or always
guess — the harder and more valuable thing is teaching it *when to
say "I'm not sure."*

## 5. Why AI only does part of the job

This is probably the single best thing to say in your interview, so
understand it properly:

**AI is good at reading messy, unstructured things** — a weirdly
formatted PDF, a scanned image, handwriting-adjacent text. That's a
"fuzzy" problem, so AI (or OCR) is the right tool.

**AI is bad at being trusted with money decisions**, because it can
be confidently wrong, and you can't always tell when. So the actual
*decision* — does this amount match, is this a duplicate, is this
vendor approved — is done with plain, boring, 100%-predictable Python
math and if/else logic. No AI involved in that part at all. If someone
asks "why did this get rejected," you can point at the exact line of
code that made that call — not "the model decided."

So: **AI reads the document. Rules make the decision.** That split is
deliberate, and it's exactly what the case study brief asked for.

## 6. Why these specific tools (plain-English version)

- **Streamlit** — lets you build a real, working web app (upload a
  file, click a button, see results) in Python, without having to
  separately build a frontend and backend. Perfect for a one-week
  solo project.
- **pdfplumber** — reads text out of normal PDFs.
- **pytesseract (+ poppler)** — the OCR engine, for when a PDF is just
  a picture with no real text underneath.
- **rapidfuzz** — handles typos when matching vendor names or PO
  numbers, so a small spelling mistake doesn't break everything.
- **SQLite** — a tiny, file-based database. No server to install,
  and the database file itself is literally your audit trail — you
  could hand someone that one file and they'd have the full history.
- **Plotly** — draws the charts on the Dashboard tab.

## 7. The edge cases, and why each one is a real test of judgment

- **Duplicate invoice** — tests whether the system remembers what it's
  already seen. Submit the same invoice twice: first time Approved,
  second time Rejected, with the exact reason ("this is a duplicate of
  invoice X").
- **Amount doesn't quite match the PO** — tests whether the system can
  tell the difference between "close enough" (small rounding/freight
  difference — flag for review, don't panic) and "way off" (reject
  outright). This is the tiered-tolerance logic.
- **Scanned / low-quality PDF** — tests the OCR fallback. No real text
  in the file at all — the system has to "look" at the image instead
  of reading it, which is strictly harder and comes with lower
  confidence.
- **Unknown vendor** — tests that the system doesn't just trust
  whatever name is printed on the page. If it's not on the approved
  list, it won't auto-approve, no matter how clean the PDF looks.
- *(Bonus)* **A PO split across two invoices** — tests whether the
  system understands that a purchase order isn't "used up" after one
  invoice; it tracks the remaining balance properly.
- *(Bonus)* **Inactive vendor** — tests a *harder* rejection: the
  vendor technically exists in the records, but they've been marked
  as not currently approved (maybe they were dropped, or are under
  review) — different from "never heard of them."

## 8. The three upgrades after the first version (and why they matter)

- **Every visitor gets their own private copy.** The app is on a public link,
  so strangers (including the reviewers) will use it at the same time. With one
  shared memory, the second person to upload a sample invoice would be told
  it's a duplicate of the first person's. So each browser session gets its own
  database and its own vendor/PO lists. Say it as: "I realised a shared link
  means shared state, so I isolated it per visitor."
- **"Needs Review" is now a real queue, not a dead end.** A person can open the
  queue, read the system's reasons, and Approve or Reject with a note. The
  important bit: the system's original verdict is never erased — both the
  machine's call and the human's call stay in the audit trail.
- **You can bring your own vendors and POs.** Add a vendor and a PO in the
  sidebar, re-run the same invoice, and watch it go from Needs Review to
  Approved. That's the best way to show the system really uses the data.

Also worth one sentence: it was tested on real invoices, and the failures it
found (vendor read as "From", GST number read as tax, an ambiguous 05/10 date)
were fixed and locked in with tests. An ambiguous date is never silently
guessed — it shows a warning and lowers confidence.

---

## 9. The second round of upgrades (and why they matter)

- **The AI step is real now, but fenced in.** When the pattern reader can't
  make sense of a document, a language model reads it. But it only fills
  blanks (never overwrites), every value it gives must actually appear in the
  document (so it can't invent a total), it has a spending cap, and if it
  fails the system just carries on without it. The approve/reject decision is
  still plain Python. Say it as: "AI reads, rules decide, and I don't trust the
  AI's answer unless I can find it on the page."
- **GST checks.** Indian invoices carry a GSTIN with a built-in check character,
  and the right tax type depends on whether supplier and buyer are in the same
  state. The system checks both, deterministically. A wrong GSTIN doesn't
  reject the invoice; it sends it to a person.
- **Exports.** Download the audit trail as CSV/Excel, and each approved
  invoice as a "bill" JSON, which is what you would send to an accounting
  system. It shows what happens after approval.
- **Batch upload.** Drop many PDFs and get one summary table.
- **A REST API.** A second way in: another program sends a PDF and gets the
  decision back as JSON. It is the same engine, so the API and the app always
  agree. It runs on your computer (Streamlit Cloud only hosts the UI).
- **Alerts.** If you set a Slack-style webhook, a short message is sent
  when an invoice needs a person. Off unless configured.

---

## 10. What to actually say in your video (cheat sheet)

You don't need a script — just hit these beats in your own words:

1. "This solves [problem] — AP teams manually check hundreds of
   invoices a month against purchase orders, and it's slow and
   error-prone."
2. "Here's the happy path." *(run `01_happy_path_nimbus.pdf`, narrate
   each stage as it lights up)* "Approved — vendor's recognized, PO
   matches, amount's right, no duplicate."
3. "Now watch what happens with a messier case." *(pick ONE edge
   case — duplicate is the most visually satisfying: run it twice,
   watch it flip from Approved to Rejected)*
4. "The important design decision here is that AI only reads the
   document — the actual approve/reject logic is deterministic Python,
   so every decision is explainable and reproducible." *(this is your
   strongest line — say it clearly)*
5. "Needs Review isn't a dead end — a person can resolve it. I'll approve
   this one with a note, and notice the system's original call is kept next
   to the human's." *(use the review panel)*
6. "And here's the dashboard — every invoice ever processed, with the
   full reasoning stored for audit." *(switch to Dashboard tab)*

That's it. Confident, plain language, don't over-explain the code —
explain the *decisions* you made and why.

---

*Keep this file for yourself — it's not meant to be code or a
deliverable, just your own prep notes.*
