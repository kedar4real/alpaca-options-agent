"""Offline tests for strategy.py iron-condor construction (no network)."""

from __future__ import annotations

import math
from datetime import date

import pytest

from trading_agent import strategy as s
from trading_agent.alpaca_trader import OptionContract
from trading_agent.data import IVRegime

EXPIRY = date(2026, 9, 8)          # a Tuesday; 1 trading day after Fri 2026-09-04
TODAY = date(2026, 9, 4)          # so nth_trading_day(1..3, TODAY) == 09-08 .. 09-10
ELIGIBLE = IVRegime(0.20, None, "hackathon_static", True, "test-eligible")
BLOCKED = IVRegime(0.10, None, "hackathon_static", False, "test-blocked")


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
    # widen wings to 20 so credit/width falls well under 25%
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
    assert "below 25% target" in plan.reason
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
