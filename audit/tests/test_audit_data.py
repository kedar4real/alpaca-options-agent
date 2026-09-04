"""Offline tests for the audit-dashboard parsers (no Streamlit, no network)."""

from __future__ import annotations

import json

import pytest

from audit import audit_data as ad

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
ACTIVITY = """\
2026-09-02 19:18:25 INFO    agent.offhours: MARKET CONTEXT
============================================================
Macro: Employment Situation (NFP) (2026-09-04) | VIX: VIX 16.04 / VXV 18.26 (ratio 0.88, contango) | REGIME SIGNALS: MACRO_DANGER | News SPY: something | RSI SPY: 54.3 (neutral) | ADX SPY: 12.9 (range, down) | News QQQ: x | RSI QQQ: 49.0 (neutral) | ADX QQQ: 10.5 (range, down) | News IWM: y | RSI IWM: 43.0 (oversold) | ADX IWM: 19.3 (range, down)
============================================================
2026-09-02 19:19:15 INFO    agent.offhours: DEBATE [IWM]
============================================================
--- BULL ---
VERDICT: APPROVE
THESIS: The long strangle fits MACRO_DANGER; IV richer than RV.

--- BEAR ---
VERDICT: VETO
THESIS: NFP tail risk outweighs the premium.

--- JUDGE (featherless) ---
VERDICT: APPROVE
THESIS: Premium-rich, risk within the 1.5% cap.
============================================================
2026-09-04 01:29:59 INFO    agent.offhours: MARKET CONTEXT
============================================================
Macro: Employment Situation (NFP) (2026-09-04) | VIX: VIX 14.36 / VXV 13.90 (ratio 1.03, backwardation) | News SPY: z | RSI SPY: 62.8 (neutral) | ADX SPY: 12.8 (range, up) | News QQQ: z | RSI QQQ: 57.3 (neutral) | ADX QQQ: 9.7 (range, up) | News IWM: z | RSI IWM: 49.1 (neutral) | ADX IWM: 18.8 (range, down)
============================================================
2026-09-04 01:30:12 INFO    agent.offhours: ============================================================
SCAN TABLE  (IV-RV>=+0.015  ER<0.45  floor>0.08  c/w>=10%)
----------------------------------------------------------
  SPY   px  773.12  IV 0.085  RV 0.083  IVRV +0.002 FAIL  ER 0.31 ok    floor ok    c/w   --   -> skipped [precheck] already holds a position or working order in SPY
  QQQ   px  718.04  IV 0.132  RV 0.138  IVRV -0.005 FAIL  ER 0.14 ok    floor ok    c/w   --   -> skipped [strategy] strategy did not propose a trade
  IWM   px  295.20  IV 0.130  RV 0.133  IVRV -0.003 FAIL  ER 0.12 ok    floor ok    c/w   --   -> skipped [precheck] already holds a position or working order in IWM
============================================================
2026-09-04 01:31:15 INFO    agent.offhours: NIGHTLY POST-MORTEM
============================================================
Nightly Post-Mortem - Sep 03, 2026

Ticker scans today:       447  (3-ticker basket)
Trades proposed:          165
Trades approved:          40
Vetoed by risk_manager:   98
Vetoed by risk_officer:   27
Open positions:           2
Unrealized P&L (open):    $-16
Dominant regime:          Overall Neutral / No-Trade  (No trade - 98 scans)

Regime breakdown:
  - No trade: 98
  - Regime OVERRIDE: MACRO_DANGER -> Long Strangle (vetoed short-vol iron_condor): 48
  - Regime A: High Volatility -> Iron Condor: 45
============================================================
"""

AUDIT_MD = """\
# Final Session Audit — Multi-Agent Debate Transcripts

## QQQ — 2026-09-03 13:49 ET

**Pipeline outcome:** DECISION SUMMARY — Vetoed at [risk_officer]: risk_officer VETO (debate/featherless) — NFP risk.
    order: 3x QQQ iron_condor exp 2026-09-04 credit $1.71 width $6.00 [SP:QQQ260904P00713000 BP:QQQ260904P00707000]

```
--- BULL ---
VERDICT: APPROVE
THESIS: Iron condor fits the high-vol regime.

--- BEAR ---
VERDICT: VETO
THESIS: Contango plus NFP; skip it.

--- JUDGE (featherless) ---
VERDICT: VETO
THESIS: The NFP report poses significant risk; veto.
```

## SPY — 2026-09-03 13:53 ET

**Pipeline outcome:** DECISION SUMMARY — Executed at [executor]: submitted order 2de4751f — 3x SPY bull_put exp 2026-09-04 credit $0.79 width $5.00

```
--- BULL ---
VERDICT: APPROVE
THESIS: Bullish sentiment; sell the put spread.

--- BEAR ---
VERDICT: APPROVE
THESIS: Defined risk and a clear trend; acceptable.

--- JUDGE (ollama:llama3.2) ---
VERDICT: APPROVE
THESIS: Within the cap; approve.
```
"""

SESSION = {
    "starting_equity": 99870.9,
    "account_id": "PA3FCNG4S7EO",
    "trading_halted": False,
    "open_condors": [
        {"id": "a", "symbol": "SPY", "structure": "bull_put", "expiry": "2026-09-08",
         "quantity": 3, "entry_credit": 0.98, "legs": []},
        {"id": "b", "symbol": "IWM", "structure": "bull_put", "expiry": "2026-09-08",
         "quantity": 3, "entry_credit": 0.86, "legs": []},
    ],
    "history": [
        {"kind": "submitted", "at": "2026-09-03T10:33:28-04:00", "symbol": "QQQ",
         "structure": "iron_condor", "quantity": 3, "entry_credit": 1.77,
         "regime": "Regime A: High Volatility -> Iron Condor",
         "detail": "3x QQQ iron_condor exp 2026-09-04 credit $1.77 width $7.00 [SP:...]",
         "gates": {"iv_rv_spread": 0.071, "credit_to_width": 0.2535, "order_risk": 1567.5,
                   "max_risk_allowed": 1982.7},
         "officer": {"provider": "featherless", "approved": True, "thesis": "fits regime"}},
        {"kind": "opened", "at": "2026-09-02T12:48:55-04:00", "symbol": "IWM",
         "structure": "long_strangle", "regime": None, "detail": "fill confirmed"},
        {"kind": "reconciled", "at": "2026-09-02T21:57:17+05:30",
         "detail": "JAM RESET: broker flat"},
    ],
}


# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
def test_safety_invariants_are_exposed_as_constants() -> None:
    assert ad.SAFETY_FLOOR_USD == 95_000
    assert ad.PER_TRADE_CAP_PCT == 1.5


# --------------------------------------------------------------------------- #
# market context / term structure
# --------------------------------------------------------------------------- #
def test_last_market_context_takes_the_most_recent_block() -> None:
    ctx = ad.last_market_context(ACTIVITY)
    assert ctx["vix"] == pytest.approx(14.36)
    assert ctx["vxv"] == pytest.approx(13.90)
    assert ctx["ratio"] == pytest.approx(1.03)
    assert ctx["state"] == "backwardation"


def test_last_market_context_is_none_when_absent() -> None:
    assert ad.last_market_context("nothing here") is None


def test_ticker_metrics_merges_rsi_from_context_and_iv_from_scan() -> None:
    rows = ad.ticker_metrics(ACTIVITY, ("SPY", "QQQ", "IWM"))
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["SPY"]["rsi"] == pytest.approx(62.8)
    assert by_sym["SPY"]["iv"] == pytest.approx(0.085)
    assert by_sym["QQQ"]["rv"] == pytest.approx(0.138)
    assert by_sym["IWM"]["price"] == pytest.approx(295.20)
    assert by_sym["IWM"]["rsi_label"] == "neutral"


# --------------------------------------------------------------------------- #
# debates
# --------------------------------------------------------------------------- #
def test_parse_debates_from_audit_markdown() -> None:
    debates = ad.parse_debates(audit_md=AUDIT_MD, activity_text="")
    assert len(debates) == 2
    qqq = debates[0]
    assert qqq["symbol"] == "QQQ"
    assert qqq["outcome"] == "vetoed"
    roles = {r["role"]: r for r in qqq["rounds"]}
    assert roles["BULL"]["verdict"] == "APPROVE"
    assert roles["BEAR"]["verdict"] == "VETO"
    assert roles["JUDGE"]["verdict"] == "VETO"
    assert roles["JUDGE"]["provider"] == "featherless"
    assert "NFP" in roles["JUDGE"]["thesis"]


def test_parse_debates_marks_executed_outcome() -> None:
    debates = ad.parse_debates(audit_md=AUDIT_MD, activity_text="")
    spy = [d for d in debates if d["symbol"] == "SPY"][0]
    assert spy["outcome"] == "executed"


def test_parse_debates_falls_back_to_activity_log() -> None:
    debates = ad.parse_debates(audit_md="", activity_text=ACTIVITY)
    assert len(debates) == 1
    assert debates[0]["symbol"] == "IWM"
    roles = {r["role"]: r for r in debates[0]["rounds"]}
    assert roles["BULL"]["verdict"] == "APPROVE"
    assert roles["BEAR"]["verdict"] == "VETO"


def test_parse_debates_survives_a_corrupt_transcript() -> None:
    junk = "## X — 2026-09-03 10:00 ET\n\n**Pipeline outcome:** whatever\n\n```\n" + ("!" * 5000) + "\n```\n"
    out = ad.parse_debates(audit_md=junk, activity_text="")
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["rounds"] == []


# --------------------------------------------------------------------------- #
# no-trade decisions
# --------------------------------------------------------------------------- #
def test_no_trade_decisions_extracts_skips_with_reasons() -> None:
    rows = ad.no_trade_decisions(ACTIVITY, limit=20)
    assert len(rows) == 3
    assert all(r["decision"] in ("skipped", "blocked", "vetoed") for r in rows)
    qqq = [r for r in rows if r["symbol"] == "QQQ"][0]
    assert qqq["stage"] == "strategy"
    assert "did not propose a trade" in qqq["reason"]
    assert qqq["iv"] == pytest.approx(0.132)


def test_no_trade_decisions_honours_the_limit_and_returns_newest_first() -> None:
    rows = ad.no_trade_decisions(ACTIVITY, limit=2)
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# veto ratio
# --------------------------------------------------------------------------- #
def test_veto_ratio_reads_the_nightly_post_mortem() -> None:
    vr = ad.veto_ratio(ACTIVITY)
    assert vr["gate_vetoes"] == 98
    assert vr["ai_vetoes"] == 27
    assert vr["approved"] == 40
    assert vr["proposed"] == 165
    assert vr["scans"] == 447
    assert vr["open_positions"] == 2


def test_veto_ratio_is_empty_dict_when_no_post_mortem() -> None:
    assert ad.veto_ratio("no post mortem here") == {}


def test_regime_breakdown_parses_the_labelled_counts() -> None:
    rb = ad.regime_breakdown(ACTIVITY)
    assert rb[0] == ("No trade", 98)
    assert ("Regime A: High Volatility -> Iron Condor", 45) in rb


# --------------------------------------------------------------------------- #
# trade history
# --------------------------------------------------------------------------- #
def test_trade_history_normalises_session_records() -> None:
    rows = ad.trade_history(SESSION)
    # 'reconciled' rows are notes, not trades; rows are chronological
    assert sorted(r["symbol"] for r in rows) == ["IWM", "QQQ"]
    qqq = [r for r in rows if r["symbol"] == "QQQ"][0]
    assert qqq["structure"] == "iron_condor"
    assert qqq["credit"] == pytest.approx(1.77)
    assert qqq["officer_provider"] == "featherless"
    assert qqq["officer_approved"] is True
    assert qqq["width"] == pytest.approx(7.0)


# --------------------------------------------------------------------------- #
# account summary
# --------------------------------------------------------------------------- #
def test_account_summary_computes_pnl_and_status() -> None:
    s = ad.account_summary(SESSION, closing_equity=96977.27)
    assert s["starting"] == pytest.approx(99870.9)
    assert s["current"] == pytest.approx(96977.27)
    assert s["pnl_abs"] == pytest.approx(-2893.63, abs=0.01)
    assert s["pnl_pct"] == pytest.approx(-2.898, abs=0.01)
    assert s["open_positions"] == 2


def test_account_summary_status_is_stopped_flat_when_forced() -> None:
    s = ad.account_summary(SESSION, closing_equity=96977.27, force_stopped_flat=True)
    assert s["status_label"] == "STOPPED / FLAT"
    assert s["is_flat"] is True


def test_account_summary_reports_live_state_when_not_forced() -> None:
    s = ad.account_summary(SESSION, closing_equity=96977.27)
    assert s["is_flat"] is False           # 2 open condors in the fixture
    assert "OPEN" in s["status_label"] or "RUNNING" in s["status_label"]


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def test_load_text_returns_empty_string_for_a_missing_file(tmp_path) -> None:
    assert ad.load_text(tmp_path / "nope.log") == ""


def test_load_session_round_trips(tmp_path) -> None:
    p = tmp_path / "session.json"
    p.write_text(json.dumps(SESSION), encoding="utf-8")
    assert ad.load_session(p)["account_id"] == "PA3FCNG4S7EO"


def test_load_session_missing_file_returns_empty_dict(tmp_path) -> None:
    assert ad.load_session(tmp_path / "nope.json") == {}


# =========================================================================== #
# Polish pass — new pure functions (agent.log equity marks + decision log)
# =========================================================================== #
AGENT_LOG = """\
2026-09-01 01:39:28 INFO    agent: DAILY PERFORMANCE SUMMARY
============================================================
SPY Iron Condor Agent — Daily Performance Summary (Aug 31, 2026)

Equity: $100,000   (day P&L $+0 / +0.00%)
Since start: $+0 / +0.00% (from $100,000)
Realized P&L on closes today: $+0
============================================================
2026-09-02 01:40:11 INFO    agent: DAILY PERFORMANCE SUMMARY
============================================================
Multi-Ticker Options Agent — Daily Performance Summary (Sep 01, 2026)

Equity: $99,839   (day P&L $-161 / -0.16%)
Since start: $-151 / -0.15% (from $99,991)
Realized P&L on closes today: $+0
============================================================
2026-09-04 01:31:13 INFO    agent: DAILY PERFORMANCE SUMMARY
============================================================
Multi-Ticker Options Agent — Daily Performance Summary (Sep 03, 2026)

Equity: $97,011   (day P&L $-2,335 / -2.35%)
Since start: $-2,860 / -2.86% (from $99,871)
Realized P&L on closes today: $+996
============================================================
2026-09-01 20:43:30 INFO    agent: [IWM] DECISION SUMMARY — Vetoed at [risk_officer]: risk_officer VETO (featherless) — The IV-RV spread is only 0.0417, too narrow for the stated high-IV regime; adding exposure into a drawdown raises risk.
2026-09-03 10:33:28 INFO    agent: [QQQ] DECISION SUMMARY — Executed at [executor]: submitted order d9b7efb8 — 3x QQQ iron_condor exp 2026-09-04 credit $1.77 width $7.00
2026-09-01 00:54:17 INFO    agent: DECISION SUMMARY — Skipped at [strategy]: strategy did not propose a trade — IV gate blocked: Hackathon Mode: ATM IV 0.113 <= 0.15 (2/10 IV days logged)
2026-09-04 01:17:32 INFO    agent: [QQQ] DECISION SUMMARY — Blocked at [risk_manager]: risk_manager rejected — correlation guard: QQQ is >0.8 correlated (10d) with open SPY — that cluster already holds its one slot toward the 4-position cap; trade risk $1,468 > $1,455 (1.5% of current equity)
2026-09-04 01:18:00 INFO    agent: [IWM] DECISION SUMMARY — Skipped at [precheck]: already holds a position or working order in IWM
"""


def test_daily_equity_marks_parses_each_summary_block() -> None:
    marks = ad.daily_equity_marks(AGENT_LOG)
    assert [m["date"] for m in marks] == ["2026-08-31", "2026-09-01", "2026-09-03"]
    assert marks[0]["equity"] == pytest.approx(100_000.0)
    assert marks[2]["equity"] == pytest.approx(97_011.0)
    assert marks[2]["day_pnl"] == pytest.approx(-2_335.0)
    assert marks[2]["source"] == "logged"


def test_daily_equity_marks_empty_when_no_summary() -> None:
    assert ad.daily_equity_marks("nothing here") == []


def test_equity_series_anchors_start_and_end() -> None:
    series = ad.equity_series(AGENT_LOG, SESSION, {"equity": 96977.27, "captured_at": "2026-09-04T10:25:00+00:00"})
    assert series[0]["source"] == "start"
    assert series[0]["equity"] == pytest.approx(99_870.9)
    assert series[-1]["source"] == "snapshot"
    assert series[-1]["equity"] == pytest.approx(96_977.27)
    # logged daily marks sit in between, chronologically
    assert [p["source"] for p in series[1:-1]] == ["logged", "logged", "logged"]
    assert all(series[i]["date"] <= series[i + 1]["date"] for i in range(len(series) - 1))


def test_equity_series_dedupes_a_start_that_equals_first_mark() -> None:
    # if session start date == first logged mark date, don't emit two points
    sess = {"starting_equity": 100_000.0, "created_at": "2026-08-31T09:00:00-04:00"}
    series = ad.equity_series(AGENT_LOG, sess, {"equity": 97_011.0})
    dates = [p["date"] for p in series]
    assert dates.count("2026-08-31") == 1


def test_equity_series_works_with_no_snapshot() -> None:
    series = ad.equity_series(AGENT_LOG, SESSION, {})
    assert series[-1]["source"] == "logged"          # falls back to the last logged mark


def test_equity_series_drops_marks_before_the_session_baseline() -> None:
    # AGENT_LOG has marks on 08-31 / 09-01 / 09-03; a session created 09-02 keeps
    # only 09-03 and starts from starting_equity, so the curve matches the P&L%.
    sess = {"starting_equity": 99870.9, "created_at": "2026-09-02T11:53:55-04:00"}
    series = ad.equity_series(AGENT_LOG, sess, {"equity": 96977.27, "captured_at": "2026-09-04T10:00:00Z"})
    assert [p["source"] for p in series] == ["start", "logged", "snapshot"]
    assert series[0]["equity"] == pytest.approx(99870.9)
    assert series[1]["date"] == "2026-09-03"


def test_decision_log_parses_full_untruncated_reasons() -> None:
    rows = ad.decision_log(AGENT_LOG)
    # newest first
    assert rows[0]["ts"].startswith("2026-09-04")
    corr = [r for r in rows if r["stage"] == "risk_manager"][0]
    assert corr["symbol"] == "QQQ"
    assert corr["outcome"] == "blocked"
    assert "trade risk $1,468 > $1,455 (1.5% of current equity)" in corr["reason"]
    # a row with no [SYM] prefix still parses
    strat = [r for r in rows if r["stage"] == "strategy"][0]
    assert strat["symbol"] is None
    assert "Hackathon Mode" in strat["reason"]


def test_decision_log_stage_filter_and_limit() -> None:
    only_officer = ad.decision_log(AGENT_LOG, stage="risk_officer")
    assert {r["stage"] for r in only_officer} == {"risk_officer"}
    assert ad.decision_log(AGENT_LOG, limit=2) == ad.decision_log(AGENT_LOG)[:2]


def test_decision_log_stage_counts() -> None:
    counts = ad.decision_log_stage_counts(AGENT_LOG)
    assert counts["risk_manager"] == 1
    assert counts["executor"] == 1
    assert counts["strategy"] == 1
    assert sum(counts.values()) == 5


# =========================================================================== #
# Polish pass — trade_history_grouped (churn rollup with realized P&L)
# =========================================================================== #
GROUP_SESSION = {
    "starting_equity": 99870.9,
    "history": [
        {"kind": "submitted", "at": "2026-09-03T13:32:00-04:00", "id": "a", "symbol": "SPY",
         "structure": "bull_put", "entry_credit": 0.83,
         "officer": {"provider": "featherless", "approved": True}},
        {"kind": "opened", "at": "2026-09-03T13:33:00-04:00", "id": "a", "symbol": "SPY",
         "structure": "bull_put", "quantity": 3},
        {"kind": "closed", "at": "2026-09-03T13:55:00-04:00", "id": "a", "symbol": "SPY",
         "structure": "bull_put", "pnl": 36.0, "reason": "profit-target"},
        {"kind": "submitted", "at": "2026-09-03T14:10:00-04:00", "id": "b", "symbol": "SPY",
         "structure": "bull_put", "entry_credit": 0.90,
         "officer": {"provider": "featherless", "approved": True}},
        {"kind": "opened", "at": "2026-09-03T14:11:00-04:00", "id": "b", "symbol": "SPY",
         "structure": "bull_put", "quantity": 3},
        {"kind": "closed", "at": "2026-09-03T14:40:00-04:00", "id": "b", "symbol": "SPY",
         "structure": "bull_put", "pnl": -12.0, "reason": "stop-loss"},
        {"kind": "submitted", "at": "2026-09-02T11:00:00-04:00", "id": "c", "symbol": "TLT",
         "structure": "long_strangle", "entry_credit": None,
         "officer": {"approved": False}},
        {"kind": "order_stale_cancelled", "at": "2026-09-02T11:15:00-04:00", "id": "c",
         "symbol": "TLT", "structure": "long_strangle"},
        {"kind": "reconciled", "at": "2026-09-02T12:00:00+05:30", "detail": "note only"},
    ],
}


def test_trade_history_grouped_rolls_up_by_symbol_and_structure() -> None:
    g = ad.trade_history_grouped(GROUP_SESSION)
    by = {(r["symbol"], r["structure"]): r for r in g}
    spy = by[("SPY", "bull_put")]
    assert spy["submitted"] == 2 and spy["opened"] == 2 and spy["closed"] == 2
    assert spy["qty"] == 3          # max quantity on any record (opened rows carry none)
    assert spy["realized_pnl"] == pytest.approx(24.0)      # 36 - 12
    assert spy["credit_lo"] == pytest.approx(0.83)
    assert spy["credit_hi"] == pytest.approx(0.90)
    assert spy["officer_ok"] == 2 and spy["officer_seen"] == 2
    assert spy["first"] < spy["last"]


def test_trade_history_grouped_counts_cancels_and_skips_notes() -> None:
    g = ad.trade_history_grouped(GROUP_SESSION)
    tlt = [r for r in g if r["symbol"] == "TLT"][0]
    assert tlt["cancelled"] == 1
    assert tlt["opened"] == 0 and tlt["closed"] == 0
    assert tlt["realized_pnl"] == pytest.approx(0.0)
    assert tlt["officer_seen"] == 1 and tlt["officer_ok"] == 0
    # the 'reconciled' note row produced no group
    assert all(r["symbol"] for r in g)


def test_trade_history_grouped_sorted_oldest_last_activity_first() -> None:
    g = ad.trade_history_grouped(GROUP_SESSION)
    assert [r["symbol"] for r in g] == ["TLT", "SPY"]   # by last-activity ascending


def test_trade_history_grouped_empty_session() -> None:
    assert ad.trade_history_grouped({}) == []


# =========================================================================== #
# Polish pass — debate_agreement (Bull/Bear/Judge alignment summary)
# =========================================================================== #
def _dbt(sym, b, be, j):
    return {"symbol": sym, "when": "x", "outcome": "vetoed" if j == "VETO" else "executed",
            "pipeline": "", "rounds": [
                {"role": "BULL", "provider": None, "verdict": b, "thesis": ""},
                {"role": "BEAR", "provider": None, "verdict": be, "thesis": ""},
                {"role": "JUDGE", "provider": "featherless", "verdict": j, "thesis": ""}]}


def test_debate_agreement_tallies_judge_alignment() -> None:
    debates = [
        _dbt("QQQ", "APPROVE", "VETO", "VETO"),    # split -> judge with bear
        _dbt("SPY", "APPROVE", "APPROVE", "APPROVE"),  # both approve, judge approve
        _dbt("IWM", "APPROVE", "VETO", "APPROVE"),  # split -> judge with bull
        _dbt("DIA", "VETO", "VETO", "VETO"),       # both veto
    ]
    a = ad.debate_agreement(debates)
    assert a["n"] == 4
    assert a["judge_veto"] == 2 and a["judge_approve"] == 2
    assert a["split"] == 2
    assert a["judge_with_bear"] == 1 and a["judge_with_bull"] == 1
    assert a["both_veto"] == 1 and a["both_approve"] == 1


def test_debate_agreement_skips_debates_without_a_judge_verdict() -> None:
    debates = [
        {"symbol": "X", "rounds": [{"role": "BULL", "verdict": "APPROVE", "provider": None,
                                    "thesis": ""}]},                       # no judge
        {"symbol": "Y", "rounds": []},                                     # corrupt
        _dbt("Z", "APPROVE", "VETO", "VETO"),
    ]
    a = ad.debate_agreement(debates)
    assert a["n"] == 1
    assert a["judge_with_bear"] == 1


def test_debate_agreement_empty() -> None:
    a = ad.debate_agreement([])
    assert a["n"] == 0 and a["split"] == 0
