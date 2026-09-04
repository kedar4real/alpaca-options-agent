"""The Volatility Arbiter — Post-Competition Audit Dashboard.

A READ-ONLY retrospective evidence viewer for hackathon judges. It renders what
the agent did and — more importantly — what it *declined* to do, straight from
the on-disk artifacts. Nothing here can place, cancel, or modify an order.

Run:
    streamlit run audit/dashboard.py
"""

from __future__ import annotations

import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

try:  # package import (pytest / -m) or flat import (`streamlit run`)
    from audit import audit_data as ad
except ImportError:  # pragma: no cover
    import audit_data as ad

# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
ROOT = Path(os.environ.get("AUDIT_ROOT", Path(__file__).resolve().parent.parent))
SESSION_PATH = ROOT / "session.json"
ACTIVITY_PATH = ROOT / "logs" / "agent_activity.log"
AGENT_LOG_PATH = ROOT / "logs" / "agent.log"
AUDIT_MD_PATH = ROOT / "REPORTS" / "FINAL_SESSION_AUDIT.md"
SNAPSHOT_PATH = ROOT / "REPORTS" / "audit_snapshot.json"
JOURNAL_PATH = ROOT / "journal.md"

BASKET = ("SPY", "QQQ", "IWM")
AGENT_NAME = "THE VOLATILITY ARBITER"
AGENT_VERSION = "v1.0 · session PA3FCNG4S7EO · Sep 1–4 2026"

st.set_page_config(
    page_title="Volatility Arbiter — Post-Competition Audit",
    page_icon="⚖️",
    layout="wide",
)

# --------------------------------------------------------------------------- #
# 1. THE SKIN — professional CSS
# --------------------------------------------------------------------------- #
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

    :root { color-scheme: light; }

    /* --- force a light, high-contrast canvas on every machine ------------- */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    [data-testid="stHeader"] { background: #f7f8fa !important; }

    [data-testid="stSidebar"] { background: #eceff4 !important; }
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] li,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3, [data-testid="stSidebar"] h4 { color: #2b2f3a !important; }

    /* base font + ink — cascades to spans WITHOUT touching icon-font elements */
    .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        color: #2b2f3a;
    }
    .stApp p, .stApp li, .stApp label, .stApp td, .stApp th,
    .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
    .stMarkdown, [data-testid="stMarkdownContainer"] { color: #2b2f3a; }
    .stApp [data-testid="stCaptionContainer"],
    .stApp [data-testid="stCaptionContainer"] * { color: #5c6472 !important; }
    /* never restyle Streamlit's Material icon glyphs */
    [data-testid="stIconMaterial"], .material-symbols-rounded, span[class*="material"] {
        font-family: 'Material Symbols Rounded', 'Material Symbols Outlined' !important;
    }

    code, pre, [data-testid="stMetricValue"] {
        font-family: 'IBM Plex Mono', ui-monospace, monospace !important;
    }
    div[data-testid="stMetricValue"] { font-size: 26px; font-weight: 600; color: #1f2430; }
    div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] * {
        color: #5c6472 !important; text-transform: uppercase;
        font-size: 11px; letter-spacing: 0.05em;
    }

    /* dataframes / tables */
    [data-testid="stDataFrame"], [data-testid="stTable"] { background: #ffffff; }

    /* code blocks — light terminal look, dark text */
    [data-testid="stCode"], .stCodeBlock, pre {
        background-color: #f0f2f6 !important;
        border-radius: 8px !important;
        border: 1px solid #e0e0e0 !important;
    }
    [data-testid="stCode"] * , .stCodeBlock * { color: #2b2f3a; }

    /* --- slim institutional status bar (always dark, self-contained) ----- */
    .status-bar {
        display: flex; justify-content: space-between; align-items: center;
        gap: 16px; flex-wrap: wrap;
        background: #1f2430; border: 1px solid #2c3342; border-radius: 8px;
        padding: 8px 16px; margin-bottom: 18px;
        font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; letter-spacing: 0.02em;
    }
    .status-bar, .status-bar * { color: #e7e9ee !important; }
    .status-bar .mid span {
        background: #2b3342; border: 1px solid #3a4356;
        padding: 3px 9px; border-radius: 5px; margin: 0 3px;
    }
    .status-bar .mid span, .status-bar .mid span * { color: #cfd4de !important; }
    .status-bar .right, .status-bar .right * { color: #f0b45f !important; font-weight: 600; }
    .status-bar.amber .mid span, .status-bar .amber span,
    .status-bar.amber .mid span * { color: #f4c27a !important; border-color: #6b5321; background: #33291b; }

    /* --- invariant cards (the cage) ------------------------------------- */
    .invariant {
        background: #eef1f6; border: 1px solid #dde2ec; border-left: 3px solid #4a5a80;
        border-radius: 6px; padding: 8px 12px; margin-bottom: 8px;
        font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; color: #2b2f3a;
    }
    .invariant, .invariant b { color: #2b2f3a !important; }
    .invariant span { color: #5c6472 !important; }

    /* --- the atomic-fix note box -------------------------------------- */
    .note-box {
        background: #fbf3e4; border: 1px solid #ecdcae; border-left: 3px solid #cf9b4a;
        border-radius: 6px; padding: 12px 14px; font-size: 13px;
    }
    .note-box, .note-box * { color: #5a4a2a !important; }
    .note-box code { background: #f2e6cd !important; border: none !important; }

    /* --- chat-message soft cards for the debate ----------------------- */
    [data-testid="stChatMessage"] {
        background: #ffffff !important; border: 1px solid #e4e7ec;
        border-radius: 10px; padding: 10px 14px; margin-bottom: 8px;
    }
    [data-testid="stChatMessage"] p, [data-testid="stChatMessage"] li,
    [data-testid="stChatMessage"] span:not(.verdict-pill) { color: #2b2f3a; }

    .verdict-pill {
        padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 700;
        letter-spacing: 0.04em; font-family: 'IBM Plex Mono', monospace;
    }
    .pill-approve { background: #dcecdf !important; color: #2f6b45 !important; border: 1px solid #b6d4c0; }
    .pill-veto    { background: #f3dcdc !important; color: #963f43 !important; border: 1px solid #e0bebe; }
    .pill-na      { background: #e6e8ee !important; color: #565f6e !important; border: 1px solid #d3d7df; }

    .role-bull  { color: #2f6b45 !important; font-weight: 700; }
    .role-bear  { color: #963f43 !important; font-weight: 700; }
    .role-judge { color: #3f4d72 !important; font-weight: 700; }

    /* de-clutter the top chrome but keep it reachable (Settings -> Theme) */
    [data-testid="stToolbar"] { background: transparent !important; }
    [data-testid="stDecoration"] { display: none; }
    footer { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# data (cached against file mtimes — a frozen snapshot that still refreshes)
# --------------------------------------------------------------------------- #
def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def load_all(_sig: tuple[float, ...]) -> dict:
    session = ad.load_session(SESSION_PATH)
    activity = ad.load_text(ACTIVITY_PATH)
    agent_log = ad.load_text(AGENT_LOG_PATH)
    audit_md = ad.load_text(AUDIT_MD_PATH)
    snapshot = ad.load_json(SNAPSHOT_PATH)
    closing_equity = float(snapshot.get("equity") or session.get("starting_equity") or 0.0)
    return {
        "session": session,
        "activity": activity,
        "agent_log": agent_log,
        "audit_md": audit_md,
        "snapshot": snapshot,
        "closing_equity": closing_equity,
        "summary": ad.account_summary(session, closing_equity, force_stopped_flat=True),
        "context": ad.last_market_context(activity),
        "tickers": ad.ticker_metrics(activity, BASKET),
        "debates": ad.parse_debates(audit_md=audit_md, activity_text=activity),
        "decision_counts": ad.decision_log_stage_counts(agent_log),
        "veto": ad.veto_ratio(activity),
        "regimes": ad.regime_breakdown(activity),
        "trades": ad.trade_history(session),
        "open_pos": ad.open_positions_detail(session),
        "run_mode": ad.latest_run_mode(activity),
        "equity_series": ad.equity_series(agent_log, session, snapshot),
    }


sig = tuple(_mtime(p) for p in
            (SESSION_PATH, ACTIVITY_PATH, AGENT_LOG_PATH, AUDIT_MD_PATH, SNAPSHOT_PATH))
D = load_all(sig)
S = D["summary"]
ctx = D["context"]

# --------------------------------------------------------------------------- #
# 2. THE HEADER — system heartbeat / status bar
# --------------------------------------------------------------------------- #
if ctx:
    term = ctx["state"].upper()
    amber = ctx["state"] == "backwardation"
    mid = (
        f"<span>VIX: {ctx['vix']:.2f}</span>"
        f"<span>VXV·3M: {ctx['vxv']:.2f}</span>"
        f"<span>RATIO: {ctx['ratio']:.2f}</span>"
        f"<span>TERM: {term}</span>"
    )
else:
    amber = False
    mid = "<span>VIX: n/a</span><span>TERM: n/a</span>"

st.markdown(
    f"""
    <div class="status-bar">
      <div class="left"><b>{AGENT_NAME}</b> &nbsp;·&nbsp; {AGENT_VERSION}</div>
      <div class="mid {'amber' if amber else ''}">{mid}</div>
      <div class="right">[{S['account_id'] or 'PA3FCNG4S7EO'}] — STOPPED / FLAT</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# 2b. EQUITY CURVE — the headline: where the cage held the line
# --------------------------------------------------------------------------- #
_eq = D["equity_series"]
if len(_eq) >= 2:
    _MON = {"01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
            "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec"}

    def _lbl(p: dict) -> str:
        y, m, d = (p["date"].split("-") + ["", "", ""])[:3]
        tag = {"start": "start", "logged": "close", "snapshot": "now"}.get(p["source"], p["source"])
        return f"{_MON.get(m, m)} {int(d):02d} · {tag}" if m and d else p["source"]

    eq_df = pd.DataFrame(_eq)
    eq_df["label"] = eq_df.apply(_lbl, axis=1)
    eq_df["anchor"] = eq_df["source"].map({"start": "anchor", "snapshot": "anchor"}).fillna("logged")
    eq_df["tag"] = eq_df["equity"].map(lambda v: f"${v:,.0f}")
    lo = float(min(p["equity"] for p in _eq))
    hi = float(max(p["equity"] for p in _eq))
    order = list(eq_df["label"])

    _pad = max((hi - lo) * 0.35, 150)
    _x = alt.X("label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0, grid=False))
    _y = alt.Y("equity:Q", title="account equity ($)",
               scale=alt.Scale(zero=False, domain=[round(lo - _pad), round(hi + _pad)]),
               axis=alt.Axis(format="$,.0f", grid=True))
    _tt = [alt.Tooltip("label:N", title="point"),
           alt.Tooltip("equity:Q", title="equity", format="$,.2f"),
           alt.Tooltip("day_pnl:Q", title="Δ vs prev", format="+$,.0f"),
           alt.Tooltip("source:N", title="source")]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Starting equity", f"${S['starting']:,.0f}")
    m2.metric("Final equity", f"${S['current']:,.0f}")
    m3.metric("Net P&L", f"{S['pnl_pct']:+.2f}%", delta=f"{S['pnl_abs']:+,.0f} USD",
              delta_color="inverse")
    floor_gap = lo - ad.SAFETY_FLOOR_USD
    m4.metric("Headroom to $95k floor", f"${floor_gap:,.0f}",
              help="Lowest equity in the window minus the hard floor — the panic line was never approached.")

    eq_chart = (
        alt.Chart(eq_df)
        .mark_line(
            color="#2f3a5c", strokeWidth=3, interpolate="monotone",
            point={"size": 170, "color": "#2f3a5c", "filled": True,
                   "stroke": "#ffffff", "strokeWidth": 1.5},
        )
        .encode(x=_x, y=_y, tooltip=_tt)
        .properties(height=260)
        .configure_view(fill="#ffffff", stroke="#e4e7ec")
        .configure_axis(labelColor="#5c6472", titleColor="#5c6472")
    )
    st.altair_chart(eq_chart, width="stretch")
    st.caption(
        "Points labelled **· close** are the agent's own end-of-day **DAILY PERFORMANCE "
        "SUMMARY** equity marks from `logs/agent.log` — directly logged, not reconstructed "
        "or interpolated. **· start** is the session's `starting_equity`; **· now** is the "
        f"live `audit_snapshot.json` (${S['current']:,.0f}). Dates are the trading session; "
        "the underlying log timestamps are log-local time (IST on the box; ET = IST − 9:30)."
    )
    st.divider()

# --------------------------------------------------------------------------- #
# A. SIDEBAR — the Account Seal
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("## The Volatility Arbiter")
    st.caption("Post-Competition Audit")
    st.error("System Status: STOPPED / FLAT")

    st.metric("Starting Equity", f"${S['starting']:,.2f}")
    st.metric(
        "Current Equity",
        f"${S['current']:,.2f}",
        delta=f"{S['pnl_abs']:+,.2f}",
        delta_color="inverse" if S["pnl_abs"] < 0 else "normal",
    )
    st.metric("P&L", f"{S['pnl_pct']:+.2f}%", delta=f"{S['pnl_abs']:+,.0f} USD")

    st.divider()
    st.markdown("#### Safety Invariants — the cage")
    st.markdown(
        f'<div class="invariant">HARD FLOOR&nbsp;&nbsp;&nbsp;&nbsp;${ad.SAFETY_FLOOR_USD:,.0f} equity'
        f'<br><span style="color:#667085">→ panic-flatten + sticky halt below this line</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="invariant">PER-TRADE CAP&nbsp;&nbsp;{ad.PER_TRADE_CAP_PCT:.1f}% of live equity'
        f'<br><span style="color:#667085">→ max defined loss, re-checked at submit</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="invariant">DEFINED RISK ONLY<br>'
        '<span style="color:#667085">→ verticals / condors — never a naked leg</span></div>',
        unsafe_allow_html=True,
    )

    st.divider()
    st.button(
        "🛑 Emergency Flatten",
        disabled=True,
        help="Agent is already flat and stopped.",
        width='stretch',
    )
    st.caption("Read-only audit view — no control surface is wired.")

# --------------------------------------------------------------------------- #
# main — three columns
# --------------------------------------------------------------------------- #
st.markdown("### Post-Competition Audit — evidence viewer")
st.caption(
    "Retrospective, read-only. Every number below is parsed from `session.json`, "
    "`logs/agent_activity.log`, and `REPORTS/FINAL_SESSION_AUDIT.md`."
)

col1, col2, col3 = st.columns([1.05, 1.25, 1.15], gap="large")

# ----- Column 1: Market Context (the environment) ----- #
with col1:
    st.markdown("#### 1 · Market Context — the environment")

    if ctx:
        ts_df = pd.DataFrame(
            {"tenor": ["VIX · 1M", "VXV · 3M"], "vol": [ctx["vix"], ctx["vxv"]], "order": [0, 1]}
        )
        inverted = ctx["vix"] > ctx["vxv"]
        line_color = "#cf9b4a" if inverted else "#4a5a80"
        chart = (
            alt.Chart(ts_df)
            .mark_line(point=alt.OverlayMarkDef(size=90, filled=True), strokeWidth=2.5, color=line_color)
            .encode(
                x=alt.X("tenor:N", sort=["VIX · 1M", "VXV · 3M"], title=None,
                        axis=alt.Axis(labelAngle=0, grid=False)),
                y=alt.Y("vol:Q", title="implied vol", scale=alt.Scale(zero=False),
                        axis=alt.Axis(grid=True)),
                tooltip=["tenor", alt.Tooltip("vol:Q", format=".2f")],
            )
            .properties(height=190)
        )
        bg = "#fbf3e4" if inverted else "#ffffff"
        chart = chart.configure_view(fill=bg, stroke="#e4e7ec").configure_axis(
            labelColor="#667085", titleColor="#667085"
        )
        st.altair_chart(chart, width='stretch')
        state_txt = (
            "⚠️ **INVERSION / BACKWARDATION** — short-dated vol bid over long-dated. "
            "Panic-regime signal: short-volatility structures are vetoed."
            if inverted
            else "✅ **CONTANGO** — the normal, stable term structure. Not a veto condition."
        )
        st.markdown(state_txt)
        if ctx.get("ts"):
            st.caption(f"Last reading logged {ctx['ts']} (log local time)")
    else:
        st.info("No MARKET CONTEXT block found in the activity log.")

    st.markdown("**Basket — last cycle**")
    tick_df = pd.DataFrame(D["tickers"])[["symbol", "price", "rsi", "rsi_label", "iv", "rv"]]
    tick_df.columns = ["Ticker", "Price", "RSI", "Zone", "IV", "RV"]
    st.dataframe(
        tick_df.style.format(
            {"Price": "{:.2f}", "RSI": "{:.1f}", "IV": "{:.3f}", "RV": "{:.3f}"},
            na_rep="—",
        ),
        hide_index=True,
        width='stretch',
    )
    st.caption("SPY/QQQ/IWM — final RSI from MARKET CONTEXT, IV/RV from the last SCAN TABLE.")

# ----- Column 2: The Logic (the brain) ----- #
with col2:
    st.markdown("#### 2 · The Logic — the brain")

    debates = D["debates"]
    st.markdown(f"**Multi-Agent Debate** &nbsp;·&nbsp; {len(debates)} transcript(s) on file")
    if debates:
        labels = [
            f"{i+1:>2}. {d['symbol']}  ·  {d['when']}  ·  {d['outcome'].upper()}"
            for i, d in enumerate(debates)
        ]
        default_idx = next(
            (i for i, d in reversed(list(enumerate(debates))) if d["outcome"] == "vetoed"),
            len(debates) - 1,
        )
        pick = st.selectbox("Transcript", options=range(len(debates)),
                            format_func=lambda i: labels[i], index=default_idx)
        d = debates[pick]
        if d.get("pipeline"):
            st.caption(f"Pipeline outcome — {d['pipeline']}")

        _AVA = {"BULL": "🐂", "BEAR": "🐻", "JUDGE": "⚖️"}
        _ROLECLS = {"BULL": "role-bull", "BEAR": "role-bear", "JUDGE": "role-judge"}
        if not d["rounds"]:
            st.warning("Transcript on file is empty or corrupt (LLM returned no parseable text).")
        for r in d["rounds"]:
            with st.chat_message(r["role"].lower(), avatar=_AVA.get(r["role"], "•")):
                v = r["verdict"]
                pill = (
                    f'<span class="verdict-pill pill-approve">APPROVE</span>' if v == "APPROVE"
                    else f'<span class="verdict-pill pill-veto">VETO</span>' if v == "VETO"
                    else '<span class="verdict-pill pill-na">NO VERDICT</span>'
                )
                prov = f' <span style="color:#98a2b3">({r["provider"]})</span>' if r["provider"] else ""
                st.markdown(
                    f'<span class="{_ROLECLS.get(r["role"], "")}">{r["role"]}</span>{prov} &nbsp; {pill}',
                    unsafe_allow_html=True,
                )
                st.markdown(r["thesis"] or "_(no thesis text)_")
    else:
        st.info("No Bull/Bear/Judge transcripts found.")

    st.divider()
    _dc = D["decision_counts"]
    _dc_total = sum(_dc.values())
    st.markdown(f"**Decision Log** &nbsp;·&nbsp; {_dc_total:,} pipeline decisions, full reasons")
    _stage_opts = ["all"] + [s for s in
                             ("precheck", "strategy", "risk_manager", "risk_officer", "executor")
                             if _dc.get(s)]
    _fmt = (lambda s: f"all · {_dc_total:,}" if s == "all" else f"{s} · {_dc.get(s, 0):,}")
    _pick = st.segmented_control("Pipeline stage", _stage_opts, format_func=_fmt,
                                 default="all", key="dlog_stage") or "all"
    _rows = ad.decision_log(D["agent_log"], stage=None if _pick == "all" else _pick, limit=40)
    if _rows:
        dl_df = pd.DataFrame(_rows)[["ts", "symbol", "outcome", "stage", "reason"]]
        dl_df["ts"] = dl_df["ts"].str.slice(0, 16)
        dl_df.columns = ["When (log-local)", "Sym", "Call", "Stage", "Reason (full)"]
        st.dataframe(
            dl_df, hide_index=True, width="stretch", height=340,
            column_config={"Reason (full)": st.column_config.TextColumn(width="large")},
        )
        st.caption(
            "Full `DECISION SUMMARY` text from `logs/agent.log` — untruncated, newest first "
            "(the SCAN TABLE in `agent_activity.log` clips these at ~72 chars). "
            "`When` is log-local time (IST; ET = IST − 9:30)."
        )
    else:
        st.info("No DECISION SUMMARY lines found in logs/agent.log.")
    v = D["veto"]
    if v:
        approved = v.get("approved") or 0
        gate = v.get("gate_vetoes") or 0
        ai = v.get("ai_vetoes") or 0
        prop = v.get("proposed") or (approved + gate + ai)
        st.caption(
            f"Session totals — {v.get('scans','?')} scans → {prop} proposed → "
            f"**{gate} killed by the Gate**, {ai} vetoed by the AI, {approved} approved. "
            "The agent's default answer is No."
        )

# ----- Column 3: The Evidence (the audit) ----- #
with col3:
    st.markdown("#### 3 · The Evidence — the audit")

    trades = D["trades"]
    st.markdown(f"**Trade History** &nbsp;·&nbsp; {len(trades)} lifecycle record(s)")
    if trades:
        tr_df = pd.DataFrame(trades)
        show = tr_df[["when", "symbol", "structure", "qty", "credit", "width",
                      "order_risk", "max_risk_allowed", "officer_provider", "officer_approved"]].copy()
        show["when"] = show["when"].str.slice(0, 16).str.replace("T", " ")
        show.columns = ["When", "Sym", "Structure", "Qty", "Credit", "Width",
                        "Risk $", "Cap $", "Officer", "OK?"]
        st.dataframe(
            show.style.format(
                {"Credit": "{:.2f}", "Width": "{:.2f}", "Risk $": "{:,.0f}", "Cap $": "{:,.0f}"},
                na_rep="—",
            ),
            hide_index=True, width='stretch', height=300,
        )
        st.caption(
            "Every `Risk $` sits under its `Cap $` (1.5% of live equity at submit time) — "
            "the per-trade invariant, enforced on every fill."
        )
    else:
        st.info("No trades recorded in session.json history.")

    if D["open_pos"]:
        st.markdown("**Still-open at session end** (auto-flatten pending at the next open)")
        op_df = pd.DataFrame(D["open_pos"])[["symbol", "structure", "expiry", "qty", "entry_credit"]]
        op_df.columns = ["Sym", "Structure", "Expiry", "Qty", "Credit"]
        st.dataframe(op_df, hide_index=True, width='stretch')

    st.markdown(
        '<div class="note-box"><b>NOTE — Execution Invariant.</b> Trade execution was '
        "halted following the identification of bid-ask friction in leg-by-leg closes. "
        "Commit <code>b83438d</code> (Atomic MLEG) was implemented as the final system "
        "invariant: every exit is now a single reversing multi-leg order, so an unwind "
        "can never strand one leg filled and another open.</div>",
        unsafe_allow_html=True,
    )

# --------------------------------------------------------------------------- #
# 4. THE QUANT EDGE — "the Gate is the Hero"
# --------------------------------------------------------------------------- #
st.divider()
st.markdown("### The Gate is the Hero — where 165 proposals went")

gcol, rcol = st.columns([1, 1], gap="large")

with gcol:
    v = D["veto"]
    executed = len([t for t in D["trades"] if t.get("kind") in ("opened", "closed")]) or (v.get("approved") if v else 0)
    if v:
        funnel = pd.DataFrame(
            {
                "stage": ["Rejected by Gate (risk_manager)", "Vetoed by AI (risk_officer)", "Approved / Executed"],
                "count": [v.get("gate_vetoes") or 0, v.get("ai_vetoes") or 0, v.get("approved") or 0],
                "kind": ["gate", "ai", "exec"],
            }
        )
        bar = (
            alt.Chart(funnel)
            .mark_bar(height=34, cornerRadiusEnd=4)
            .encode(
                x=alt.X("count:Q", title="count", axis=alt.Axis(grid=True)),
                y=alt.Y("stage:N", sort=list(funnel["stage"]), title=None),
                color=alt.Color(
                    "kind:N",
                    scale=alt.Scale(domain=["gate", "ai", "exec"],
                                    range=["#4a5a80", "#cf9b4a", "#5b8c6e"]),
                    legend=None,
                ),
                tooltip=["stage", "count"],
            )
            .properties(height=170)
            .configure_view(stroke="#e4e7ec")
            .configure_axis(labelColor="#667085", titleColor="#667085")
        )
        st.altair_chart(bar, width='stretch')
        st.caption(
            f"Of **{v.get('proposed','?')}** structures the strategy proposed, the deterministic "
            f"risk gate killed **{v.get('gate_vetoes','?')}** before any LLM was consulted. "
            "The AI debate is the second line, not the first."
        )
    else:
        st.info("No NIGHTLY POST-MORTEM block found to build the funnel.")

with rcol:
    st.markdown("**Regime mix — what the quant brain saw**")
    rb = D["regimes"]
    if rb:
        rdf = pd.DataFrame(rb, columns=["Regime", "Cycles"]).head(8)
        st.dataframe(rdf, hide_index=True, width='stretch', height=300)
    else:
        st.info("No regime breakdown in the post-mortem.")
    if D["run_mode"]:
        with st.expander("Final RUN MODE banner (the configured cage)"):
            st.code("\n".join(D["run_mode"]), language="text")

# --------------------------------------------------------------------------- #
# 5. EXECUTION AUDIT — the Atomic Fix diff
# --------------------------------------------------------------------------- #
st.divider()
st.markdown("### Incident Response: Infrastructure Hardening (Commit `b83438d`)")
st.markdown(
    "During the final session a routine unwind was rejected mid-flight: closing a "
    "short leg before its long left the account **transiently naked**, the broker "
    "rejected the next legs (*“account not eligible to trade uncovered option "
    "contracts”*), and the remaining legs were stranded as orphans. Fix: **one "
    "reversing `OrderClass.MLEG` market order** — the broker fills or rejects the "
    "combo as a unit, so a partial unwind is structurally impossible."
)

old_col, new_col = st.columns(2, gap="large")
with old_col:
    st.markdown("**✗ Before — leg-by-leg close**")
    st.code(
        '''def close_condor(self, condor):
    # one close_position() call per leg, in list order
    for leg in condor.legs:
        if not leg.symbol:
            continue
        self.trading.close_position(leg.symbol)
        # ↑ closes a SHORT before its LONG → account is
        #   transiently naked → broker rejects the rest →
        #   orphan legs, leg-by-leg fire sale''',
        language="python",
    )
with new_col:
    st.markdown("**✓ After — atomic MLEG (`b83438d`)**")
    st.code(
        '''_CLOSE_SIDE = {"buy": OrderSide.SELL, "sell": OrderSide.BUY}

def build_close_request(legs, quantity):
    option_legs = [
        OptionLegRequest(symbol=leg.symbol,
                         side=_CLOSE_SIDE[leg.action],
                         ratio_qty=1)
        for leg in legs
    ]
    return MarketOrderRequest(
        qty=int(quantity),
        order_class=OrderClass.MLEG,      # fills/rejects as ONE unit
        time_in_force=TIME_IN_FORCE,
        legs=option_legs,
    )

def close_condor(self, condor):
    legs = [lg for lg in condor.legs if lg.symbol]
    self.trading.submit_order(
        build_close_request(legs, condor.quantity))
    # fallback only if the combo submit itself fails:
    # leg out SHORTS-first so it is never naked''',
        language="python",
    )

st.caption(
    "Market, not limit: a forced exit (profit target, stop, expiry, hard-stop flatten) "
    "has to fill, and on a defined-risk structure the worst case is already the known max loss."
)

# honest footnote — the audit view forces STOPPED/FLAT for the retrospective framing
snap = D["snapshot"]
if snap:
    st.caption(
        f"Snapshot captured {snap.get('captured_at','?')} · equity "
        f"${snap.get('equity',0):,.2f} · {len(snap.get('position_legs') or [])} option legs on the book "
        f"at capture ({snap.get('captured_at_note','')}). This audit view fixes the status "
        "as STOPPED / FLAT for the post-competition retrospective."
    )
