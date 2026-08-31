"""Offline tests for data.py IV-percentile + IV-regime gate (no network)."""

from __future__ import annotations

import math
import statistics

import pytest

from trading_agent import data as d


# --------------------------------------------------------------------------- #
# calculate_iv_percentile
# --------------------------------------------------------------------------- #
def test_percentile_none_without_enough_history() -> None:
    assert d.calculate_iv_percentile(0.20, [0.1] * 9) is None


def test_percentile_none_when_current_iv_missing() -> None:
    assert d.calculate_iv_percentile(None, [0.1] * 20) is None


def test_percentile_top_and_bottom() -> None:
    hist = [0.10 + i * 0.01 for i in range(10)]  # 0.10 .. 0.19
    assert d.calculate_iv_percentile(0.25, hist) == 100.0
    assert d.calculate_iv_percentile(0.05, hist) == 0.0


def test_percentile_midrange() -> None:
    hist = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.28]
    assert d.calculate_iv_percentile(0.19, hist) == 50.0


# --------------------------------------------------------------------------- #
# evaluate_iv_regime — Hackathon Mode (thin history)
# --------------------------------------------------------------------------- #
def test_regime_hackathon_eligible_above_static_threshold() -> None:
    r = d.evaluate_iv_regime(0.20, [])
    assert r.mode == "hackathon_static"
    assert r.trade_eligible is True
    assert "Hackathon Mode" in r.reason


def test_regime_hackathon_blocked_below_static_threshold() -> None:
    r = d.evaluate_iv_regime(0.12, [0.1] * 5)
    assert r.mode == "hackathon_static"
    assert r.trade_eligible is False


def test_regime_hackathon_boundary_is_strict() -> None:
    # exactly the threshold is NOT eligible (must be > 15%)
    assert d.evaluate_iv_regime(d.STATIC_IV_THRESHOLD, []).trade_eligible is False


def test_regime_blocked_when_iv_unavailable() -> None:
    r = d.evaluate_iv_regime(None, [])
    assert r.trade_eligible is False
    assert r.mode == "hackathon_static"
    assert "unavailable" in r.reason


# --------------------------------------------------------------------------- #
# evaluate_iv_regime — percentile mode (>= 10 days)
# --------------------------------------------------------------------------- #
def test_regime_percentile_eligible_when_elevated() -> None:
    hist = [0.10] * 20
    r = d.evaluate_iv_regime(0.30, hist)
    assert r.mode == "percentile"
    assert r.iv_percentile == 100.0
    assert r.trade_eligible is True


def test_regime_percentile_blocked_when_below_median() -> None:
    hist = [0.10 + i * 0.01 for i in range(20)]  # 0.10 .. 0.29
    r = d.evaluate_iv_regime(0.11, hist)
    assert r.mode == "percentile"
    assert r.trade_eligible is False


def test_regime_percentile_threshold_is_inclusive() -> None:
    hist = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.28]
    r = d.evaluate_iv_regime(0.19, hist)  # exactly 50.0
    assert r.iv_percentile == 50.0
    assert r.trade_eligible is True


# --------------------------------------------------------------------------- #
# calculate_realized_vol
# --------------------------------------------------------------------------- #
def _prices_from_log_returns(start, returns):
    prices = [start]
    for r in returns:
        prices.append(prices[-1] * math.exp(r))
    return prices


def test_realized_vol_none_without_enough_closes() -> None:
    # window + 1 = 11 closes required
    assert d.calculate_realized_vol([100.0] * 10) is None


def test_realized_vol_none_on_non_positive_price() -> None:
    assert d.calculate_realized_vol([100.0] * 10 + [0.0]) is None


def test_realized_vol_flat_series_is_zero() -> None:
    assert d.calculate_realized_vol([100.0] * 11) == 0.0


def test_realized_vol_matches_independent_calc_and_annualizes() -> None:
    returns = [0.004, -0.006, 0.010, -0.002, 0.007, -0.009, 0.003, 0.005, -0.004, 0.008]
    prices = _prices_from_log_returns(100.0, returns)  # 11 closes
    expected = round(statistics.stdev(returns) * math.sqrt(252), 4)
    assert d.calculate_realized_vol(prices) == expected


def test_realized_vol_uses_only_the_last_window() -> None:
    tail = [0.004, -0.006, 0.010, -0.002, 0.007, -0.009, 0.003, 0.005, -0.004, 0.008]
    # 10 quiet days then the 10 `tail` days; window=10 must ignore the quiet prefix
    quiet = _prices_from_log_returns(50.0, [0.0001] * 10)
    prices = quiet + _prices_from_log_returns(quiet[-1], tail)[1:]
    expected = round(statistics.stdev(tail) * math.sqrt(252), 4)
    assert d.calculate_realized_vol(prices, window=10) == expected


def test_realized_vol_bigger_swings_give_higher_vol() -> None:
    calm = d.calculate_realized_vol(_prices_from_log_returns(100.0, [0.002, -0.002] * 5))
    wild = d.calculate_realized_vol(_prices_from_log_returns(100.0, [0.02, -0.02] * 5))
    assert wild > calm > 0.0
