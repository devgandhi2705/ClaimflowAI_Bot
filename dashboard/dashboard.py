"""ClaimFlow AI - Operations dashboard (Streamlit).

Run:  streamlit run dashboard/dashboard.py
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ui  # noqa: E402
from app import config, db, pipeline  # noqa: E402
from app.config import load_policy, update_policy_value  # noqa: E402
from app.extractor import chaos_enabled, set_chaos  # noqa: E402
from app.logger import LOG_JSON  # noqa: E402

st.set_page_config(page_title="ClaimFlow AI", page_icon="🧾", layout="wide", initial_sidebar_state="expanded")
db.init_db()
ui.inject_css()

PLOTLY_CFG = {"displayModeBar": False}
MANUAL_MINUTES = 12  # baseline: manual handling time per claim (problem statement)
HEARTBEAT = config.DATA_DIR / "bot_heartbeat.txt"
WAITING = ("PENDING_APPROVAL", "NEEDS_REVIEW", "RETRY_PENDING")


# ================================================================ data
def query(sql: str, params=()) -> pd.DataFrame:
    with db.connect() as c:
        return pd.read_sql_query(sql, c, params=params)


def cutoff() -> str:
    days = {"24h": 1, "7d": 7, "30d": 30}.get(st.session_state.get("period") or "All")
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds") if days else "0000"


def claims_df() -> pd.DataFrame:
    df = query("SELECT * FROM claims WHERE created_at >= ? ORDER BY created_at DESC", (cutoff(),))
    if not df.empty:
        df["created"] = pd.to_datetime(df.created_at)
        amounts = dict(query("SELECT id, total_amount FROM claims").values)
        df["value"] = df.total_amount
        dup = df.status.eq("DUPLICATE") & df.duplicate_of.notna()
        df.loc[dup, "value"] = df.loc[dup, "duplicate_of"].map(amounts)
    return df


def bot_online() -> tuple:
    try:
        age = time.time() - float(HEARTBEAT.read_text())
        return age < 45, age
    except Exception:
        return False, None


def refresh_every(seconds: int):
    return seconds if st.session_state.get("auto_refresh", True) else None


def chart(fig):
    st.plotly_chart(fig, config=PLOTLY_CFG, width="stretch")


# ================================================================ claim detail (shared)
def decision_controls(c: dict, key: str):
    note = st.text_input("Comment (optional)", key=f"note-{key}", placeholder="e.g. Approved for client visit")
    a, r = st.columns(2)
    if a.button("Approve", key=f"ap-{key}", type="primary", icon=":material/check_circle:", width="stretch"):
        _decide(c["id"], True, note)
    if r.button("Reject", key=f"rj-{key}", icon=":material/cancel:", width="stretch"):
        _decide(c["id"], False, note)


def _decide(claim_id: str, approve: bool, note: str):
    try:
        with st.spinner("Recording decision and emailing voucher…"):
            pipeline.manager_decision(claim_id, approve, "Finance team (Dashboard)", note)
        st.toast(f"{claim_id} {'approved' if approve else 'rejected'} · email sent",
                 icon="✅" if approve else "❌")
    except ValueError as err:
        st.toast(str(err), icon="⚠️")
    time.sleep(0.6)
    st.rerun()


def claim_detail(c: dict, key: str):
    amount = ui.money(c.get("total_amount"), c.get("currency") or "INR")
    st.markdown(f'<div class="cf-page-title" style="margin-top:6px"><div><h2 style="font-size:22px">{ui.e(c["id"])} '
                f'&nbsp;{ui.pill(c["status"])}</h2><p>{ui.e(c.get("vendor") or "Unreadable receipt")} · '
                f'submitted {ui.ago(c["created_at"])} by {ui.e(c.get("employee_name"))}</p></div>'
                f'<div style="font-size:28px;font-weight:800;color:{ui.INK}">{amount}</div></div>',
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 1.2, 1.25], gap="medium")
    with c1:
        if c.get("image_path") and Path(c["image_path"]).exists():
            st.image(c["image_path"], width="stretch")
        voucher = config.VOUCHERS_DIR / f"{c['id']}.pdf"
        if voucher.exists():
            st.download_button("Download PDF voucher", voucher.read_bytes(), voucher.name, "application/pdf",
                               icon=":material/picture_as_pdf:", width="stretch", key=f"dl-{key}")
    with c2:
        conf = f"{c['confidence']:.0%}" if c.get("confidence") is not None else "–"
        rows = [("Employee", c.get("employee_name")), ("Purpose", c.get("purpose")), ("Vendor", c.get("vendor")),
                ("Receipt date", c.get("receipt_date")), ("Invoice no.", c.get("invoice_number")),
                ("Category", (c.get("category") or "–").replace("_", " ").title()), ("Amount", amount),
                ("AI confidence", conf), ("Decided by", c.get("decided_by"))]
        kv = "".join(f'<div class="k">{k}</div><div class="v">{ui.e(v) if v else "–"}</div>' for k, v in rows)
        st.markdown(f'<div class="cf-card"><div class="cf-kv">{kv}</div></div>', unsafe_allow_html=True)
        st.write("")
        chart(ui.gauge(c.get("risk_score")))
    with c3:
        ui.section("Why this decision")
        st.markdown(f'<div class="cf-card" style="padding:12px 14px;font-size:13.5px">{ui.e(c.get("reason"))}</div>',
                    unsafe_allow_html=True)
        st.write("")
        ui.section("Policy & fraud checks")
        st.markdown(ui.flags_html(ui.parse_flags(c.get("flags_json"))), unsafe_allow_html=True)
        ui.section("Audit trail", "immutable history")
        st.markdown(ui.timeline_html(db.audit_trail(c["id"])), unsafe_allow_html=True)
    if c["status"] in ("PENDING_APPROVAL", "NEEDS_REVIEW"):
        with st.container(border=True):
            ui.section("Your decision", "approving here works exactly like the Telegram buttons")
            decision_controls(c, key)
    with st.expander("AI extraction · raw JSON"):
        try:
            st.json(json.loads(c.get("extraction_json") or "{}"))
        except ValueError:
            st.write(c.get("extraction_json"))


# ================================================================ pages
def page_command_center():
    st.markdown(
        '<div class="cf-hero"><h1>Expense claims, on autopilot.</h1>'
        '<p>Employees send a receipt photo to Telegram. AI reads it, checks for fraud and duplicates, applies the company '
        'policy, then approves it, rejects it or asks a manager. Every step is logged and explained.</p>'
        '<div class="chips"><span class="chip">📲 Telegram intake</span><span class="chip">🧠 Gemini Vision</span>'
        '<span class="chip">📜 Policy-as-code</span><span class="chip">🛡️ Fraud & duplicate checks</span>'
        '<span class="chip">👤 Human-in-the-loop</span><span class="chip">📧 Email + PDF voucher</span></div></div>',
        unsafe_allow_html=True)

    def body():
        df = claims_df()
        if df.empty:
            ui.empty_state("📭", "No claims in this period",
                           "Send a receipt photo to the Telegram bot, or widen the time range in the sidebar.")
            return
        n = len(df)
        approved = df[df.status.isin(["AUTO_APPROVED", "APPROVED"])]
        auto = df.status.isin(["AUTO_APPROVED", "AUTO_REJECTED", "DUPLICATE"]).sum()
        blocked = df[df.status.isin(["AUTO_REJECTED", "REJECTED", "DUPLICATE"])]
        avg_s = df.processing_ms.dropna().mean() / 1000 if df.processing_ms.notna().any() else None
        faster = f"<b>{MANUAL_MINUTES * 60 / avg_s:,.0f}× faster</b> than manual ({MANUAL_MINUTES} min)" if avg_s else "–"
        saved_h = (n * MANUAL_MINUTES * 60 - df.processing_ms.fillna(0).sum() / 1000) / 3600
        today = (df.created.dt.date == datetime.now().date()).sum()
        ui.kpi_grid([
            ("Claims processed", f"{n}", f"{today} today", "🧾", ui.BLUE),
            ("Approved value", ui.money(approved.total_amount.sum(), short=True), f"{len(approved)} claims reimbursed",
             "💸", ui.STATUS_HEX["good"]),
            ("Hands-free rate", f"{auto / n:.0%}", "decided with zero human effort", "🤖", ui.VIOLET),
            ("Avg processing", f"{avg_s:.1f}s" if avg_s else "–", faster, "⚡", ui.STATUS_HEX["warning"]),
            ("Leakage prevented", ui.money(blocked.value.sum(), short=True), f"{len(blocked)} risky claims stopped",
             "🛡️", ui.STATUS_HEX["critical"]),
            ("Staff hours saved", f"{saved_h:.1f} h", f"vs {MANUAL_MINUTES} min of manual work per claim", "⏱️",
             ui.STATUS_HEX["serious"]),
        ])

        # ---- pipeline flow
        ui.section("How claims flowed through the pipeline", "live counts for the selected period")
        dup = int((df.status == "DUPLICATE").sum())
        read = int(df.confidence.notna().sum())
        checked = int(df.risk_score.notna().sum() - dup)
        conf = df.confidence.dropna().mean()
        split = [("Auto-approved", (df.status == "AUTO_APPROVED").sum(), "good"),
                 ("Sent to manager", df.status.isin(["PENDING_APPROVAL", "APPROVED", "REJECTED"]).sum(), "warning"),
                 ("Auto-rejected", (df.status == "AUTO_REJECTED").sum(), "critical"),
                 ("Review / retry", df.status.isin(["NEEDS_REVIEW", "RETRY_PENDING", "FAILED"]).sum(), "serious")]
        node = lambda ic, num, t, d: (f'<div class="cf-node"><div class="ic">{ic}</div><div class="n">{num}</div>'  # noqa: E731
                                      f'<div class="t">{t}</div><div class="d">{d}</div></div>')
        arrow = '<div class="cf-arrow">➜</div>'
        splits = "".join(f'<div class="cf-node"><div class="cf-bar" style="background:{ui.STATUS_HEX[r]}"></div>'
                         f'<div class="n">{int(v)}</div><div class="t" style="margin:0">{t}</div></div>' for t, v, r in split)
        st.markdown(
            '<div class="cf-flow">' + node("📥", n, "Received", "via Telegram") + arrow +
            node("🔐", n - dup, "Fingerprinted", f"{dup} duplicate(s) blocked") + arrow +
            node("🧠", read, "Read by AI", f"avg confidence {conf:.0%}" if conf == conf else "–") + arrow +
            node("📋", max(checked, 0), "Policy checked", "rules + risk score") + arrow +
            f'<div class="cf-split">{splits}</div></div>', unsafe_allow_html=True)

        # ---- charts row 1
        a, b = st.columns([1.7, 1], gap="medium")
        with a:
            daily = df.assign(day=df.created.dt.floor("h" if st.session_state.get("period") == "24h" else "D"),
                              outcome=df.status.map(ui.OUTCOME)).groupby(["day", "outcome"]).size().reset_index(name="n")
            fig = go.Figure()
            for outcome, color in ui.OUTCOME_COLORS.items():
                d = daily[daily.outcome == outcome]
                fig.add_bar(x=d.day, y=d.n, name=outcome, marker=dict(color=color, line=dict(color="#fff", width=2)),
                            hovertemplate=f"<b>{outcome}</b><br>%{{x|%d %b %Y}}<br>%{{y}} claim(s)<extra></extra>")
            fig.update_layout(barmode="stack")
            chart(ui.style(fig, "Claims over time", 330, legend=True))
        with b:
            s = df.status.value_counts().reset_index()
            s.columns = ["status", "n"]
            s["label"] = s.status.map(ui.status_label)
            s = s.sort_values("n")
            fig = go.Figure(go.Bar(x=s.n, y=s.label, orientation="h", text=s.n, textposition="outside",
                                   marker_color=[ui.STATUS_HEX[ui.STATUS.get(x, ("", "neutral"))[1]] for x in s.status],
                                   textfont=dict(color=ui.INK), cliponaxis=False,
                                   hovertemplate="<b>%{y}</b><br>%{x} claim(s)<extra></extra>"))
            fig.update_xaxes(showgrid=False, showticklabels=False)
            chart(ui.style(fig, "Where claims ended up", 330))

        # ---- charts row 2
        a, b = st.columns(2, gap="medium")
        with a:
            cat = (df[df.status.isin(["AUTO_APPROVED", "APPROVED"])].dropna(subset=["category"])
                   .groupby("category").total_amount.sum().sort_values())
            fig = go.Figure(go.Bar(x=cat.values, y=[c.replace("_", " ").title() for c in cat.index], orientation="h",
                                   marker_color=ui.BLUE, text=[ui.money(v, short=True) for v in cat.values],
                                   textposition="outside", textfont=dict(color=ui.INK), cliponaxis=False,
                                   hovertemplate="<b>%{y}</b><br>₹%{x:,.0f} approved<extra></extra>"))
            fig.update_xaxes(showgrid=False, showticklabels=False)
            chart(ui.style(fig, "Approved spend by category", 300))
        with b:
            r = df.risk_score.dropna()
            bins = pd.cut(r, bins=range(0, 101, 10), right=False, include_lowest=True).value_counts().sort_index()
            mids = [i.left + 5 for i in bins.index]
            p = load_policy()["routing"]
            colors = [ui.STATUS_HEX["good"] if m < p["auto_approve_max_risk"] else
                      ui.STATUS_HEX["warning"] if m < p["auto_reject_min_risk"] else ui.STATUS_HEX["critical"] for m in mids]
            fig = go.Figure(go.Bar(x=mids, y=bins.values, width=8.6, marker_color=colors,
                                   customdata=[f"{i.left}–{i.right}" for i in bins.index],
                                   hovertemplate="Risk %{customdata}<br>%{y} claim(s)<extra></extra>"))
            for x0, x1, label, role in [(0, p["auto_approve_max_risk"], "auto-approve", "good"),
                                        (p["auto_approve_max_risk"], p["auto_reject_min_risk"], "manager decides", "warning"),
                                        (p["auto_reject_min_risk"], 100, "auto-reject", "critical")]:
                fig.add_vrect(x0=x0, x1=x1, line_width=0, fillcolor=ui.tint(ui.STATUS_HEX[role], .07), layer="below",
                              annotation_text=label, annotation_position="top left",
                              annotation_font=dict(size=11, color=ui.INK_2))
            fig.update_xaxes(range=[0, 100], title="risk score", dtick=10, showgrid=False)
            fig.update_yaxes(title="claims", range=[0, max(bins.max(), 1) * 1.25])
            chart(ui.style(fig, "Risk score distribution", 300))

        # ---- row 3
        a, b = st.columns([1, 1], gap="medium")
        with a:
            codes = [f["code"] for fj in df.flags_json.dropna() for f in ui.parse_flags(fj) if f["code"] != "AI_CONFIDENCE"]
            if codes:
                rc = pd.Series(codes).value_counts().sort_values()
                fig = go.Figure(go.Bar(x=rc.values, y=[ui.RULES.get(c, c) for c in rc.index], orientation="h",
                                       marker_color=ui.BLUE, text=rc.values, textposition="outside",
                                       textfont=dict(color=ui.INK), cliponaxis=False,
                                       hovertemplate="<b>%{y}</b><br>triggered %{x} time(s)<extra></extra>"))
                fig.update_xaxes(showgrid=False, showticklabels=False)
                chart(ui.style(fig, "Policy rules triggered most", 340))
            else:
                ui.empty_state("✅", "No policy rules triggered", "Every claim in this period was clean.")
        with b:
            feed = db_rows("SELECT * FROM audit_log WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT 9", (cutoff(),))
            st.markdown(f'<div class="cf-card" style="height:340px;overflow:auto"><div class="cf-section" '
                        f'style="margin-top:0">Recent activity</div>{ui.feed_html(feed)}</div>', unsafe_allow_html=True)

    st.fragment(run_every=refresh_every(10))(body)()


def db_rows(sql: str, params=()) -> list:
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def page_inbox():
    rows = db.claims_by_status(*WAITING)
    counts = {s: sum(r["status"] == s for r in rows) for s in WAITING}
    ui.page_title("Approval inbox", "Claims that need a human decision. Approving here works exactly like the "
                  "manager buttons in Telegram, and the employee is emailed straight away.")
    view = st.segmented_control(
        "Show", ["All", "Awaiting manager", "Needs review", "Retry queue"], default="All", key="inbox_view",
        format_func=lambda v: {"All": f"All · {len(rows)}", "Awaiting manager": f"Awaiting manager · {counts['PENDING_APPROVAL']}",
                               "Needs review": f"Needs review · {counts['NEEDS_REVIEW']}",
                               "Retry queue": f"Retry queue · {counts['RETRY_PENDING']}"}[v])
    pick = {"Awaiting manager": "PENDING_APPROVAL", "Needs review": "NEEDS_REVIEW", "Retry queue": "RETRY_PENDING"}.get(view)
    rows = [r for r in rows if not pick or r["status"] == pick]
    if not rows:
        ui.empty_state("🎉", "You're all caught up", "No claims are waiting for a decision right now.")
        return
    for c in sorted(rows, key=lambda r: r["created_at"]):
        with st.container(border=True):
            a, b, d = st.columns([0.9, 2.3, 1.3], gap="medium")
            with a:
                if c.get("image_path") and Path(c["image_path"]).exists():
                    st.image(c["image_path"], width="stretch")
            with b:
                amount = ui.money(c.get("total_amount"), c.get("currency") or "INR")
                st.markdown(
                    f'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
                    f'<code style="font-size:13px">{ui.e(c["id"])}</code>{ui.pill(c["status"])}'
                    f'<span style="color:{ui.MUTED};font-size:12.5px">waiting {ui.ago(c["created_at"]).replace(" ago", "")}</span></div>'
                    f'<div style="font-size:24px;font-weight:800;color:{ui.INK};margin:8px 0 0">{amount}</div>'
                    f'<div style="color:{ui.INK_2};font-size:14px;margin-bottom:10px">{ui.e(c.get("vendor") or "Unreadable receipt")}'
                    f' · {ui.e((c.get("category") or "–").replace("_", " "))} · {ui.e(c.get("receipt_date"))}</div>'
                    f'<div class="cf-kv" style="grid-template-columns:96px 1fr;margin-bottom:10px">'
                    f'<div class="k">Employee</div><div class="v">{ui.e(c.get("employee_name"))}</div>'
                    f'<div class="k">Purpose</div><div class="v">{ui.e(c.get("purpose"))}</div>'
                    f'<div class="k">Why it\'s here</div><div class="v" style="font-weight:500">{ui.e(c.get("reason"))}</div></div>',
                    unsafe_allow_html=True)
                flags = [f for f in ui.parse_flags(c.get("flags_json")) if f["code"] != "AI_CONFIDENCE"]
                if flags:
                    st.markdown(ui.flags_html(flags), unsafe_allow_html=True)
            with d:
                if c.get("risk_score") is not None:
                    chart(ui.gauge(c["risk_score"], 170))
                if c["status"] == "RETRY_PENDING":
                    st.info(f"AI service was unavailable. Retried automatically every "
                            f"{config.RETRY_QUEUE_INTERVAL_SEC}s (cycle {c['retry_count']}). No action needed.",
                            icon=":material/autorenew:")
                else:
                    decision_controls(c, f"inbox-{c['id']}")


def page_claims():
    ui.page_title("Claims explorer", "Search every claim, click a row to see the receipt, the AI's reading, the "
                  "policy checks and the full audit trail.")
    df = claims_df()
    if df.empty:
        ui.empty_state("📭", "No claims yet", "Claims appear here as soon as a receipt is sent to the bot.")
        return
    f1, f2, f3 = st.columns([2, 1.5, 1.5])
    q = f1.text_input("Search", placeholder="Claim ID, vendor or employee", label_visibility="collapsed",
                      icon=":material/search:")
    sts = f2.multiselect("Status", sorted(df.status.unique(), key=ui.status_label), format_func=ui.status_label,
                         placeholder="All statuses", label_visibility="collapsed")
    cats = f3.multiselect("Category", sorted(df.category.dropna().unique()), placeholder="All categories",
                          format_func=lambda c: c.replace("_", " ").title(), label_visibility="collapsed")
    v = df
    if q:
        m = (v.id.str.contains(q, case=False) | v.vendor.fillna("").str.contains(q, case=False)
             | v.employee_name.fillna("").str.contains(q, case=False))
        v = v[m]
    if sts:
        v = v[v.status.isin(sts)]
    if cats:
        v = v[v.category.isin(cats)]
    table = pd.DataFrame({
        "Status": [f"{ui.status_emoji(s)} {ui.status_label(s)}" for s in v.status],
        "Claim": v.id, "Submitted": v.created, "Employee": v.employee_name, "Vendor": v.vendor.fillna("—"),
        "Category": v.category.fillna("–").str.replace("_", " ").str.title(), "Amount (₹)": v.total_amount,
        "AI confidence": v.confidence, "Risk": v.risk_score,
    })
    st.caption(f"{len(v)} of {len(df)} claims · click a row for details")
    ev = st.dataframe(
        table, hide_index=True, width="stretch", height=min(420, 38 + 35 * max(len(table), 1)),
        on_select="rerun", selection_mode="single-row", key="claims_table",
        column_config={
            "Amount (₹)": st.column_config.NumberColumn(format="%.2f", width="small"),
            "Category": st.column_config.TextColumn(width="small"),
            "Submitted": st.column_config.DatetimeColumn(format="DD MMM, HH:mm", width="small"),
            "Status": st.column_config.TextColumn(width="medium"),
            "AI confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent", width="small"),
            "Risk": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d", width="small"),
        })
    st.download_button("Export CSV", table.to_csv(index=False).encode(), "claimflow_claims.csv", "text/csv",
                       icon=":material/download:")
    sel = ev.selection.rows if ev and ev.selection else []
    if sel:
        st.divider()
        claim_detail(db.get_claim(v.iloc[sel[0]].id), "explorer")
    else:
        st.info("Select a claim in the table to open its full details.", icon=":material/touch_app:")


def page_activity():
    ui.page_title("Live activity", "Everything the system does, as it happens. The business view is in plain "
                  "language; the system view shows the raw structured logs.")
    t1, t2 = st.tabs(["Business activity", "System logs"])
    with t1:
        cid = st.text_input("Filter by claim", placeholder="e.g. CLM-2026-0004", key="act_cid",
                            icon=":material/filter_alt:")

        def feed():
            rows = db_rows("SELECT * FROM audit_log WHERE (? = '' OR claim_id LIKE ?) ORDER BY ts DESC, id DESC LIMIT 60",
                           (cid, f"%{cid}%"))
            st.markdown(f'<div class="cf-card" style="max-height:720px;overflow:auto">{ui.feed_html(rows)}</div>',
                        unsafe_allow_html=True)
        st.fragment(run_every=refresh_every(3))(feed)()
    with t2:
        a, b, c = st.columns([2, 1.5, 1])
        levels = a.pills("Levels", ["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"], selection_mode="multi",
                         default=["INFO", "SUCCESS", "WARNING", "ERROR"], key="lv")
        lcid = b.text_input("Claim ID", key="log_cid", placeholder="correlation id")
        n = c.number_input("Lines", 20, 1000, 200, step=50)

        def logs():
            if not LOG_JSON.exists():
                ui.empty_state("📜", "No logs yet", "Start the bot to see logs stream in.")
                return
            lines = LOG_JSON.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]
            out = []
            for ln in reversed(lines):
                try:
                    r = json.loads(ln)["record"]
                except Exception:
                    continue
                lvl, ex = r["level"]["name"], r["extra"]
                if levels and lvl not in levels:
                    continue
                if lcid and lcid.lower() not in str(ex.get("claim_id", "")).lower():
                    continue
                out.append(f'<div class="l"><span class="ts">{r["time"]["repr"][11:19]}</span> '
                           f'<span class="{lvl}">{lvl:<7}</span> <span class="cid">{ui.e(ex.get("claim_id", "-"))}</span> '
                           f'<span class="stg">{ui.e(ex.get("stage", "-"))}</span> {ui.e(r["message"])}</div>')
                if len(out) >= n:
                    break
            st.markdown(f'<div class="cf-log">{"".join(out) or "No matching log lines"}</div>', unsafe_allow_html=True)
            st.caption("Newest first · refreshes every 3s · also written to logs/claimflow.log and logs/claimflow.jsonl")
        st.fragment(run_every=refresh_every(3))(logs)()


def svc_card(name: str, icon: str, label: str, role: str, meta: str) -> str:
    c = ui.STATUS_HEX[role]
    return (f'<div class="cf-card cf-svc"><div class="name">{icon} {name}</div><div class="state">'
            f'<span class="cf-pill" style="background:{ui.tint(c, .16)}"><span class="dot" style="background:{c}"></span>'
            f'{label}</span></div><div class="meta">{meta}</div></div>')


def page_health():
    ui.page_title("System health", "Status of every integrated service, AI response times, and how the system "
                  "recovers from failures (retries, model failover, retry queue).")

    def body():
        m = query("SELECT * FROM api_metrics ORDER BY id")
        online, age = bot_online()
        g = m[m.service == "gemini"]
        recent = g.tail(20)
        mail = m[m.service == "gmail"]
        queue = len(db.claims_by_status("RETRY_PENDING"))
        with db.connect() as c:
            n_claims = c.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            n_audit = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        size = config.DB_PATH.stat().st_size / 1024 if config.DB_PATH.exists() else 0

        if chaos_enabled():
            ai = ("Outage simulation", "critical")
        elif recent.empty:
            ai = ("No calls yet", "neutral")
        else:
            ai = ("Healthy", "good") if recent.success.mean() >= 0.8 else ("Degraded", "warning")
        ok_lat = g[g.success == 1].latency_ms
        cards = [
            svc_card("Telegram bot", "📲", "Online" if online else "Offline", "good" if online else "critical",
                     f"Heartbeat {age:.0f}s ago" if age is not None else "Run <code>run_bot.bat</code> to start it"),
            svc_card("Gemini Vision AI", "🧠", ai[0], ai[1],
                     f"{len(g)} calls · {g.success.mean():.0%} success<br>avg {ok_lat.mean() / 1000:.1f}s per receipt"
                     if len(g) and len(ok_lat) else f"{len(g)} calls"),
            svc_card("Gmail SMTP", "📧", "Healthy" if mail.empty or mail.tail(5).success.all() else "Errors",
                     "good" if mail.empty or mail.tail(5).success.all() else "warning",
                     f"{int(mail.success.sum())} emails sent" + (f" · {int((1 - mail.success).sum())} failed" if len(mail) else "")),
            svc_card("SQLite database", "🗄️", "Healthy", "good", f"{n_claims} claims · {n_audit} audit events<br>{size:,.0f} KB"),
            svc_card("Retry queue", "🔁", "Empty" if queue == 0 else f"{queue} parked", "good" if queue == 0 else "warning",
                     f"Auto re-processed every {config.RETRY_QUEUE_INTERVAL_SEC}s"),
        ]
        st.markdown(f'<div class="cf-grid">{"".join(cards)}</div>', unsafe_allow_html=True)

        if g.empty:
            ui.empty_state("📡", "No AI calls recorded yet", "Metrics appear after the first receipt is processed.")
            return
        g = g.assign(ts=pd.to_datetime(g.ts), result=g.success.map({1: "Success", 0: "Failure"}),
                     model=g.model.fillna("gemini-2.5-flash"))
        a, b = st.columns([1.6, 1], gap="medium")
        with a:
            fig = go.Figure()
            for res, role in [("Success", "good"), ("Failure", "critical")]:
                d = g[g.result == res]
                fig.add_scatter(x=d.ts, y=d.latency_ms / 1000, mode="markers", name=res,
                                marker=dict(size=10, color=ui.STATUS_HEX[role], symbol="circle" if res == "Success" else "x",
                                            line=dict(color="#fff", width=2 if res == "Success" else 0)),
                                customdata=d[["model", "error"]].fillna("").values,
                                hovertemplate="<b>%{customdata[0]}</b><br>%{x|%d %b %H:%M:%S}<br>%{y:.2f}s"
                                              "<br>%{customdata[1]}<extra>" + res + "</extra>")
            fig.update_yaxes(title="seconds", rangemode="tozero")
            chart(ui.style(fig, "AI response time per request", 320, legend=True))
        with b:
            by = g.groupby(["model", "result"]).size().unstack(fill_value=0)
            fig = go.Figure()
            for res, role in [("Success", "good"), ("Failure", "critical")]:
                if res in by:
                    fig.add_bar(y=by.index, x=by[res], name=res, orientation="h",
                                marker=dict(color=ui.STATUS_HEX[role], line=dict(color="#fff", width=2)),
                                hovertemplate="<b>%{y}</b><br>%{x} " + res.lower() + "(s)<extra></extra>")
            fig.update_layout(barmode="stack")
            chart(ui.style(fig, "Requests per AI model (failover)", 320, legend=True))
        errs = m[m.success == 0][["ts", "service", "model", "error"]].tail(15).iloc[::-1]
        if len(errs):
            ui.section("Recent failures", "each was retried or failed over automatically")
            st.dataframe(errs.rename(columns={"ts": "Time", "service": "Service", "model": "Model", "error": "Error"}),
                         hide_index=True, width="stretch")

    st.fragment(run_every=refresh_every(5))(body)()


def page_policy():
    p = load_policy()
    ui.page_title("Expense policy", "The rules every claim is checked against. They are stored as code in "
                  "<code>config/policy.yaml</code>, and changes made here take effect in the bot immediately.")
    r = p["routing"]
    lo, hi = r["auto_approve_max_risk"], r["auto_reject_min_risk"]
    ui.section("How a claim is decided", "every triggered rule adds risk points")
    st.markdown(
        f'<div class="cf-card"><div class="cf-scale">'
        f'<div style="flex:{lo};background:{ui.STATUS_HEX["good"]}">✓ Auto-approve (if ≤ {ui.money(p["auto_approve_limit"], short=True)})</div>'
        f'<div style="flex:{hi - lo};background:#e09b00">👤 Manager decides</div>'
        f'<div style="flex:{100 - hi};background:{ui.STATUS_HEX["critical"]}">✕ Auto-reject</div></div>'
        f'<div class="cf-scale-axis" style="position:relative;height:16px"><span style="position:absolute;left:0">risk 0</span>'
        f'<span style="position:absolute;left:{lo}%;transform:translateX(-50%)">{lo}</span>'
        f'<span style="position:absolute;left:{hi}%;transform:translateX(-50%)">{hi}</span>'
        f'<span style="position:absolute;right:0">100</span></div>'
        f'<div style="font-size:13px;color:{ui.INK_2};margin-top:10px">Hard rules (receipt older than '
        f'{p["max_receipt_age_days"]} days, future date, more than {p["hard_limit_multiplier"]}× the category limit) '
        f'reject the claim outright. AI confidence below {p["min_ai_confidence"]:.0%} sends it to manual review.</div></div>',
        unsafe_allow_html=True)
    st.write("")
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        with st.form("policy_form", border=True):
            ui.section("Edit policy")
            a, b = st.columns(2)
            auto_limit = a.number_input("Auto-approve limit (₹)", 0, 100000, int(p["auto_approve_limit"]), step=500)
            max_age = b.number_input("Max receipt age (days)", 1, 365, int(p["max_receipt_age_days"]))
            min_conf = a.slider("Min AI confidence", 0.3, 0.99, float(p["min_ai_confidence"]), 0.05)
            mult = b.number_input("Hard limit (× category limit)", 1.5, 10.0, float(p["hard_limit_multiplier"]), 0.5)
            zone = st.slider("Risk zones: auto-approve below / auto-reject at or above", 0, 100, (lo, hi))
            st.markdown("**Category limits (₹ per claim)**")
            cols = st.columns(3)
            limits = {k: cols[i % 3].number_input(k.replace("_", " ").title(), 0, 500000, int(v), step=500, key=f"lim-{k}")
                      for i, (k, v) in enumerate(p["category_limits"].items())}
            if st.form_submit_button("Save policy", type="primary", icon=":material/save:"):
                changes = {}
                for key, new, old in [("auto_approve_limit", auto_limit, p["auto_approve_limit"]),
                                      ("max_receipt_age_days", max_age, p["max_receipt_age_days"]),
                                      ("min_ai_confidence", round(min_conf, 2), p["min_ai_confidence"]),
                                      ("hard_limit_multiplier", mult, p["hard_limit_multiplier"]),
                                      ("auto_approve_max_risk", zone[0], lo), ("auto_reject_min_risk", zone[1], hi),
                                      *[(k, v, p["category_limits"][k]) for k, v in limits.items()]]:
                    if new != old:
                        update_policy_value(key, int(new) if float(new).is_integer() and key != "min_ai_confidence" else new)
                        changes[key] = f"{old} → {new}"
                if changes:
                    db.audit(None, "Finance team (Dashboard)", "POLICY_UPDATED", details=changes)
                    st.toast(f"Policy saved · {len(changes)} change(s) live in the bot", icon="📜")
                    time.sleep(0.6)
                    st.rerun()
                else:
                    st.toast("No changes to save", icon="ℹ️")
    with right:
        lim = pd.Series(p["category_limits"]).sort_values()
        fig = go.Figure(go.Bar(x=lim.values, y=[k.replace("_", " ").title() for k in lim.index], orientation="h",
                               marker_color=ui.BLUE, text=[ui.money(v, short=True) for v in lim.values],
                               textposition="outside", textfont=dict(color=ui.INK), cliponaxis=False,
                               hovertemplate="<b>%{y}</b><br>limit ₹%{x:,.0f}<extra></extra>"))
        fig.update_xaxes(showgrid=False, showticklabels=False)
        chart(ui.style(fig, "Spending limit per claim", 260))
        w = {ui.RULES.get(k.upper(), k.replace("_", " ").title()): v for k, v in p["risk_weights"].items()
             if k != "low_confidence_factor"}
        ws = pd.Series(w).sort_values()
        fig = go.Figure(go.Bar(x=ws.values, y=ws.index, orientation="h", marker_color=ui.VIOLET, text=ws.values,
                               textposition="outside", textfont=dict(color=ui.INK), cliponaxis=False,
                               hovertemplate="<b>%{y}</b><br>+%{x} risk points<extra></extra>"))
        fig.update_xaxes(showgrid=False, showticklabels=False)
        chart(ui.style(fig, "Risk points per rule", 260))
        ui.section("Non-reimbursable items")
        st.markdown('<div class="cf-chipset">' + "".join(f'<span class="cf-chip">🚫 {ui.e(k)}</span>'
                                                         for k in p["prohibited_keywords"]) + "</div>",
                    unsafe_allow_html=True)
    with st.expander("View policy.yaml source"):
        st.code(config.POLICY_PATH.read_text(encoding="utf-8"), language="yaml")


# ================================================================ shell
waiting = len(db.claims_by_status(*WAITING))
st.logo(str(Path(__file__).parent / "logo.svg"), size="large")
pages = {
    "Operations": [
        st.Page(page_command_center, title="Command center", icon=":material/space_dashboard:", default=True),
        st.Page(page_inbox, title=f"Approval inbox{f' ({waiting})' if waiting else ''}", icon=":material/inbox:",
                url_path="inbox"),
        st.Page(page_claims, title="Claims explorer", icon=":material/receipt_long:", url_path="claims"),
    ],
    "Monitoring": [
        st.Page(page_activity, title="Live activity", icon=":material/monitoring:", url_path="activity"),
        st.Page(page_health, title="System health", icon=":material/health_and_safety:", url_path="health"),
    ],
    "Configuration": [
        st.Page(page_policy, title="Expense policy", icon=":material/policy:", url_path="policy"),
    ],
}
nav = st.navigation(pages, position="sidebar")

with st.sidebar:
    online, _ = bot_online()
    dot = lambda ok: f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{"#22c55e" if ok else "#ef4444"};margin-right:7px"></span>'  # noqa: E731
    st.markdown(f'<div style="font-size:13px;line-height:1.9;color:#cbd5e1">{dot(online)}Bot {"online" if online else "offline"}<br>'
                f'{dot(not chaos_enabled())}AI service {"healthy" if not chaos_enabled() else "outage (simulated)"}</div>',
                unsafe_allow_html=True)
    st.divider()
    st.segmented_control("Time range", ["24h", "7d", "30d", "All"], default="All", key="period")
    chaos = st.toggle("Simulate AI outage", value=chaos_enabled(),
                      help="Chaos testing: every AI call fails, so claims go to the retry queue. Turn it off and they "
                           "are processed automatically.")
    if chaos != chaos_enabled():
        set_chaos(chaos)
        db.audit(None, "Finance team (Dashboard)", f"CHAOS_{'ON' if chaos else 'OFF'}")
        st.rerun()
    st.toggle("Live auto-refresh", value=True, key="auto_refresh")
    st.caption(f"{load_policy()['company']}  \nClaimFlow AI · CA3 Automation Capstone")

nav.run()
