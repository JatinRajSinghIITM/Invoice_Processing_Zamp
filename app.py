"""
Streamlit UI — invoice upload + live run view + processing history dashboard.

Two tabs:
  - Process Invoice: upload (or pick a sample) a PDF, watch each pipeline
    stage execute live, see the final decision and reasoning.
  - Dashboard: full processing history / audit trail, status breakdown,
    and drill-down into any past run — this is what satisfies "show history,
    status, and outputs across runs" from the candidate guide.
"""
import html
import os
import time
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.express as px

from core import ai_assist, database, export, matching, notify
from core.pipeline import run_pipeline
from core.workspace import new_session_workspace, reset_reference_data

ROOT = Path(__file__).parent
SAMPLES_DIR = ROOT / "sample_invoices"

st.set_page_config(page_title="Invoice Processing — From PDF to Decision",
                    page_icon="📒", layout="wide")

# Streamlit Cloud exposes secrets as environment variables already; this just
# makes sure the key is picked up if only st.secrets has it. The key is never
# displayed or logged anywhere in the app.
try:
    if "ANTHROPIC_API_KEY" in st.secrets:
        os.environ.setdefault("ANTHROPIC_API_KEY", str(st.secrets["ANTHROPIC_API_KEY"]))
except Exception:
    pass  # no secrets configured: the app runs fully deterministic

# Every browser session gets its own private workspace (audit trail, vendor
# master, PO dataset, uploads), so visitors to the deployed app never see or
# disturb each other's runs.
if "ws" not in st.session_state:
    st.session_state["ws"] = new_session_workspace()
ws = st.session_state["ws"]
database.init_db(ws)

# Status palette reads like ink-stamp colors (approve/review/reject), used
# consistently everywhere — stage markers, the decision stamp, dashboard
# charts — so the eye learns it once and reads it everywhere after.
STATUS_COLOR = {"Approved": "#3E8E63", "Needs Review": "#C98A2B", "Rejected": "#B5452E"}
STAGE_MARK = {"ok": "✓", "warning": "!", "fail": "✕"}

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap');

:root {
  --ink: #101B16;
  --panel: #16221C;
  --paper: #EDEAE0;
  --rule: #33463B;
  --muted: #9AA99E;
  --brass: #C9A227;
  --approved: #3E8E63;
  --review: #C98A2B;
  --rejected: #B5452E;
  --serif: 'Source Serif 4', Georgia, serif;
  --mono: 'IBM Plex Mono', 'Courier New', monospace;
  --sans: 'IBM Plex Sans', sans-serif;
}

.ledger-header { border-bottom: 1px solid var(--rule); padding-bottom: 18px; margin-bottom: 8px; }
.ledger-title { font-family: var(--serif); font-size: 2.1rem; font-weight: 600; color: var(--paper); line-height: 1.2; margin: 0; }
.ledger-subtitle { font-family: var(--sans); font-size: 0.92rem; color: var(--muted); margin-top: 6px; }

.section-label { font-family: var(--mono); font-size: 0.82rem; color: var(--muted); letter-spacing: 0.02em;
                  border-bottom: 1px solid var(--rule); padding-bottom: 6px; margin-bottom: 14px; }

/* --- stage timeline: a ledger spine, not floating cards --- */
.ledger-row { display: flex; gap: 14px; position: relative; padding: 7px 0; }
.ledger-row:not(:last-child)::after {
  content: ''; position: absolute; left: 12px; top: 30px; bottom: -7px; width: 2px; background: #3F5A4C;
}
.ledger-dot {
  width: 26px; height: 26px; min-width: 26px; border-radius: 50%; display: flex; align-items: center;
  justify-content: center; font-family: var(--mono); font-size: 13px; font-weight: 600;
  border: 1px solid var(--rule); background: var(--panel); z-index: 1;
}
.ledger-dot.ok { border-color: var(--approved); color: var(--approved); }
.ledger-dot.warning { border-color: var(--review); color: var(--review); }
.ledger-dot.fail { border-color: var(--rejected); color: var(--rejected); }
.ledger-body { padding-top: 1px; }
.ledger-stage-name { font-family: var(--mono); font-weight: 600; font-size: 0.9rem; color: var(--paper); }
.ledger-summary { font-family: var(--sans); font-size: 0.86rem; color: var(--paper); opacity: 0.88; margin-top: 1px; }
.ledger-detail { font-family: var(--mono); font-size: 0.76rem; color: var(--muted); margin-top: 3px; }

/* --- the decision stamp: this is the one bold moment on the page --- */
.stamp {
  display: inline-block; padding: 12px 26px; border: 3px double var(--approved); color: var(--approved);
  font-family: var(--mono); font-weight: 600; font-size: 1.25rem; letter-spacing: 0.1em;
  transform: rotate(-1.5deg); background: rgba(62,142,99,0.07); margin: 6px 0 14px 0;
}
.stamp.review { border-color: var(--review); color: var(--review); background: rgba(201,138,43,0.07); }
.stamp.rejected { border-color: var(--rejected); color: var(--rejected); background: rgba(181,69,46,0.07); }

.reason-line { font-family: var(--sans); font-size: 0.92rem; padding: 3px 0 3px 14px; border-left: 2px solid var(--rule); margin-bottom: 4px; }

.review-outcome { font-family: var(--sans); font-size: 0.92rem; padding: 8px 14px; margin: 10px 0 4px 0;
                  border-left: 3px solid var(--brass); background: rgba(201,162,39,0.08); }
.review-outcome.approved { border-left-color: var(--approved); }
.review-outcome.rejected { border-left-color: var(--rejected); }

/* --- dashboard summary: a report header, not KPI cards --- */
.ledger-metrics { display: flex; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); margin: 6px 0 22px 0; }
.ledger-metric { flex: 1; text-align: center; padding: 14px 8px; border-right: 1px solid var(--rule); }
.ledger-metric:last-child { border-right: none; }
.ledger-metric .num { font-family: var(--mono); font-size: 1.85rem; font-weight: 600; color: var(--paper); }
.ledger-metric .lbl { font-family: var(--sans); font-size: 0.78rem; color: var(--muted); margin-top: 2px; }
.ledger-metric.c-approved .num { color: var(--approved); }
.ledger-metric.c-review .num { color: var(--review); }
.ledger-metric.c-rejected .num { color: var(--rejected); }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="ledger-header">
  <p class="ledger-title">Invoice processing — from PDF to decision</p>
  <p class="ledger-subtitle">Built for Zamp's AI Solutions Analyst case study, problem PS-1.</p>
</div>
""", unsafe_allow_html=True)

def esc(value) -> str:
    """Everything that came out of a PDF is untrusted text. It is HTML-escaped
    before being placed in markup, because this app accepts arbitrary uploads."""
    return html.escape("" if value is None else str(value))


def money(value) -> str:
    return "—" if value is None or pd.isna(value) else f"₹{float(value):,.2f}"


def flash(kind: str, message: str) -> None:
    """Show a message on the next run (forms rerun the page, which would
    otherwise swallow a message shown at submit time)."""
    st.session_state["flash"] = (kind, message)


def stage_row_html(stage) -> str:
    num, _, name = stage.stage.partition(". ")
    detail_html = f'<div class="ledger-detail">{esc(stage.detail)}</div>' if stage.detail else ""
    return (f'<div class="ledger-row">'
            f'<div class="ledger-dot {stage.status}">{STAGE_MARK[stage.status]}</div>'
            f'<div class="ledger-body">'
            f'<span class="ledger-stage-name">{esc(num)}. {esc(name)}</span>'
            f'<div class="ledger-summary">{esc(stage.summary)}</div>'
            f'{detail_html}'
            f'</div></div>')


def review_panel(invoice_id: str, key: str) -> None:
    """Approve / reject controls for an invoice the system sent to a human."""
    note = st.text_input("Reviewer note (optional)", key=f"note_{key}",
                         placeholder="Why you are approving or rejecting")
    col_ok, col_no = st.columns(2)
    decision = None
    if col_ok.button("Approve", key=f"approve_{key}", type="primary", width='stretch'):
        decision = "Approved"
    if col_no.button("Reject", key=f"reject_{key}", width='stretch'):
        decision = "Rejected"
    if decision:
        try:
            database.record_review(invoice_id, decision, note, ws)
            flash("success", f"{invoice_id} marked {decision} by reviewer.")
        except ValueError as err:
            flash("error", str(err))
        st.rerun()


def render_result(result) -> None:
    status = result.decision.status
    stamp_class = {"Approved": "", "Needs Review": "review", "Rejected": "rejected"}[status]
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(f'<div class="stamp {stamp_class}">{status.upper()}</div>', unsafe_allow_html=True)

    for reason in result.decision.reasons:
        st.markdown(f'<div class="reason-line">{esc(reason)}</div>', unsafe_allow_html=True)

    if status == "Needs Review":
        run = database.get_run(result.invoice_id, ws)
        if run and run["human_decision"]:
            note = f" Note: {esc(run['reviewer_note'])}" if run["reviewer_note"] else ""
            st.markdown(f'<div class="review-outcome {run["human_decision"].lower()}">'
                        f'Resolved by a reviewer: {esc(run["human_decision"])}.{note}</div>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<p class="section-label" style="margin-top:22px;">Reviewer decision</p>',
                        unsafe_allow_html=True)
            review_panel(result.invoice_id, key=f"run_{result.invoice_id}")

    st.markdown('<p class="section-label" style="margin-top:22px;">Extracted fields</p>', unsafe_allow_html=True)
    e = result.extracted
    fcol1, fcol2, fcol3 = st.columns(3)
    fcol1.metric("Vendor", e.vendor_name_raw or "—")
    fcol1.metric("Invoice #", e.invoice_number or "—")
    fcol2.metric("Invoice Date", e.invoice_date or "—")
    fcol2.metric("PO Number", e.po_number or "—")
    fcol3.metric("Total", f"₹{e.total:,.2f}" if e.total else "—")
    fcol3.metric("Extraction confidence", f"{e.extraction_confidence:.0%}")

    detail_bits = []
    if e.gstin:
        detail_bits.append(f"Supplier GSTIN {e.gstin}")
    if e.currency and e.currency != "INR":
        detail_bits.append(f"Currency {e.currency}")
    if e.ai_fields:
        detail_bits.append("AI-assisted fields: " + ", ".join(ai_assist.FIELD_LABELS[f] for f in e.ai_fields)
                           + " (each verified against the document text)")
    if detail_bits:
        st.caption("  |  ".join(detail_bits))

    run_row = database.get_run(result.invoice_id, ws)
    bill = export.bill_json_bytes(run_row) if run_row else None
    if bill:
        st.download_button("Download bill JSON (accounting hand-off)", bill,
                           file_name=f"bill_{result.invoice_id}.json", mime="application/json",
                           key=f"bill_{result.invoice_id}")

    with st.expander("Full audit record (JSON)"):
        st.json({
            "invoice_id": result.invoice_id,
            "extracted": vars(result.extracted),
            "vendor_match": vars(result.vendor_match),
            "po_match": vars(result.po_match),
            "duplicate_check": vars(result.duplicate_check),
            "decision": vars(result.decision),
        })


# ---------------------------------------------------------------- Sidebar
with st.sidebar:
    pending_flash = st.session_state.pop("flash", None)
    if pending_flash:
        getattr(st, pending_flash[0])(pending_flash[1])

    st.markdown('<p class="section-label">Reference data</p>', unsafe_allow_html=True)
    vendors_df = matching.load_vendors(ws)
    pos_df = matching.load_purchase_orders(ws)

    with st.expander("Approved vendor master", expanded=False):
        st.dataframe(vendors_df, hide_index=True, width='stretch')
        with st.form("add_vendor_form", clear_on_submit=True):
            st.caption("Testing your own invoice? Add its vendor here. Only your session sees this.")
            new_vendor = st.text_input("Vendor name as printed on the invoice")
            new_status = st.selectbox("Status", ["active", "inactive"])
            if st.form_submit_button("Add vendor", width='stretch'):
                try:
                    vendor_id = matching.add_vendor(new_vendor, new_status, ws=ws)
                    flash("success", f"Added {new_vendor.strip()} as {vendor_id}.")
                except ValueError as err:
                    flash("error", str(err))
                st.rerun()

    with st.expander("Purchase order dataset", expanded=False):
        st.dataframe(pos_df, hide_index=True, width='stretch')
        name_by_id = dict(zip(vendors_df["vendor_id"], vendors_df["vendor_name"]))
        with st.form("add_po_form", clear_on_submit=True):
            st.caption("If the invoice prints no PO number, give its vendor exactly one PO and the "
                       "invoice is matched to it by vendor and amount.")
            po_vendor = st.selectbox("Vendor", list(name_by_id),
                                     format_func=lambda vid: f"{vid}  {name_by_id[vid]}")
            new_po_number = st.text_input("PO number")
            new_po_amount = st.number_input("PO amount (₹)", min_value=0.0, step=100.0, format="%.2f")
            if st.form_submit_button("Add purchase order", width='stretch'):
                try:
                    added = matching.add_purchase_order(new_po_number, po_vendor, new_po_amount, ws=ws)
                    flash("success", f"Added purchase order {added}.")
                except ValueError as err:
                    flash("error", str(err))
                st.rerun()

    st.divider()
    st.markdown('<p class="section-label">Demo controls</p>', unsafe_allow_html=True)
    live_delay = st.toggle("Simulate live execution delay", value=True,
                            help="Small pause between stages so the run view is watchable during a live demo.")
    use_ai = st.toggle("Use AI for hard documents", value=True,
                       help="When on, a language model reads documents the pattern parser can't. Every value it "
                            "returns must appear in the document text, or it is discarded.")
    if ai_assist.is_configured():
        st.caption("AI assist is available. It runs only when extraction is low-confidence or a key field is "
                   "missing, and the approve / review / reject decision is always plain rules.")
    else:
        st.caption("No AI key is configured on this deployment, so extraction is fully deterministic. "
                   "Try the 09_ai_unusual_layout sample to see what the AI step is for.")
    if notify.is_enabled():
        st.caption("Reviewer alerts are on for this deployment: when an invoice needs review, a short notice "
                   "(file name, vendor, total, first reason) is sent to the app owner's channel.")
    if st.button("Reset reference data to defaults", width='stretch'):
        reset_reference_data(ws)
        flash("success", "Vendor master and PO dataset restored to the defaults.")
        st.rerun()
    if st.button("⚠️ Reset my audit trail", width='stretch'):
        database.reset_db(ws)
        st.session_state.pop("last_result", None)
        flash("success", "Your audit trail was cleared.")
        st.rerun()
    st.caption("Everything on this page is private to your browser session. Other visitors "
               "never see your runs, and you never see theirs.")

tab_process, tab_dashboard = st.tabs(["Process invoice", "Dashboard"])

# ---------------------------------------------------------------- Process tab
with tab_process:
    col_input, col_run = st.columns([1, 2], gap="large")

    with col_input:
        st.markdown('<p class="section-label">1 — Provide an invoice</p>', unsafe_allow_html=True)
        source = st.radio("Source", ["Use a sample invoice", "Upload a PDF", "Upload several PDFs (batch)"],
                          label_visibility="collapsed")
        is_batch = source.startswith("Upload several")

        pdf_path = None
        filename = None
        batch_files = []

        if is_batch:
            uploaded_many = st.file_uploader("Invoice PDFs", type=["pdf"], accept_multiple_files=True)
            seen_names = set()
            for up in uploaded_many or []:
                name = Path(up.name).name
                stem, n = Path(name).stem, 1
                while name in seen_names:          # two files with the same name stay two files
                    n += 1
                    name = f"{stem}_{n}.pdf"
                seen_names.add(name)
                target = ws.uploads_dir / name
                target.write_bytes(up.getvalue())
                batch_files.append((str(target), name))
            if batch_files:
                st.caption(f"{len(batch_files)} file(s) ready. They are processed one after another, in the order shown.")
        elif source == "Upload a PDF":
            uploaded = st.file_uploader("Invoice PDF", type=["pdf"])
            if uploaded:
                filename = Path(uploaded.name).name
                tmp_path = ws.uploads_dir / filename
                tmp_path.write_bytes(uploaded.getvalue())
                pdf_path = str(tmp_path)
        else:
            SAMPLE_BLURBS = {
                "01_happy_path": "Happy path — clean invoice, exact PO match, approved vendor.",
                "02_edge_duplicate": "Edge case: upload this twice to see duplicate detection trigger on the 2nd run.",
                "03_edge_amount_mismatch": "Edge case: invoice total is 8% over the matched PO's remaining balance.",
                "04_edge_scanned": "Edge case: scanned/noisy document — no digital text layer, forces OCR fallback.",
                "05_edge_unknown_vendor": "Edge case: vendor not present in the approved vendor database.",
                "06_bonus_split_po_part1": "Bonus: first of two invoices against the same PO (split PO).",
                "06_bonus_split_po_part2": "Bonus: second invoice against the same PO — should still Approve via remaining-balance tracking.",
                "07_bonus_inactive_vendor": "Bonus: vendor exists but is marked inactive -> hard Rejected.",
                "08_bonus_missing_fields": "Bonus: invoice number and PO reference both missing from the document.",
                "09_ai_unusual_layout": "AI demo: labels the pattern parser doesn't know. Needs Review without an AI key; read and approved with one.",
            }
            samples = sorted(SAMPLES_DIR.glob("*.pdf"))
            labels = [f.name for f in samples]
            choice = st.selectbox("Sample invoice", labels, index=0 if labels else None)
            if choice:
                pdf_path = str(SAMPLES_DIR / choice)
                filename = choice
                blurb = next((v for k, v in SAMPLE_BLURBS.items() if choice.startswith(k)), "")
                if blurb:
                    st.caption(blurb)

        run_clicked = st.button(
            f"▶ Process {len(batch_files)} invoices" if is_batch else "▶ Run processing pipeline",
            type="primary", width='stretch',
            disabled=(not batch_files) if is_batch else (pdf_path is None))

    with col_run:
        st.markdown('<p class="section-label">2 — Live execution</p>', unsafe_allow_html=True)
        stage_container = st.container()
        result_container = st.container()

        if is_batch:
            if run_clicked and batch_files:
                rows, bar = [], st.progress(0.0, text="Starting batch...")
                for i, (path, name) in enumerate(batch_files, start=1):
                    bar.progress((i - 1) / len(batch_files), text=f"Processing {name} ({i} of {len(batch_files)})")
                    try:
                        res = [it for it in run_pipeline(path, name, ws=ws, use_ai=use_ai) if hasattr(it, "decision")][0]
                        rows.append({"file": name, "decision": res.decision.status,
                                     "vendor": res.extracted.vendor_name_raw or "—",
                                     "total": money(res.extracted.total),
                                     "invoice #": res.extracted.invoice_number or "—",
                                     "main reason": res.decision.reasons[0] if res.decision.reasons else "",
                                     "invoice_id": res.invoice_id})
                    except Exception as err:      # one unreadable file must not stop the rest of the batch
                        rows.append({"file": name, "decision": "Error", "vendor": "—", "total": "—", "invoice #": "—",
                                     "main reason": f"Could not process this file ({type(err).__name__}).",
                                     "invoice_id": ""})
                bar.progress(1.0, text=f"Finished {len(batch_files)} invoice(s)")
                st.session_state["batch_results"] = rows
            batch_rows = st.session_state.get("batch_results")
            with result_container:
                if not batch_rows:
                    st.info("Choose several invoice PDFs on the left, then click **Process**. You get a summary "
                            "table here, and anything that needs a person lands in the Dashboard's review queue.")
                else:
                    bdf = pd.DataFrame(batch_rows)
                    counts = bdf["decision"].value_counts()
                    st.markdown(f"""
                    <div class="ledger-metrics">
                      <div class="ledger-metric"><div class="num">{len(bdf)}</div><div class="lbl">Processed</div></div>
                      <div class="ledger-metric c-approved"><div class="num">{int(counts.get('Approved', 0))}</div><div class="lbl">Approved</div></div>
                      <div class="ledger-metric c-review"><div class="num">{int(counts.get('Needs Review', 0))}</div><div class="lbl">Needs review</div></div>
                      <div class="ledger-metric c-rejected"><div class="num">{int(counts.get('Rejected', 0))}</div><div class="lbl">Rejected</div></div>
                    </div>""", unsafe_allow_html=True)
                    st.dataframe(bdf.drop(columns=["invoice_id"]), hide_index=True, width='stretch')
                    if int(counts.get("Needs Review", 0)):
                        st.info("Invoices marked **Needs Review** are waiting in the **Dashboard** tab's review queue.")
                    st.download_button("Download this batch as CSV", bdf.drop(columns=["invoice_id"]).to_csv(index=False),
                                       file_name="batch_results.csv", mime="text/csv", key="batch_csv")
        elif run_clicked and pdf_path:
            stage_container.empty()
            final_result = None
            # All stage rows are accumulated and re-rendered as ONE markdown
            # block each tick (not one st.markdown() call per row). Streamlit
            # wraps every st.markdown() call in its own DOM container, which
            # would make each row an only-child and silently break the
            # `:not(:last-child)` CSS the connector spine depends on.
            with stage_container:
                placeholder = st.empty()
                rows_html = []
                for item in run_pipeline(pdf_path, filename, ws=ws, use_ai=use_ai):
                    if hasattr(item, "stage"):
                        rows_html.append(stage_row_html(item))
                        placeholder.markdown("".join(rows_html), unsafe_allow_html=True)
                        if live_delay:
                            time.sleep(0.18)
                    else:
                        final_result = item
            st.session_state["last_result"] = final_result
        elif st.session_state.get("last_result") is not None:
            # A later rerun (e.g. pressing Approve) redraws the finished run
            # statically instead of replaying the animation.
            with stage_container:
                st.markdown("".join(stage_row_html(s) for s in st.session_state["last_result"].stages),
                            unsafe_allow_html=True)

        last_result = None if is_batch else st.session_state.get("last_result")
        if is_batch:
            pass
        elif last_result is not None:
            with result_container:
                render_result(last_result)
        else:
            st.info("Pick an invoice on the left and click **Run processing pipeline** to see each stage execute live.")

# -------------------------------------------------------------- Dashboard tab
with tab_dashboard:
    history = database.get_all_history(ws)
    if not history:
        st.info("No invoices processed yet — run one from the **Process invoice** tab.")
    else:
        df = pd.DataFrame(history)
        n_approved = int((df.effective_status == "Approved").sum())
        n_review = int((df.effective_status == "Needs Review").sum())
        n_rejected = int((df.effective_status == "Rejected").sum())

        st.markdown(f"""
        <div class="ledger-metrics">
          <div class="ledger-metric"><div class="num">{len(df)}</div><div class="lbl">Processed</div></div>
          <div class="ledger-metric c-approved"><div class="num">{n_approved}</div><div class="lbl">Approved</div></div>
          <div class="ledger-metric c-review"><div class="num">{n_review}</div><div class="lbl">Needs review</div></div>
          <div class="ledger-metric c-rejected"><div class="num">{n_rejected}</div><div class="lbl">Rejected</div></div>
        </div>
        """, unsafe_allow_html=True)

        # ---- review queue: what a person still has to decide
        pending = df[(df.decision_status == "Needs Review") & (df.human_decision.isna())]
        st.markdown(f'<p class="section-label">Review queue ({len(pending)} waiting)</p>', unsafe_allow_html=True)
        if pending.empty:
            st.caption("Nothing is waiting for review.")
        for _, r in pending.iterrows():
            label = f"{r['filename']}  |  {r['vendor_name'] or 'vendor not read'}  |  {money(r['total'])}"
            with st.expander(label):
                for reason in str(r["decision_reasons"] or "").split(" | "):
                    if reason:
                        st.markdown(f'<div class="reason-line">{esc(reason)}</div>', unsafe_allow_html=True)
                review_panel(r["invoice_id"], key=f"queue_{r['invoice_id']}")

        plot_template = dict(
            paper_bgcolor="#101B16", plot_bgcolor="#101B16",
            font=dict(family="IBM Plex Sans, sans-serif", color="#EDEAE0"),
            legend=dict(bgcolor="rgba(0,0,0,0)"),
        )

        c1, c2 = st.columns([1, 2])
        with c1:
            counts = df["effective_status"].value_counts().reset_index()
            counts.columns = ["status", "count"]
            fig = px.pie(counts, names="status", values="count", title="Decision breakdown",
                         color="status", color_discrete_map=STATUS_COLOR, hole=0.55)
            fig.update_traces(marker=dict(line=dict(color="#101B16", width=2)))
            fig.update_layout(margin=dict(t=40, b=0, l=0, r=0), height=320, **plot_template)
            st.plotly_chart(fig, width='stretch')
        with c2:
            df_sorted = df.sort_values("processed_at")
            df_sorted["run_order"] = range(1, len(df_sorted) + 1)
            fig2 = px.bar(df_sorted, x="run_order", y="total", color="effective_status",
                          color_discrete_map=STATUS_COLOR, title="Invoice amounts by run (chronological)",
                          labels={"run_order": "Run #", "total": "Invoice total (₹)", "effective_status": "status"})
            fig2.update_layout(margin=dict(t=40, b=0, l=0, r=0), height=320,
                               xaxis=dict(gridcolor="#2A3A31"), yaxis=dict(gridcolor="#2A3A31"), **plot_template)
            st.plotly_chart(fig2, width='stretch')

        st.markdown('<p class="section-label" style="margin-top:12px;">Processing history</p>', unsafe_allow_html=True)
        dl1, dl2, _ = st.columns([1, 1, 2])
        dl1.download_button("Download audit trail (CSV)", export.csv_bytes(history), file_name="audit_trail.csv",
                            mime="text/csv", width='stretch', key="dl_csv")
        dl2.download_button("Download Excel (trail + approved bills)", export.excel_bytes(history),
                            file_name="invoice_audit.xlsx", key="dl_xlsx", width='stretch',
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        display_cols = ["invoice_id", "filename", "vendor_name", "invoice_number", "po_number", "total",
                        "decision_status", "human_decision", "reviewer_note", "confidence", "processed_at"]
        st.dataframe(
            df[display_cols].rename(columns={"decision_status": "system_decision",
                                             "human_decision": "reviewer_decision"})
              .sort_values("processed_at", ascending=False),
            hide_index=True, width='stretch')

        st.markdown('<p class="section-label">Drill into a run</p>', unsafe_allow_html=True)
        picked = st.selectbox("Invoice ID", df["invoice_id"].tolist())
        if picked:
            row = df[df.invoice_id == picked].iloc[0]
            st.markdown(f"**System decision:** {row['decision_status']}")
            if isinstance(row["human_decision"], str) and row["human_decision"]:
                note = f" — {row['reviewer_note']}" if row["reviewer_note"] else ""
                st.markdown(f"**Reviewer decision:** {row['human_decision']}{note}")
            st.markdown(f"**Reasoning:** {row['decision_reasons']}")
            picked_bill = export.bill_json_bytes(row.to_dict())
            if picked_bill:
                st.download_button("Download bill JSON (accounting hand-off)", picked_bill,
                                   file_name=f"bill_{picked}.json", mime="application/json", key=f"dl_bill_{picked}")
            with st.expander("Full audit JSON"):
                st.code(row["full_result_json"], language="json")
