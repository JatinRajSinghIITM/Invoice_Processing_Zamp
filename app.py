"""
Streamlit UI — invoice upload + live run view + processing history dashboard.

Two tabs:
  - Process Invoice: upload (or pick a sample) a PDF, watch each pipeline
    stage execute live, see the final decision and reasoning.
  - Dashboard: full processing history / audit trail, status breakdown,
    and drill-down into any past run — this is what satisfies "show history,
    status, and outputs across runs" from the candidate guide.
"""
import time
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.express as px

from core import database
from core.pipeline import run_pipeline

ROOT = Path(__file__).parent
SAMPLES_DIR = ROOT / "sample_invoices"

st.set_page_config(page_title="Invoice Processing — From PDF to Decision",
                    page_icon="📒", layout="wide")

database.init_db()

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

with st.sidebar:
    st.markdown('<p class="section-label">Reference data</p>', unsafe_allow_html=True)
    with st.expander("Approved vendor master", expanded=False):
        st.dataframe(pd.read_csv(ROOT / "data" / "vendors.csv"), hide_index=True, width='stretch')
    with st.expander("Purchase order dataset", expanded=False):
        st.dataframe(pd.read_csv(ROOT / "data" / "purchase_orders.csv"), hide_index=True, width='stretch')

    st.divider()
    st.markdown('<p class="section-label">Demo controls</p>', unsafe_allow_html=True)
    live_delay = st.toggle("Simulate live execution delay", value=True,
                            help="Small pause between stages so the run view is watchable during a live demo.")
    if st.button("⚠️ Reset audit trail", width='stretch'):
        database.reset_db()
        st.success("Audit trail cleared.")
        st.rerun()

tab_process, tab_dashboard = st.tabs(["Process invoice", "Dashboard"])

# ---------------------------------------------------------------- Process tab
with tab_process:
    col_input, col_run = st.columns([1, 2], gap="large")

    with col_input:
        st.markdown('<p class="section-label">1 — Provide an invoice</p>', unsafe_allow_html=True)
        source = st.radio("Source", ["Use a sample invoice", "Upload a PDF"], label_visibility="collapsed")

        pdf_path = None
        filename = None

        if source == "Upload a PDF":
            uploaded = st.file_uploader("Invoice PDF", type=["pdf"])
            if uploaded:
                tmp_path = ROOT / "data" / f"_upload_{uploaded.name}"
                tmp_path.write_bytes(uploaded.getvalue())
                pdf_path = str(tmp_path)
                filename = uploaded.name
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

        run_clicked = st.button("▶ Run processing pipeline", type="primary", width='stretch',
                                 disabled=pdf_path is None)

    with col_run:
        st.markdown('<p class="section-label">2 — Live execution</p>', unsafe_allow_html=True)
        stage_container = st.container()
        result_container = st.container()

        if run_clicked and pdf_path:
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
                for item in run_pipeline(pdf_path, filename):
                    if hasattr(item, "stage"):
                        mark = STAGE_MARK[item.status]
                        num, _, name = item.stage.partition(". ")
                        detail_html = f'<div class="ledger-detail">{item.detail}</div>' if item.detail else ""
                        rows_html.append(
                            f'<div class="ledger-row">'
                            f'<div class="ledger-dot {item.status}">{mark}</div>'
                            f'<div class="ledger-body">'
                            f'<span class="ledger-stage-name">{num}. {name}</span>'
                            f'<div class="ledger-summary">{item.summary}</div>'
                            f'{detail_html}'
                            f'</div></div>')
                        placeholder.markdown("".join(rows_html), unsafe_allow_html=True)
                        if live_delay:
                            time.sleep(0.18)
                    else:
                        final_result = item

            if final_result:
                with result_container:
                    st.markdown("<br>", unsafe_allow_html=True)
                    status = final_result.decision.status
                    stamp_class = {"Approved": "", "Needs Review": "review", "Rejected": "rejected"}[status]
                    st.markdown(f'<div class="stamp {stamp_class}">{status.upper()}</div>', unsafe_allow_html=True)

                    for r in final_result.decision.reasons:
                        st.markdown(f'<div class="reason-line">{r}</div>', unsafe_allow_html=True)

                    st.markdown('<p class="section-label" style="margin-top:22px;">Extracted fields</p>', unsafe_allow_html=True)
                    e = final_result.extracted
                    fcol1, fcol2, fcol3 = st.columns(3)
                    fcol1.metric("Vendor", e.vendor_name_raw or "—")
                    fcol1.metric("Invoice #", e.invoice_number or "—")
                    fcol2.metric("Invoice Date", e.invoice_date or "—")
                    fcol2.metric("PO Number", e.po_number or "—")
                    fcol3.metric("Total", f"₹{e.total:,.2f}" if e.total else "—")
                    fcol3.metric("Extraction confidence", f"{e.extraction_confidence:.0%}")

                    with st.expander("Full audit record (JSON)"):
                        st.json({
                            "invoice_id": final_result.invoice_id,
                            "extracted": vars(final_result.extracted),
                            "vendor_match": vars(final_result.vendor_match),
                            "po_match": vars(final_result.po_match),
                            "duplicate_check": vars(final_result.duplicate_check),
                            "decision": vars(final_result.decision),
                        })
        else:
            st.info("Pick an invoice on the left and click **Run processing pipeline** to see each stage execute live.")

# -------------------------------------------------------------- Dashboard tab
with tab_dashboard:
    history = database.get_all_history()
    if not history:
        st.info("No invoices processed yet — run one from the **Process Invoice** tab.")
    else:
        df = pd.DataFrame(history)
        n_approved = int((df.decision_status == "Approved").sum())
        n_review = int((df.decision_status == "Needs Review").sum())
        n_rejected = int((df.decision_status == "Rejected").sum())

        st.markdown(f"""
        <div class="ledger-metrics">
          <div class="ledger-metric"><div class="num">{len(df)}</div><div class="lbl">Processed</div></div>
          <div class="ledger-metric c-approved"><div class="num">{n_approved}</div><div class="lbl">Approved</div></div>
          <div class="ledger-metric c-review"><div class="num">{n_review}</div><div class="lbl">Needs review</div></div>
          <div class="ledger-metric c-rejected"><div class="num">{n_rejected}</div><div class="lbl">Rejected</div></div>
        </div>
        """, unsafe_allow_html=True)

        plot_template = dict(
            paper_bgcolor="#101B16", plot_bgcolor="#101B16",
            font=dict(family="IBM Plex Sans, sans-serif", color="#EDEAE0"),
            legend=dict(bgcolor="rgba(0,0,0,0)"),
        )

        c1, c2 = st.columns([1, 2])
        with c1:
            counts = df["decision_status"].value_counts().reset_index()
            counts.columns = ["status", "count"]
            fig = px.pie(counts, names="status", values="count", title="Decision breakdown",
                         color="status", color_discrete_map=STATUS_COLOR, hole=0.55)
            fig.update_traces(marker=dict(line=dict(color="#101B16", width=2)))
            fig.update_layout(margin=dict(t=40, b=0, l=0, r=0), height=320, **plot_template)
            st.plotly_chart(fig, width='stretch')
        with c2:
            df_sorted = df.sort_values("processed_at")
            df_sorted["run_order"] = range(1, len(df_sorted) + 1)
            fig2 = px.bar(df_sorted, x="run_order", y="total", color="decision_status",
                          color_discrete_map=STATUS_COLOR, title="Invoice amounts by run (chronological)",
                          labels={"run_order": "Run #", "total": "Invoice total (₹)"})
            fig2.update_layout(margin=dict(t=40, b=0, l=0, r=0), height=320,
                               xaxis=dict(gridcolor="#2A3A31"), yaxis=dict(gridcolor="#2A3A31"), **plot_template)
            st.plotly_chart(fig2, width='stretch')

        st.markdown('<p class="section-label" style="margin-top:12px;">Processing history</p>', unsafe_allow_html=True)
        display_cols = ["invoice_id", "filename", "vendor_name", "invoice_number", "po_number",
                         "total", "decision_status", "confidence", "processed_at"]
        st.dataframe(df[display_cols].sort_values("processed_at", ascending=False),
                     hide_index=True, width='stretch')

        st.markdown('<p class="section-label">Drill into a run</p>', unsafe_allow_html=True)
        picked = st.selectbox("Invoice ID", df["invoice_id"].tolist())
        if picked:
            row = df[df.invoice_id == picked].iloc[0]
            st.markdown(f"**Decision:** {row['decision_status']}")
            st.markdown(f"**Reasoning:** {row['decision_reasons']}")
            with st.expander("Full audit JSON"):
                st.code(row["full_result_json"], language="json")
