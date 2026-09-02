"""Offline tests for context_gatherer.py — the Contextual Intelligence /
Macro-Filter layer.

Every external pull (macro calendar, VIX proxy, news, price history) is either a
pure function or injected here; no network. The contract under test:

  * Wilder 14-day RSI + overbought/oversold classification
  * high-impact macro calendar: "inside the next 48h" and "today" lookups
  * headline sentiment score (keyword heuristic)
  * MarketContext.synthesis() one-line format
  * gather_context(): partial failures degrade to that part's "unavailable"
    marker; a total failure yields "No Context Available"
  * prioritize(): best IV-RV spread first, news score as the tiebreak
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading_agent import context_gatherer as cg
from trading_agent.context_gatherer import (
    MacroEvent,
    MarketContext,
    TickerContext,
    classify_rsi,
    gather_context,
    high_impact_today,
    prioritize,
    score_headlines,
    upcoming_high_impact,
    wilder_rsi,
)

UTC = timezone.utc


# ======================================================================= #
# Wilder RSI
# ======================================================================= #
def test_rsi_is_100_for_a_monotonic_rally() -> None:
    closes = [100 + i for i in range(20)]
    assert wilder_rsi(closes) == 100.0


def test_rsi_is_0_for_a_monotonic_selloff() -> None:
    closes = [100 - i for i in range(20)]
    assert wilder_rsi(closes) == 0.0


def test_rsi_is_50_for_a_flat_series() -> None:
    assert wilder_rsi([50.0] * 20) == 50.0


def test_rsi_none_when_not_enough_history() -> None:
    assert wilder_rsi([1, 2, 3]) is None
    assert wilder_rsi([]) is None


def test_rsi_mixed_series_is_between_0_and_100() -> None:
    closes = [44, 44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
              45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03,
              46.41, 46.22, 45.64]
    r = wilder_rsi(closes)
    # classic Wilder worked series; the last three closes tick down, pulling it
    # off the ~70 mid-series reading to the high-50s
    assert r is not None and 55.0 < r < 65.0


# ======================================================================= #
# Wilder ADX
# ======================================================================= #
def test_adx_flags_a_strong_uptrend() -> None:
    highs = [100.0 + i for i in range(40)]
    lows = [99.0 + i for i in range(40)]
    closes = [99.5 + i for i in range(40)]
    adx, direction = cg.wilder_adx(highs, lows, closes)
    assert adx is not None and adx >= cg.ADX_TREND
    assert direction == "up"


def test_adx_flags_a_strong_downtrend() -> None:
    highs = [140.0 - i for i in range(40)]
    lows = [139.0 - i for i in range(40)]
    closes = [139.5 - i for i in range(40)]
    adx, direction = cg.wilder_adx(highs, lows, closes)
    assert adx is not None and adx >= cg.ADX_TREND
    assert direction == "down"


def test_adx_is_low_in_a_choppy_range() -> None:
    highs, lows, closes = [], [], []
    for i in range(60):
        mid = 100.0 + (0.5 if i % 2 else -0.5)
        highs.append(mid + 0.5)
        lows.append(mid - 0.5)
        closes.append(mid)
    adx, _ = cg.wilder_adx(highs, lows, closes)
    assert adx is not None and adx < cg.ADX_RANGE


def test_adx_none_when_not_enough_history() -> None:
    assert cg.wilder_adx([1, 2, 3], [1, 2, 3], [1, 2, 3]) == (None, None)
    assert cg.wilder_adx([], [], []) == (None, None)


def test_classify_adx_bands() -> None:
    assert cg.classify_adx(30.0) == "trend"
    assert cg.classify_adx(25.0) == "trend"
    assert cg.classify_adx(19.9) == "range"
    assert cg.classify_adx(22.0) == "mixed"
    assert cg.classify_adx(None) == "n/a"


# ======================================================================= #
# Basket correlation clusters
# ======================================================================= #
def test_correlation_clusters_groups_names_that_move_together() -> None:
    spy = [100 + i for i in range(15)]
    qqq = [200 + 2 * i for i in range(15)]          # affine fn of spy -> identical returns
    tlt = [50 + ((-1) ** i) for i in range(15)]     # zig-zag, uncorrelated
    clusters = cg.correlation_clusters({"SPY": spy, "QQQ": qqq, "TLT": tlt})
    assert clusters == (frozenset({"QQQ", "SPY"}),)


def test_correlation_clusters_empty_when_nothing_correlates() -> None:
    a = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107]
    b = [100, 98, 99, 97, 98, 96, 97, 95, 96, 94, 95, 93]   # mirror -> negative corr
    assert cg.correlation_clusters({"A": a, "B": b}) == ()


def test_correlation_clusters_skips_series_with_too_little_history() -> None:
    assert cg.correlation_clusters({"A": [1, 2, 3], "B": [1, 2, 3]}) == ()
    assert cg.correlation_clusters({}) == ()


def test_classify_rsi_bands() -> None:
    assert classify_rsi(72.0) == "overbought"
    assert classify_rsi(70.0) == "overbought"
    assert classify_rsi(30.0) == "oversold"
    assert classify_rsi(21.3) == "oversold"
    assert classify_rsi(50.0) == "neutral"
    assert classify_rsi(None) == "n/a"


# ======================================================================= #
# Headline sentiment
# ======================================================================= #
def test_score_headlines_positive_and_negative() -> None:
    assert score_headlines(["Stocks surge as earnings beat", "Analysts upgrade the name"]) >= 2
    assert score_headlines(["Market selloff deepens", "Company warns on guidance, shares plunge"]) <= -2


def test_score_headlines_neutral_or_empty() -> None:
    assert score_headlines([]) == 0
    assert score_headlines(["Company to host investor day in October"]) == 0


# ======================================================================= #
# Macro calendar
# ======================================================================= #
_CAL = (
    MacroEvent(date(2026, 9, 4), "Employment Situation (NFP)"),
    MacroEvent(date(2026, 9, 11), "CPI release"),
    MacroEvent(date(2026, 9, 16), "FOMC rate decision"),
)


def test_upcoming_high_impact_respects_the_48h_window() -> None:
    now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)          # NFP is ~26h away
    ev = upcoming_high_impact(now, calendar=_CAL)
    assert [e.name for e in ev] == ["Employment Situation (NFP)"]

    now2 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)          # nothing within 48h
    assert upcoming_high_impact(now2, calendar=_CAL) == []


def test_high_impact_today_flag() -> None:
    assert high_impact_today(datetime(2026, 9, 16, 12, 0, tzinfo=UTC), calendar=_CAL) is True
    assert high_impact_today(datetime(2026, 9, 15, 12, 0, tzinfo=UTC), calendar=_CAL) is False


def test_shipped_calendar_has_high_impact_events() -> None:
    # sanity: the bundled static calendar is populated and typed
    assert len(cg.HIGH_IMPACT_CALENDAR) >= 12
    assert all(isinstance(e, MacroEvent) and e.impact == "High" for e in cg.HIGH_IMPACT_CALENDAR)


# ======================================================================= #
# MarketContext.synthesis
# ======================================================================= #
def _ctx(**kw) -> MarketContext:
    base = dict(
        as_of="2026-09-01T10:00:00+00:00",
        macro_events=(MacroEvent(date(2026, 9, 16), "FOMC rate decision"),),
        macro_today_high_impact=False,
        vix_proxy=18.2,
        vix_change_5d_pct=12.5,
        vix_note="elevated / possibly spiking",
        tickers=(
            TickerContext("SPY", 41.0, "neutral", ("Bond rout sparks selloff",), -1),
            TickerContext("QQQ", 72.0, "overbought", ("Nasdaq jumps on chip rally",), 1),
        ),
        ok=True,
        errors=(),
    )
    base.update(kw)
    return MarketContext(**base)


def test_synthesis_has_all_four_sections() -> None:
    s = _ctx().synthesis()
    assert "Macro:" in s and "FOMC rate decision" in s
    assert "VIX" in s and "18.2" in s
    assert "News SPY:" in s and "Bond rout" in s
    assert "RSI SPY:" in s and "41" in s
    assert "RSI QQQ:" in s and "overbought" in s
    assert "\n" not in s          # single line


def test_synthesis_reports_no_events_cleanly() -> None:
    s = _ctx(macro_events=()).synthesis()
    assert "macro: none" in s.lower()


def test_next_macro_event_date_returns_the_earliest_upcoming_event() -> None:
    ctx = _ctx(macro_events=(
        MacroEvent(date(2026, 9, 16), "FOMC rate decision"),
        MacroEvent(date(2026, 9, 4), "Employment Situation (NFP)"),
        MacroEvent(date(2026, 9, 11), "CPI release"),
    ))
    assert ctx.next_macro_event_date() == date(2026, 9, 4)


def test_next_macro_event_date_is_none_without_events() -> None:
    assert _ctx(macro_events=()).next_macro_event_date() is None


def test_unavailable_context_synthesises_to_no_context_available() -> None:
    mc = MarketContext.unavailable("everything is down")
    assert mc.ok is False
    assert mc.synthesis() == "No Context Available"
    assert mc.macro_today_high_impact is False        # fail-safe: never triggers the macro guard


# ======================================================================= #
# gather_context — orchestration + fail-safe degradation
# ======================================================================= #
def _closes_fn_ok(sym):
    return {"SPY": [100 + i for i in range(20)], "QQQ": [200 - i for i in range(20)]}[sym]


def test_gather_context_happy_path_with_injected_fetchers() -> None:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    mc = gather_context(
        creds=None, symbols=["SPY", "QQQ"], now=now, calendar=_CAL,
        headlines_fn=lambda: {"SPY": ["Stocks surge, earnings beat"], "QQQ": ["Shares plunge on downgrade"]},
        vix_fn=lambda: (17.4, 3.0, "calm"),
        closes_fn=lambda: {s: _closes_fn_ok(s) for s in ("SPY", "QQQ")},
    )
    assert mc.ok is True
    assert mc.vix_proxy == 17.4
    spy = mc.ticker("SPY")
    assert spy.rsi == 100.0 and spy.rsi_state == "overbought" and spy.news_score >= 2
    qqq = mc.ticker("QQQ")
    assert qqq.rsi == 0.0 and qqq.news_score <= -1
    # FOMC on the 16th is within 48h of the 15th
    assert [e.name for e in mc.macro_events] == ["FOMC rate decision"]


def test_gather_context_news_failure_degrades_only_news() -> None:
    def boom():
        raise RuntimeError("news api 503")
    mc = gather_context(
        creds=None, symbols=["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=_CAL,
        headlines_fn=boom,
        vix_fn=lambda: (17.4, 1.0, "calm"),
        closes_fn=lambda: {"SPY": [100 + i for i in range(20)]},
    )
    assert mc.ok is True                       # partial context is still context
    assert mc.ticker("SPY").headlines == ()
    assert mc.vix_proxy == 17.4
    assert any("news" in e for e in mc.errors)
    assert "unavailable" in mc.synthesis().lower()


def test_gather_context_total_failure_is_no_context_available() -> None:
    def boom():
        raise RuntimeError("down")
    mc = gather_context(
        creds=None, symbols=["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=(),
        headlines_fn=boom, vix_fn=boom, closes_fn=boom,
    )
    assert mc.ok is False
    assert mc.synthesis() == "No Context Available"


def test_gather_context_never_raises_on_bad_creds(monkeypatch) -> None:
    # no injected fns -> real fetchers run with creds=None; must swallow and degrade
    mc = gather_context(creds=None, symbols=["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC))
    assert isinstance(mc, MarketContext)
    assert mc.synthesis()          # some string, no exception


# ======================================================================= #
# prioritize — best IV-RV spread first, news score breaks ties
# ======================================================================= #
def test_prioritize_orders_by_iv_rv_spread_then_news() -> None:
    snaps = {
        "SPY": {"iv_rv_spread": 0.03},
        "QQQ": {"iv_rv_spread": 0.06},
        "IWM": {"iv_rv_spread": 0.06},
        "TLT": {"iv_rv_spread": None},
    }
    ctx = _ctx(tickers=(
        TickerContext("SPY", 50, "neutral", (), 0),
        TickerContext("QQQ", 50, "neutral", (), -2),
        TickerContext("IWM", 50, "neutral", (), 3),
        TickerContext("TLT", 50, "neutral", (), 0),
    ))
    assert prioritize(["SPY", "QQQ", "IWM", "TLT"], snaps, ctx) == ["IWM", "QQQ", "SPY", "TLT"]
