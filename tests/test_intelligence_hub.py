"""Offline tests for intelligence_hub.py — the Quantamental context layer.

yfinance is the primary source; the Alpaca-based context_gatherer fetchers are
the fallback; "No Context Available" is the last resort. Every fetch is injected
here — no network, no real yfinance call.

Contract under test:
  * VIX term structure -> ratio -> PANIC_REGIME on backwardation (VIX > VXV)
  * MACRO_DANGER when a High-Impact event is inside the next 48h
  * yfinance failure on any pipe -> that pipe falls back to Alpaca, then empties
  * total failure -> MarketContext.unavailable() (synthesis == "No Context Available")
  * synthesis string surfaces the regime flags
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from trading_agent import intelligence_hub as ih
from trading_agent.context_gatherer import MacroEvent, MarketContext

UTC = timezone.utc
_CAL = (
    MacroEvent(date(2026, 9, 16), "FOMC rate decision"),
    MacroEvent(date(2026, 9, 4), "Employment Situation (NFP)"),
)


def _yf_ok(**over):
    base = dict(
        vix_fn=lambda: (16.0, 18.0, round(16.0 / 18.0, 3)),          # contango
        news_fn=lambda syms: {s: [f"{s} headline one", f"{s} rallies, earnings beat"] for s in syms},
        closes_fn=lambda syms: {s: [100 + i for i in range(20)] for s in syms},
    )
    base.update(over)
    return base


# ======================================================================= #
# VIX term structure -> PANIC_REGIME
# ======================================================================= #
def test_contango_is_not_a_panic_regime() -> None:
    mc = ih.gather(None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=_CAL, **_yf_ok())
    assert mc.vix == 16.0 and mc.vxv == 18.0
    assert mc.vix_vxv_ratio < 1.0
    assert mc.panic_regime is False
    assert "PANIC_REGIME" not in mc.regime_flags()


def test_backwardation_signals_panic_regime() -> None:
    mc = ih.gather(None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=_CAL,
                   **_yf_ok(vix_fn=lambda: (28.0, 24.0, round(28.0 / 24.0, 3))))
    assert mc.panic_regime is True
    assert "PANIC_REGIME" in mc.regime_flags()
    assert "BACKWARDATION" in mc.synthesis()


# ======================================================================= #
# MACRO_DANGER
# ======================================================================= #
def test_macro_danger_when_event_within_48h() -> None:
    # NFP on the 4th, ~14h out
    mc = ih.gather(None, ["SPY"], now=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
                   calendar=_CAL, **_yf_ok())
    assert mc.macro_danger is True
    assert "MACRO_DANGER" in mc.regime_flags()
    assert "MACRO_DANGER" in mc.synthesis()


def test_no_macro_danger_when_calendar_is_clear() -> None:
    mc = ih.gather(None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=_CAL, **_yf_ok())
    assert mc.macro_danger is False


# ======================================================================= #
# RSI + news land on the tickers
# ======================================================================= #
def test_ticker_rsi_and_news_come_through() -> None:
    mc = ih.gather(None, ["SPY", "QQQ"], now=datetime(2026, 9, 1, tzinfo=UTC),
                   calendar=_CAL, **_yf_ok())
    spy = mc.ticker("SPY")
    assert spy.rsi == 100.0               # monotonic rally series
    assert spy.headlines and spy.news_score >= 1
    assert mc.ok is True


# ======================================================================= #
# Symmetry / fallback
# ======================================================================= #
def test_vix_yf_failure_falls_back_to_alpaca_proxy() -> None:
    def boom():
        raise RuntimeError("yahoo 999")

    mc = ih.gather(
        None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=_CAL,
        vix_fn=boom, news_fn=lambda syms: {"SPY": ["x"]},
        closes_fn=lambda syms: {"SPY": [1.0] * 20},
        alpaca_vix_fn=lambda: (17.5, 2.0, "calm"),          # context_gatherer proxy
    )
    assert mc.ok is True
    assert mc.vix is None and mc.vxv is None                # no term structure
    assert mc.vix_proxy == 17.5                             # Alpaca fallback used
    assert mc.panic_regime is False                         # can't call panic w/o term structure
    assert any("vix" in e for e in mc.errors)


def test_news_yf_failure_falls_back_to_alpaca_news() -> None:
    def boom(_syms):
        raise RuntimeError("yahoo news down")

    mc = ih.gather(
        None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=_CAL,
        vix_fn=lambda: (16.0, 18.0, 0.889),
        news_fn=boom, closes_fn=lambda syms: {"SPY": [1.0] * 20},
        alpaca_news_fn=lambda syms: {"SPY": ["Alpaca headline: shares slump"]},
    )
    assert mc.ticker("SPY").headlines == ("Alpaca headline: shares slump",)
    assert mc.ticker("SPY").news_score <= -1


def test_total_failure_is_no_context_available() -> None:
    def boom(*a, **k):
        raise RuntimeError("everything down")

    mc = ih.gather(
        None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC), calendar=(),
        vix_fn=boom, news_fn=boom, closes_fn=boom,
        alpaca_vix_fn=boom, alpaca_news_fn=boom, alpaca_closes_fn=boom,
    )
    assert mc.ok is False
    assert mc.synthesis() == "No Context Available"
    assert mc.panic_regime is False and mc.macro_danger is False


def test_gather_never_raises_with_no_injections() -> None:
    mc = ih.gather(None, ["SPY"], now=datetime(2026, 9, 1, tzinfo=UTC))
    assert isinstance(mc, MarketContext)
    assert mc.synthesis()          # a string, no exception


# ======================================================================= #
# yfinance response-shape parsing (pure helpers)
# ======================================================================= #
def test_parse_yf_news_handles_new_and_old_shapes() -> None:
    items = [
        {"id": "1", "content": {"title": "New-shape headline"}},
        {"title": "Old-shape headline"},
        {"id": "3", "content": {}},                      # no title -> skipped
    ]
    assert ih._headlines_from_yf_news(items, per=5) == ["New-shape headline", "Old-shape headline"]
