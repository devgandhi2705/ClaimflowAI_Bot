"""Design system for the ClaimFlow AI dashboard: tokens, CSS, chart template and HTML components."""
import html
import json
from datetime import datetime

import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------- tokens
INK, INK_2, MUTED = "#0f172a", "#475569", "#94a3b8"
GRID, AXIS, SURFACE = "#eef0f3", "#cbd5e1", "#ffffff"
BLUE, VIOLET = "#2a78d6", "#6250d6"
STATUS_HEX = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
              "info": "#2a78d6", "neutral": "#94a3b8"}

STATUS = {  # code -> (friendly label, status role, icon)
    "AUTO_APPROVED": ("Auto-approved", "good", "✓"),
    "APPROVED": ("Approved by manager", "good", "✓"),
    "PENDING_APPROVAL": ("Awaiting manager", "warning", "⏳"),
    "NEEDS_REVIEW": ("Needs manual review", "serious", "🔍"),
    "AUTO_REJECTED": ("Auto-rejected", "critical", "✕"),
    "REJECTED": ("Rejected by manager", "critical", "✕"),
    "DUPLICATE": ("Duplicate blocked", "critical", "⧉"),
    "RETRY_PENDING": ("Queued for retry", "info", "↻"),
    "FAILED": ("Failed", "neutral", "!"),
    "PROCESSING": ("Processing", "info", "…"),
}
OUTCOME = {  # status -> outcome bucket (used for the stacked timeline)
    "AUTO_APPROVED": "Approved", "APPROVED": "Approved",
    "PENDING_APPROVAL": "Awaiting action", "NEEDS_REVIEW": "Awaiting action", "RETRY_PENDING": "Awaiting action",
    "PROCESSING": "Awaiting action",
    "AUTO_REJECTED": "Rejected / blocked", "REJECTED": "Rejected / blocked", "DUPLICATE": "Rejected / blocked",
    "FAILED": "Rejected / blocked",
}
OUTCOME_COLORS = {"Approved": STATUS_HEX["good"], "Awaiting action": STATUS_HEX["warning"],
                  "Rejected / blocked": STATUS_HEX["critical"]}

RULES = {
    "OVER_CATEGORY_LIMIT": "Over category limit", "EXCEEDS_HARD_LIMIT": "Exceeds hard limit (3×)",
    "RECEIPT_TOO_OLD": "Receipt too old", "FUTURE_DATE": "Future-dated receipt", "WEEKEND_EXPENSE": "Weekend expense",
    "MISSING_DATE": "Date unreadable", "PROHIBITED_ITEM": "Non-reimbursable item",
    "ROUND_AMOUNT": "Suspiciously round amount", "FOREIGN_CURRENCY": "Foreign currency",
    "SUSPECTED_DUPLICATE": "Suspected duplicate", "AI_CONFIDENCE": "AI uncertainty",
}
SEVERITY = {"hard": ("critical", "⛔", "Hard rule"), "warn": ("warning", "⚠️", "Warning"), "info": ("info", "ℹ️", "Info")}

ACTIONS = {  # audit action -> (icon, verb phrase, role)
    "SUBMITTED": ("📥", "submitted a receipt", "info"),
    "AI_EXTRACTED": ("🧠", "read the receipt", "info"),
    "DECISION": ("⚖️", "made a routing decision", "info"),
    "MANAGER_APPROVED": ("✅", "approved the claim", "good"),
    "MANAGER_REJECTED": ("❌", "rejected the claim", "critical"),
    "DUPLICATE_BLOCKED": ("🚫", "blocked a duplicate", "critical"),
    "LOW_CONFIDENCE": ("🔍", "sent it to manual review", "serious"),
    "INVALID_EXTRACTION": ("🔍", "sent it to manual review", "serious"),
    "AI_SERVICE_DOWN": ("🔁", "hit an AI outage, queued for retry", "warning"),
    "RETRY_ATTEMPT": ("♻️", "retried a queued claim", "info"),
    "RETRY_EXHAUSTED": ("⚠️", "gave up after repeated retries", "critical"),
    "EMAIL_SENT": ("📧", "emailed the PDF voucher", "good"),
    "EMAIL_FAILED": ("📧", "could not send the email", "critical"),
    "PIPELINE_ERROR": ("⚠️", "hit an internal error", "critical"),
    "CHAOS_ON": ("🧪", "started an AI outage simulation", "warning"),
    "CHAOS_OFF": ("🧪", "ended the AI outage simulation", "good"),
    "POLICY_RELOADED": ("📜", "reloaded the expense policy", "info"),
    "POLICY_UPDATED": ("📜", "updated the expense policy", "info"),
    "EMAIL_REGISTERED": ("📧", "registered a notification email", "info"),
}

CSS = f"""
<style>
.block-container {{ padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1440px; }}
header[data-testid="stHeader"] {{ background: transparent; }}
h1, h2, h3 {{ letter-spacing: -0.02em; }}
[data-testid="stPlotlyChart"] {{ background: {SURFACE}; border: 1px solid #e4e7ec; border-radius: 16px;
  padding: 6px 8px 2px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }}
[data-testid="stSidebar"] hr {{ border-color: #334155; }}

.cf-hero {{ position: relative; overflow: hidden; border-radius: 20px; padding: 26px 30px; margin-bottom: 18px;
  background: radial-gradient(1200px 300px at 0% 0%, #1d4ed8 0%, transparent 60%),
              radial-gradient(900px 300px at 100% 100%, #6250d6 0%, transparent 55%), #0f172a; color: #f8fafc; }}
.cf-hero h1 {{ color: #fff; font-size: 30px; font-weight: 800; margin: 0 0 6px; padding: 0; }}
.cf-hero p {{ color: #cbd5e1; margin: 0; font-size: 15px; max-width: 820px; }}
.cf-hero .chips {{ margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }}
.cf-hero .chip {{ background: rgba(255,255,255,.09); border: 1px solid rgba(255,255,255,.14); color: #e2e8f0;
  padding: 4px 11px; border-radius: 999px; font-size: 12.5px; font-weight: 500; }}

.cf-page-title {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin: 0 0 14px; flex-wrap: wrap; }}
.cf-page-title h2 {{ margin: 0; padding: 0; font-size: 26px; font-weight: 800; color: {INK}; }}
.cf-page-title p {{ margin: 2px 0 0; color: {INK_2}; font-size: 14px; }}

.cf-grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-bottom: 16px; }}
.cf-grid.k6 {{ grid-template-columns: repeat(3, 1fr); }}
@media (max-width: 900px) {{ .cf-grid.k6 {{ grid-template-columns: repeat(2, 1fr); }} }}
.cf-card {{ background: {SURFACE}; border: 1px solid #e4e7ec; border-radius: 16px; padding: 16px 18px;
  box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 10px 28px -18px rgba(15,23,42,.25); }}
.cf-kpi .top {{ display: flex; align-items: center; justify-content: space-between; }}
.cf-kpi .label {{ font-size: 12px; font-weight: 600; color: {INK_2}; text-transform: uppercase; letter-spacing: .05em; }}
.cf-kpi .icon {{ width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; font-size: 17px; }}
.cf-kpi .value {{ font-size: 30px; font-weight: 800; color: {INK}; margin-top: 8px; line-height: 1.1; }}
.cf-kpi .sub {{ font-size: 12.5px; color: {INK_2}; margin-top: 6px; }}
.cf-kpi .sub b {{ color: #006300; }}

.cf-section {{ font-size: 15px; font-weight: 700; color: {INK}; margin: 8px 0 10px; display: flex; align-items: center; gap: 8px; }}
.cf-section span {{ font-weight: 500; color: {MUTED}; font-size: 13px; }}

.cf-pill {{ display: inline-flex; align-items: center; gap: 7px; padding: 3px 11px 3px 9px; border-radius: 999px;
  font-size: 12.5px; font-weight: 600; color: {INK}; white-space: nowrap; }}
.cf-pill .dot {{ width: 8px; height: 8px; border-radius: 50%; }}

.cf-flow {{ display: flex; align-items: center; gap: 6px; overflow-x: auto; padding: 4px 2px 8px; }}
.cf-node {{ flex: 1 1 0; min-width: 132px; background: {SURFACE}; border: 1px solid #e4e7ec; border-radius: 14px;
  padding: 12px 14px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }}
.cf-node .n {{ font-size: 24px; font-weight: 800; color: {INK}; }}
.cf-node .t {{ font-size: 12.5px; font-weight: 600; color: {INK}; margin-top: 2px; }}
.cf-node .d {{ font-size: 11.5px; color: {INK_2}; margin-top: 2px; }}
.cf-node .ic {{ font-size: 16px; }}
.cf-arrow {{ color: {AXIS}; font-size: 20px; flex: 0 0 auto; }}
.cf-split {{ display: flex; flex-direction: column; gap: 6px; flex: 1.2 1 0; min-width: 170px; }}
.cf-split .cf-node {{ padding: 8px 12px; display: flex; align-items: center; gap: 10px; }}
.cf-split .cf-node .n {{ font-size: 18px; min-width: 28px; }}
.cf-bar {{ width: 4px; align-self: stretch; border-radius: 4px; }}

.cf-feed {{ display: flex; flex-direction: column; }}
.cf-feed .row {{ display: flex; gap: 12px; padding: 10px 2px; border-bottom: 1px solid {GRID}; align-items: flex-start; }}
.cf-feed .row:last-child {{ border-bottom: 0; }}
.cf-feed .ic {{ width: 32px; height: 32px; flex: 0 0 32px; border-radius: 10px; display: grid; place-items: center; font-size: 15px; }}
.cf-feed .txt {{ font-size: 13.5px; color: {INK}; line-height: 1.35; }}
.cf-feed .meta {{ font-size: 12px; color: {MUTED}; margin-top: 2px; }}
.cf-feed code {{ font-size: 12px; background: #f1f5f9; color: {INK}; padding: 1px 6px; border-radius: 6px; }}

.cf-tl {{ position: relative; margin-left: 8px; padding-left: 20px; border-left: 2px solid {GRID}; }}
.cf-tl .ev {{ position: relative; padding: 0 0 14px; }}
.cf-tl .ev::before {{ content: ""; position: absolute; left: -27px; top: 3px; width: 12px; height: 12px; border-radius: 50%;
  background: var(--c); box-shadow: 0 0 0 3px #fff; }}
.cf-tl .h {{ font-size: 13.5px; font-weight: 600; color: {INK}; }}
.cf-tl .m {{ font-size: 12px; color: {INK_2}; }}

.cf-kv {{ display: grid; grid-template-columns: 120px 1fr; row-gap: 8px; column-gap: 12px; font-size: 13.5px; }}
.cf-kv .k {{ color: {INK_2}; }}
.cf-kv .v {{ color: {INK}; font-weight: 600; word-break: break-word; }}

.cf-flag {{ display: flex; gap: 10px; align-items: flex-start; padding: 9px 12px; border-radius: 12px; margin-bottom: 7px;
  background: #f8fafc; border: 1px solid {GRID}; border-left: 4px solid var(--c); }}
.cf-flag .t {{ font-weight: 600; font-size: 13.5px; color: {INK}; }}
.cf-flag .d {{ font-size: 12.5px; color: {INK_2}; }}
.cf-flag .p {{ margin-left: auto; font-size: 12px; font-weight: 700; color: {INK_2}; white-space: nowrap; }}

.cf-chipset {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.cf-chip {{ background: #f1f5f9; border: 1px solid #e2e8f0; color: {INK}; border-radius: 999px; padding: 3px 10px; font-size: 12.5px; }}

.cf-scale {{ display: flex; height: 38px; border-radius: 12px; overflow: hidden; font-size: 12.5px; font-weight: 600; color: #fff; gap: 2px; }}
.cf-scale div {{ display: flex; align-items: center; justify-content: center; text-align: center; padding: 0 8px; }}
.cf-scale-axis {{ display: flex; justify-content: space-between; font-size: 11.5px; color: {MUTED}; margin-top: 4px; }}

.cf-svc .name {{ font-weight: 700; color: {INK}; font-size: 14.5px; }}
.cf-svc .state {{ margin-top: 8px; }}
.cf-svc .meta {{ font-size: 12.5px; color: {INK_2}; margin-top: 8px; line-height: 1.5; }}

.cf-empty {{ text-align: center; padding: 46px 20px; color: {INK_2}; }}
.cf-empty .big {{ font-size: 44px; }}
.cf-empty h3 {{ color: {INK}; margin: 8px 0 4px; }}

.cf-log {{ font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 12.5px; background: #0b1220; color: #cbd5e1;
  border-radius: 14px; padding: 12px 14px; max-height: 560px; overflow: auto; line-height: 1.6; }}
.cf-log .l {{ white-space: pre-wrap; word-break: break-word; }}
.cf-log .ts {{ color: #64748b; }} .cf-log .cid {{ color: #86b6ef; }} .cf-log .stg {{ color: #c4b5fd; }}
.cf-log .INFO {{ color: #93c5fd; }} .cf-log .SUCCESS {{ color: #4ade80; }} .cf-log .WARNING {{ color: #fbbf24; }}
.cf-log .ERROR, .cf-log .CRITICAL {{ color: #f87171; }} .cf-log .DEBUG {{ color: #64748b; }}
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- helpers
def e(x) -> str:
    return html.escape(str(x)) if x is not None else "–"


def tint(hex_color: str, alpha: float = 0.14) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def money(v, cur: str = "INR", short: bool = False) -> str:
    if v is None or v != v:
        return "–"
    sym = "₹" if (cur or "INR") == "INR" else f"{cur} "
    if short:
        if abs(v) >= 1e7:
            return f"{sym}{v / 1e7:.1f}Cr"
        if abs(v) >= 1e5:
            return f"{sym}{v / 1e5:.1f}L"
        if abs(v) >= 1e3:
            return f"{sym}{v / 1e3:.1f}k"
    return f"{sym}{v:,.0f}" if short else f"{sym}{v:,.2f}"


def ago(ts: str) -> str:
    try:
        s = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return ts or "–"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def status_label(code: str) -> str:
    return STATUS.get(code, (code, "neutral", ""))[0]


def status_emoji(code: str) -> str:
    role = STATUS.get(code, ("", "neutral", ""))[1]
    return {"good": "🟢", "warning": "🟡", "serious": "🟠", "critical": "🔴", "info": "🔵"}.get(role, "⚪")


# ---------------------------------------------------------------- components (HTML)
def pill(code: str) -> str:
    label, role, _ = STATUS.get(code, (code, "neutral", ""))
    c = STATUS_HEX[role]
    return f'<span class="cf-pill" style="background:{tint(c, .16)}"><span class="dot" style="background:{c}"></span>{e(label)}</span>'


def page_title(title: str, subtitle: str, right_html: str = ""):
    st.markdown(f'<div class="cf-page-title"><div><h2>{title}</h2><p>{subtitle}</p></div><div>{right_html}</div></div>',
                unsafe_allow_html=True)


def section(title: str, hint: str = ""):
    st.markdown(f'<div class="cf-section">{title}{f" <span>· {hint}</span>" if hint else ""}</div>',
                unsafe_allow_html=True)


def kpi_grid(items: list):
    """items: (label, value, sub_html, icon, color)."""
    cards = "".join(
        f'<div class="cf-card cf-kpi"><div class="top"><div class="label">{label}</div>'
        f'<div class="icon" style="background:{tint(color, .14)}">{icon}</div></div>'
        f'<div class="value">{value}</div><div class="sub">{sub}</div></div>'
        for label, value, sub, icon, color in items)
    cls = "cf-grid k6" if len(items) == 6 else "cf-grid"
    st.markdown(f'<div class="{cls}">{cards}</div>', unsafe_allow_html=True)


def flags_html(flags: list) -> str:
    if not flags:
        return '<div class="cf-flag" style="--c:#0ca30c"><div><div class="t">✓ All policy checks passed</div>' \
               '<div class="d">No rule was triggered for this claim.</div></div></div>'
    out = []
    for f in flags:
        role, icon, sev = SEVERITY.get(f["severity"], ("info", "•", f["severity"]))
        pts = f'+{f["points"]} risk' if f.get("points") else sev
        out.append(f'<div class="cf-flag" style="--c:{STATUS_HEX[role]}"><div>{icon}</div><div>'
                   f'<div class="t">{e(RULES.get(f["code"], f["code"]))}</div><div class="d">{e(f["message"])}</div></div>'
                   f'<div class="p">{pts}</div></div>')
    return "".join(out)


def timeline_html(trail: list) -> str:
    rows = []
    for a in trail:
        icon, verb, role = ACTIONS.get(a["action"], ("•", a["action"].replace("_", " ").lower(), "neutral"))
        to = f" → {status_label(a['to_status'])}" if a.get("to_status") and a["action"] in ("DECISION", "DUPLICATE_BLOCKED", "LOW_CONFIDENCE", "INVALID_EXTRACTION", "AI_SERVICE_DOWN") else ""
        rows.append(f'<div class="ev" style="--c:{STATUS_HEX[role]}"><div class="h">{icon} {e(a["actor"])} {verb}{e(to)}</div>'
                    f'<div class="m">{e(a["ts"].replace("T", " "))} · {ago(a["ts"])}</div></div>')
    return f'<div class="cf-tl">{"".join(rows)}</div>'


def feed_html(rows: list) -> str:
    items = []
    for a in rows:
        icon, verb, role = ACTIONS.get(a["action"], ("•", a["action"].replace("_", " ").lower(), "neutral"))
        cid = f' <code>{e(a["claim_id"])}</code>' if a.get("claim_id") else ""
        extra = ""
        if a["action"] == "DECISION" and a.get("to_status"):
            extra = f" → <b>{e(status_label(a['to_status']))}</b>"
        items.append(f'<div class="row"><div class="ic" style="background:{tint(STATUS_HEX[role], .15)}">{icon}</div>'
                     f'<div><div class="txt"><b>{e(a["actor"])}</b> {verb}{cid}{extra}</div>'
                     f'<div class="meta">{ago(a["ts"])} · {e(a["ts"][11:19])}</div></div></div>')
    return f'<div class="cf-feed">{"".join(items)}</div>' if items else '<div class="cf-empty">No activity yet</div>'


def empty_state(icon: str, title: str, text: str):
    st.markdown(f'<div class="cf-card cf-empty"><div class="big">{icon}</div><h3>{title}</h3><div>{text}</div></div>',
                unsafe_allow_html=True)


def parse_flags(flags_json) -> list:
    try:
        return json.loads(flags_json or "[]")
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------- charts
def style(fig: go.Figure, title: str = None, height: int = 320, legend: bool = False) -> go.Figure:
    fig.update_layout(
        title=dict(text=f"<b>{title}</b>" if title else None, font=dict(size=15, color=INK), x=0.01, y=0.96),
        height=height, margin=dict(l=8, r=12, t=52 if title else 12, b=8),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family="Inter, Segoe UI, system-ui, sans-serif", size=12.5, color=INK_2),
        hoverlabel=dict(bgcolor=SURFACE, bordercolor="#e2e8f0", font=dict(color=INK, size=12.5)),
        showlegend=legend, barcornerradius=4, bargap=0.35,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, title=None, font=dict(size=12)),
    )
    fig.update_xaxes(gridcolor=GRID, linecolor=AXIS, zeroline=False, tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, zeroline=False, tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    hbars = [t for t in fig.data if t.type == "bar" and t.orientation == "h" and t.x is not None and len(t.x)]
    if hbars and fig.layout.barmode != "stack":
        top = max(max(t.x) for t in hbars)
        fig.update_xaxes(range=[0, top * 1.18 if top else 1])
    return fig


def gauge(score, height: int = 190) -> go.Figure:
    score = 0 if score is None or score != score else int(score)
    color = STATUS_HEX["good"] if score < 30 else STATUS_HEX["warning"] if score < 85 else STATUS_HEX["critical"]
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score, number=dict(suffix="/100", font=dict(size=26, color=INK)),
        title=dict(text="Risk score", font=dict(size=13, color=INK_2)),
        gauge=dict(axis=dict(range=[0, 100], tickwidth=1, tickcolor=AXIS, tickfont=dict(size=10, color=MUTED)),
                   bar=dict(color=color, thickness=0.32), bgcolor="#f8fafc", borderwidth=0,
                   steps=[dict(range=[0, 30], color=tint(STATUS_HEX["good"], .12)),
                          dict(range=[30, 85], color=tint(STATUS_HEX["warning"], .14)),
                          dict(range=[85, 100], color=tint(STATUS_HEX["critical"], .14))])))
    fig.update_layout(height=height, margin=dict(l=18, r=18, t=36, b=4), paper_bgcolor=SURFACE,
                      font=dict(family="Inter, Segoe UI, sans-serif"))
    return fig
