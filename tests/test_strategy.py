"""Offline tests for strategy.py iron-condor construction (no network)."""

from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import strategy as s
from trading_agent.alpaca_trader import OptionContract
from trading_agent.data import IVRegime

EXPIRY = date(2026, 9, 8)          # a Tuesday; 1 trading day after Fri 2026-09-04
TODAY = date(2026, 9, 4)          # so nth_trading_day(1..3, TODAY) == 09-08 .. 09-10
ELIGIBLE = IVRegime(0.20, None, "hackathon_static", True, "test-eligible")
BLOCKED = IVRegime(0.10, None, "hackathon_static", False, "test-blocked")
# Regime-C-style (IV not "elevated") but ATM IV in the normal band, so the short
# leg still targets the fixed 0.225 delta rather than the low-IV 0.10 scaling.
BLOCKED_MIDVOL = IVRegime(0.22, None, "hackathon_static", False, "test-blocked-midvol")


def mk(right, strike, abs_delta, bid, ask, expiry=EXPIRY):
    mid = (bid + ask) / 2
    return OptionContract(
        symbol=f"SPY-{right}-{int(strike)}",
        underlying="SPY",
        expiry=expiry,
        right=right,
        strike=float(strike),
        bid=bid,
        ask=ask,
        bid_size=25,
        ask_size=25,
        mid=mid,
        spread=ask - bid,
        spread_pct=(ask - bid) / mid * 100 if mid > 0 else math.nan,
        delta=(-abs_delta if right == "put" else abs_delta),
        abs_delta=abs_delta,
        implied_volatility=0.20,
    )


def sample_chain():
    """A small symmetric SPY chain around ~770 with a clean 0.225/0.10 structure."""
    return [
        # puts (delta magnitude grows as strike falls)
        mk("put", 765, 0.30, 3.40, 3.50),
        mk("put", 760, 0.225, 2.35, 2.45),   # -> short put  (mid 2.40)
        mk("put", 755, 0.16, 1.35, 1.45),
        mk("put", 750, 0.10, 0.90, 1.00),    # -> long put   (mid 0.95, delta rule)
        mk("put", 745, 0.06, 0.55, 0.65),
        # calls
        mk("call", 775, 0.30, 3.35, 3.45),
        mk("call", 780, 0.225, 2.30, 2.40),  # -> short call (mid 2.35)
        mk("call", 785, 0.16, 1.30, 1.40),
        mk("call", 790, 0.10, 0.85, 0.95),   # -> long call  (mid 0.90, delta rule)
        mk("call", 795, 0.06, 0.50, 0.60),
    ]


# --------------------------------------------------------------------------- #
# leg selection
# --------------------------------------------------------------------------- #
def test_select_short_leg_prefers_delta_band_target() -> None:
    puts = [c for c in sample_chain() if c.right == "put"]
    assert s.select_short_leg(puts).strike == 760.0


def test_select_long_leg_delta_rule() -> None:
    puts = [c for c in sample_chain() if c.right == "put"]
    short_put = s.select_short_leg(puts)
    leg, rule = s.select_long_leg(puts, short_put, "put")
    assert rule == "delta"
    assert leg.strike == 750.0


def test_select_long_leg_falls_back_to_otm_offset() -> None:
    # no strike near 0.10 delta below the short -> use short strike - $5
    puts = [
        mk("put", 760, 0.225, 2.05, 2.15),
        mk("put", 755, 0.18, 1.40, 1.50),
        mk("put", 752, 0.16, 1.20, 1.30),
    ]
    short_put = s.select_short_leg(puts)
    leg, rule = s.select_long_leg(puts, short_put, "put")
    assert rule == "otm-offset"
    assert leg.strike == 755.0  # closest to 760 - 5


def test_select_long_leg_none_when_nothing_further_otm() -> None:
    puts = [mk("put", 760, 0.225, 2.0, 2.1)]
    leg, rule = s.select_long_leg(puts, puts[0], "put")
    assert leg is None and rule == "none-further-otm"


# --------------------------------------------------------------------------- #
# plan_iron_condor
# --------------------------------------------------------------------------- #
def test_plan_blocked_by_iv_gate() -> None:
    plan = s.plan_iron_condor(
        sample_chain(), underlying_price=770.0, iv_regime=BLOCKED, today=TODAY
    )
    assert plan.eligible is False
    assert "IV gate blocked" in plan.reason
    assert plan.legs == []


def test_plan_builds_full_condor_when_criteria_met() -> None:
    plan = s.plan_iron_condor(
        sample_chain(), underlying_price=770.0, iv_regime=ELIGIBLE, today=TODAY
    )
    assert plan.eligible is True
    assert plan.expiry == EXPIRY
    assert [(l.action, l.right, l.contract.strike) for l in plan.legs] == [
        ("sell", "put", 760.0),
        ("buy", "put", 750.0),
        ("sell", "call", 780.0),
        ("buy", "call", 790.0),
    ]
    # credit at mid: (2.40 + 2.35) - (0.95 + 0.90) = 2.90 over a 10-wide wing
    assert plan.wing_width == 10.0
    assert plan.net_credit == pytest.approx(2.90)
    assert plan.credit_to_width == pytest.approx(0.29)


def test_plan_rejects_thin_credit() -> None:
    # widen wings to 20 so credit/width falls well under the MIN_CREDIT_TO_WIDTH gate
    chain = [
        mk("put", 760, 0.225, 2.05, 2.15),
        mk("put", 740, 0.10, 0.40, 0.50),
        mk("call", 780, 0.225, 2.00, 2.10),
        mk("call", 800, 0.10, 0.35, 0.45),
    ]
    plan = s.plan_iron_condor(
        chain, underlying_price=770.0, iv_regime=ELIGIBLE, today=TODAY
    )
    assert plan.eligible is False
    assert f"below {s.MIN_CREDIT_TO_WIDTH:.0%} target" in plan.reason
    assert len(plan.legs) == 4  # legs still reported for inspection


def test_plan_blocked_when_iv_not_richer_than_realized() -> None:
    plan = s.plan_iron_condor(
        sample_chain(),
        underlying_price=770.0,
        iv_regime=ELIGIBLE,
        iv_rv_spread=0.005,  # below MIN_IV_RV_SPREAD (0.02)
        today=TODAY,
    )
    assert plan.eligible is False
    assert "IV-RV spread" in plan.reason
    assert plan.iv_rv_spread == 0.005
    assert plan.legs == []


def test_plan_allows_when_iv_rv_spread_is_healthy() -> None:
    plan = s.plan_iron_condor(
        sample_chain(),
        underlying_price=770.0,
        iv_regime=ELIGIBLE,
        iv_rv_spread=0.08,
        today=TODAY,
    )
    assert plan.eligible is True
    assert plan.iv_rv_spread == 0.08


def test_plan_skips_iv_rv_check_when_spread_unknown() -> None:
    plan = s.plan_iron_condor(
        sample_chain(),
        underlying_price=770.0,
        iv_regime=ELIGIBLE,
        iv_rv_spread=None,
        today=TODAY,
    )
    assert plan.eligible is True
    assert plan.iv_rv_spread is None


def test_plan_no_expiry_in_window() -> None:
    chain = [mk("put", 760, 0.225, 2.0, 2.1, expiry=date(2026, 10, 16))]
    plan = s.plan_iron_condor(
        chain, underlying_price=770.0, iv_regime=ELIGIBLE, today=TODAY
    )
    assert plan.eligible is False
    assert "no listed expiry" in plan.reason


def test_plan_position_sizing_respects_risk_cap() -> None:
    plan = s.plan_iron_condor(
        sample_chain(), underlying_price=770.0, iv_regime=ELIGIBLE, today=TODAY
    )
    # max loss/contract = (10 - 2.90) * 100 = 710 -> floor(1500/710) = 2
    assert plan.max_loss_per_contract == pytest.approx(710.0)
    assert plan.suggested_contracts == 2
    # sizing stays within the shared 1.5% cap and can't fit one more
    n = plan.suggested_contracts
    assert n * plan.max_loss_per_contract <= s.MAX_RISK_PER_TRADE
    assert (n + 1) * plan.max_loss_per_contract > s.MAX_RISK_PER_TRADE
    assert s.MAX_RISK_PER_TRADE == 1_500.0


# =========================================================================== #
# Range-bound filter — Kaufman efficiency ratio
# =========================================================================== #
UP = [100 + i for i in range(11)]                       # straight line up
DOWN = [110 - i for i in range(11)]                     # straight line down
CHOP = [100, 103, 100, 103, 100, 103, 100, 103, 100, 103, 100]   # net 0, ER 0
WOBBLE = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 101]  # net +1, ER ~0.09


def test_efficiency_ratio_straight_line_is_one() -> None:
    assert s.efficiency_ratio(UP) == 1.0
    assert s.efficiency_ratio(DOWN) == 1.0


def test_efficiency_ratio_choppy_is_near_zero() -> None:
    assert s.efficiency_ratio(CHOP) == 0.0


def test_efficiency_ratio_none_without_enough_history() -> None:
    assert s.efficiency_ratio([100, 101, 102]) is None
    assert s.efficiency_ratio(None) is None


def test_efficiency_ratio_flat_series_is_zero() -> None:
    assert s.efficiency_ratio([50.0] * 11) == 0.0


def test_is_range_bound_matches_the_threshold_rule() -> None:
    assert s.is_range_bound(CHOP, threshold=0.3) is True        # ER 0.0 < 0.3
    assert s.is_range_bound(UP, threshold=0.3) is False         # ER 1.0 >= 0.3
    assert s.is_range_bound(WOBBLE, threshold=0.3) is True      # tiny net move, low ER
    assert s.is_range_bound([1, 2, 3]) is None                  # not enough data


def test_trend_direction() -> None:
    assert s.trend_direction(UP) == "up"
    assert s.trend_direction(DOWN) == "down"
    assert s.trend_direction([50.0] * 11) == "flat"
    assert s.trend_direction([1, 2]) is None


# =========================================================================== #
# Dynamic market-regime switch
# =========================================================================== #
def _snap(*, atm_iv, spread, iv_eligible, closes):
    return {
        "symbol": "SPY",
        "atm_iv": atm_iv,
        "iv_rv_spread": spread,
        "iv_regime": IVRegime(atm_iv, None, "hackathon_static", iv_eligible, "test"),
        "daily_closes": closes,
        "current_price": 100.0,
        "chain": {},
    }


def test_regime_a_high_vol_picks_iron_condor() -> None:
    d = s.select_regime(_snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP))
    assert d.regime == s.REGIME_IRON_CONDOR
    assert "Regime A" in d.label


def test_regime_b_low_vol_range_bound_picks_long_strangle() -> None:
    d = s.select_regime(_snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=CHOP))
    assert d.regime == s.REGIME_LONG_STRANGLE
    assert "Regime B" in d.label and "range-bound" in d.reason


def test_regime_c_low_vol_trending_up_picks_bull_put() -> None:
    d = s.select_regime(_snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=UP))
    assert d.regime == s.REGIME_BULL_PUT
    assert d.direction == "up" and "Regime C" in d.label


def test_regime_c_low_vol_trending_down_picks_bear_call() -> None:
    d = s.select_regime(_snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=DOWN))
    assert d.regime == s.REGIME_BEAR_CALL
    assert d.direction == "down"


def test_regime_none_when_vol_is_neutral() -> None:
    d = s.select_regime(_snap(atm_iv=0.14, spread=0.00, iv_eligible=False, closes=UP))
    assert d.regime == s.REGIME_NONE


def test_regime_none_when_iv_cheap_but_no_price_history() -> None:
    d = s.select_regime(_snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=[1, 2, 3]))
    assert d.regime == s.REGIME_NONE


def test_regime_a_needs_iv_elevated_not_just_rich_spread() -> None:
    # spread rich but IV gate not eligible -> not Regime A; spread positive -> not B/C
    d = s.select_regime(_snap(atm_iv=0.10, spread=0.03, iv_eligible=False, closes=CHOP))
    assert d.regime == s.REGIME_NONE


# =========================================================================== #
# Long strangle (Regime B) — net debit, sized within the 1.5% cap
# =========================================================================== #
def test_plan_long_strangle_builds_two_long_legs() -> None:
    plan = s.plan_long_strangle(
        sample_chain(), underlying_price=770.0, iv_regime=BLOCKED, today=TODAY
    )
    assert plan.eligible is True
    assert plan.structure == "long_strangle"
    assert [(lg.action, lg.right) for lg in plan.legs] == [("buy", "put"), ("buy", "call")]
    # ~0.30-delta strikes are nearest 0.25 in the sample chain (765P / 775C)
    debit = plan.legs[0].contract.mid + plan.legs[1].contract.mid
    assert plan.net_credit == pytest.approx(-debit)          # negative = debit paid
    assert plan.max_loss_per_contract == pytest.approx(debit * 100)
    assert plan.suggested_contracts >= 1
    assert plan.suggested_contracts * plan.max_loss_per_contract <= s.MAX_RISK_PER_TRADE


def test_plan_long_strangle_blocks_when_debit_exceeds_cap() -> None:
    pricey = [
        mk("put", 760, 0.25, 9.0, 9.2),
        mk("call", 780, 0.25, 9.0, 9.2),
    ]
    plan = s.plan_long_strangle(pricey, underlying_price=770.0, iv_regime=BLOCKED, today=TODAY)
    assert plan.eligible is False
    assert "risk cap" in plan.reason and plan.suggested_contracts == 0


# =========================================================================== #
# Vertical credit spreads (Regime C)
# =========================================================================== #
def _rich_put_ladder():
    # short 760 fat, long 755 ~0.10 -> credit 2.70 / width 5 = 54% (clears 25%)
    return [
        mk("put", 765, 0.30, 4.20, 4.40),
        mk("put", 760, 0.225, 3.30, 3.50),
        mk("put", 755, 0.10, 0.60, 0.80),
        mk("put", 750, 0.06, 0.30, 0.40),
    ]


def _rich_call_ladder():
    return [
        mk("call", 775, 0.30, 4.20, 4.40),
        mk("call", 780, 0.225, 3.30, 3.50),
        mk("call", 785, 0.10, 0.60, 0.80),
        mk("call", 790, 0.06, 0.30, 0.40),
    ]


def test_plan_bull_put_is_a_put_credit_spread() -> None:
    plan = s.plan_bull_put(
        _rich_put_ladder(), underlying_price=770.0, iv_regime=BLOCKED_MIDVOL, today=TODAY
    )
    assert plan.eligible is True and plan.structure == "bull_put"
    assert [(lg.action, lg.right) for lg in plan.legs] == [("sell", "put"), ("buy", "put")]
    assert plan.net_credit > 0                              # credit received
    # matched legs -> defined risk with the unchanged (width - credit) formula
    assert plan.max_loss_per_contract == pytest.approx(
        (plan.wing_width - plan.net_credit) * 100
    )
    assert plan.suggested_contracts >= 1


def test_plan_bear_call_is_a_call_credit_spread() -> None:
    plan = s.plan_bear_call(
        _rich_call_ladder(), underlying_price=770.0, iv_regime=BLOCKED_MIDVOL, today=TODAY
    )
    assert plan.eligible is True and plan.structure == "bear_call"
    assert [(lg.action, lg.right) for lg in plan.legs] == [("sell", "call"), ("buy", "call")]


def test_credit_spread_still_respects_the_credit_to_width_gate() -> None:
    # thin credit relative to a wide wing -> blocked, same MIN_CREDIT_TO_WIDTH rule as the condor
    plan = s.plan_bull_put(
        sample_chain(), underlying_price=770.0, iv_regime=BLOCKED, today=TODAY
    )
    assert plan.eligible is False
    assert f"below {s.MIN_CREDIT_TO_WIDTH:.0%} target" in plan.reason


# =========================================================================== #
# build_strategy_plan — regime dispatch + explicit logging
# =========================================================================== #
def test_build_strategy_plan_dispatches_and_logs_regime(caplog) -> None:
    snap = _snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=CHOP)
    snap["chain"] = {c.symbol: None for c in sample_chain()}  # not used; we patch build_contracts

    import trading_agent.strategy as strat
    orig = strat.build_contracts
    strat.build_contracts = lambda _chain: sample_chain()
    try:
        with caplog.at_level("INFO", logger="strategy"):
            plan = s.build_strategy_plan(snap, today=TODAY)
    finally:
        strat.build_contracts = orig

    assert plan.structure == "long_strangle"
    assert plan.regime and "Regime B" in plan.regime
    assert "REGIME [SPY]" in caplog.text
    assert "Long Strangle" in caplog.text
    assert "STRATEGY [SPY]" in caplog.text


def test_build_strategy_plan_no_regime_returns_ineligible(caplog) -> None:
    snap = _snap(atm_iv=0.14, spread=0.0, iv_eligible=False, closes=UP)
    with caplog.at_level("INFO", logger="strategy"):
        plan = s.build_strategy_plan(snap, today=TODAY)
    assert plan.eligible is False and plan.structure == s.REGIME_NONE
    assert "no tradeable regime" in plan.reason


# =========================================================================== #
# Quant enhancement 1 — dynamic delta scaling
# =========================================================================== #
def test_dynamic_short_delta_scales_inversely_with_iv() -> None:
    # IV-relative delta: high IV -> push strikes FURTHER OTM (lower delta, more PoP);
    # low/crushed IV -> move CLOSER to ATM (higher delta) to keep a worthwhile credit.
    assert s.dynamic_short_delta(None) == s.SHORT_DELTA_TARGET       # no IV -> unchanged
    assert s.dynamic_short_delta(0.10) == pytest.approx(s.DYN_DELTA_LOW_IV)   # low IV  -> 0.25 (closer)
    assert s.dynamic_short_delta(0.40) == pytest.approx(s.DYN_DELTA_HIGH_IV)  # high IV -> 0.15 (further OTM)
    assert s.DYN_DELTA_HIGH_IV < s.SHORT_DELTA_TARGET < s.DYN_DELTA_LOW_IV
    mid = s.dynamic_short_delta((s.DYN_IV_LOW + s.DYN_IV_HIGH) / 2)
    assert s.DYN_DELTA_HIGH_IV < mid < s.DYN_DELTA_LOW_IV           # normal band sits between


def test_select_short_leg_accepts_a_dynamic_target() -> None:
    calls = [mk("call", 775, 0.10, 0.9, 1.0), mk("call", 772, 0.20, 1.9, 2.0),
             mk("call", 769, 0.30, 3.4, 3.5)]
    assert s.select_short_leg(calls, target=0.30).abs_delta == 0.30   # closer to ATM
    assert s.select_short_leg(calls, target=0.10).abs_delta == 0.10   # further OTM


# =========================================================================== #
# Quant enhancement 2 — relative-value ranking
# =========================================================================== #
def test_rank_basket_orders_by_spread_then_news() -> None:
    snaps = {"SPY": {"iv_rv_spread": 0.03}, "QQQ": {"iv_rv_spread": 0.06},
             "IWM": {"iv_rv_spread": 0.06}, "TLT": {"iv_rv_spread": None}}
    ctx = SimpleNamespace(ticker=lambda sym: {
        "SPY": SimpleNamespace(news_score=0), "QQQ": SimpleNamespace(news_score=-2),
        "IWM": SimpleNamespace(news_score=3), "TLT": SimpleNamespace(news_score=0),
    }[sym])
    assert s.rank_basket(["SPY", "QQQ", "IWM", "TLT"], snaps, ctx) == ["IWM", "QQQ", "SPY", "TLT"]


# =========================================================================== #
# Quant enhancement 3 — MACRO_DANGER / PANIC_REGIME force long volatility
# =========================================================================== #
def _ctx(*, danger=False, panic=False):
    flags = (["MACRO_DANGER"] if danger else []) + (["PANIC_REGIME"] if panic else [])
    return SimpleNamespace(macro_danger=danger, panic_regime=panic,
                           regime_flags=lambda: flags)


def test_panic_regime_overrides_iron_condor_to_long_strangle() -> None:
    d = s.select_regime(
        _snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP),
        context=_ctx(panic=True),
    )
    assert d.regime == s.REGIME_LONG_STRANGLE
    assert "OVERRIDE" in d.label and "PANIC_REGIME" in d.label


def test_macro_danger_overrides_a_credit_spread_to_long_strangle() -> None:
    d = s.select_regime(
        _snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=UP),   # base = bull_put
        context=_ctx(danger=True),
    )
    assert d.regime == s.REGIME_LONG_STRANGLE
    assert "MACRO_DANGER" in d.label


def test_context_without_flags_leaves_the_regime_untouched() -> None:
    base = s.select_regime(_snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP))
    withctx = s.select_regime(_snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP),
                              context=_ctx())
    assert withctx.regime == base.regime == s.REGIME_IRON_CONDOR


def test_panic_does_not_manufacture_a_trade_when_there_is_no_regime() -> None:
    d = s.select_regime(_snap(atm_iv=0.14, spread=0.0, iv_eligible=False, closes=UP),
                        context=_ctx(panic=True, danger=True))
    assert d.regime == s.REGIME_NONE


# =========================================================================== #
# Quant enhancement 4 — ADX trend-strength filter disables condors in a trend
# =========================================================================== #
def _adx_ctx(*, adx, direction=None):
    return SimpleNamespace(
        regime_flags=lambda: [],
        adx_for=lambda sym: adx,
        adx_direction_for=lambda sym: direction,
    )


def test_strong_uptrend_adx_overrides_condor_to_bull_put() -> None:
    d = s.select_regime(
        _snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP),   # base = iron_condor
        context=_adx_ctx(adx=32.0, direction="up"),
    )
    assert d.regime == s.REGIME_BULL_PUT
    assert "ADX OVERRIDE" in d.label and d.direction == "up"


def test_strong_downtrend_adx_overrides_condor_to_bear_call() -> None:
    d = s.select_regime(
        _snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP),
        context=_adx_ctx(adx=40.0, direction="down"),
    )
    assert d.regime == s.REGIME_BEAR_CALL
    assert "ADX OVERRIDE" in d.label


def test_adx_grey_zone_leaves_the_condor_alone() -> None:
    d = s.select_regime(
        _snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP),
        context=_adx_ctx(adx=22.0, direction="up"),   # 20-25 -> fall back to ER
    )
    assert d.regime == s.REGIME_IRON_CONDOR


def test_strong_adx_without_direction_stands_aside() -> None:
    d = s.select_regime(
        _snap(atm_iv=0.22, spread=0.05, iv_eligible=True, closes=CHOP),
        context=_adx_ctx(adx=30.0, direction=None),   # CHOP has no trend_direction
    )
    assert d.regime == s.REGIME_NONE
    assert "no clear direction" in d.label


def test_adx_does_not_touch_a_non_condor_regime() -> None:
    # Regime B (low IV / range-bound) -> long strangle; ADX filter only kills condors
    d = s.select_regime(
        _snap(atm_iv=0.10, spread=-0.06, iv_eligible=False, closes=CHOP),
        context=_adx_ctx(adx=45.0, direction="up"),
    )
    assert d.regime == s.REGIME_LONG_STRANGLE
