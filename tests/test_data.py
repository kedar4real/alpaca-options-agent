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
    r = d.evaluate_iv_regime(0.05, [0.1] * 5)   # 5% < 8% static floor
    assert r.mode == "hackathon_static"
    assert r.trade_eligible is False


def test_regime_hackathon_boundary_is_strict() -> None:
    # exactly the threshold is NOT eligible (must be strictly > STATIC_IV_THRESHOLD)
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


# --------------------------------------------------------------------------- #
# log_iv_reading — one shared file, symbol column, appended every cycle
# --------------------------------------------------------------------------- #
def _rows(path):
    import csv
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_log_iv_reading_creates_file_with_new_schema(tmp_path) -> None:
    p = tmp_path / "iv.csv"
    d.log_iv_reading("SPY", 0.20, 0.14, 0.06, log_path=str(p))
    rows = _rows(p)
    assert list(rows[0].keys()) == ["timestamp", "symbol", "iv", "rv", "spread"]
    assert rows[0]["symbol"] == "SPY" and rows[0]["iv"] == "0.2"
    assert rows[0]["rv"] == "0.14" and rows[0]["spread"] == "0.06"


def test_log_iv_reading_appends_every_call_for_every_ticker(tmp_path) -> None:
    p = tmp_path / "iv.csv"
    d.log_iv_reading("SPY", 0.20, 0.14, 0.06, log_path=str(p))
    d.log_iv_reading("QQQ", 0.18, 0.22, -0.04, log_path=str(p))
    d.log_iv_reading("SPY", 0.21, 0.14, 0.07, log_path=str(p))   # same day, still appended
    rows = _rows(p)
    assert len(rows) == 3
    assert [r["symbol"] for r in rows] == ["SPY", "QQQ", "SPY"]


def test_read_iv_history_filters_by_symbol_and_collapses_to_one_per_day(tmp_path) -> None:
    p = tmp_path / "iv.csv"
    p.write_text(
        "timestamp,symbol,iv,rv,spread\n"
        "2026-08-30T10:00:00,SPY,0.11,,\n"
        "2026-08-31T10:00:00,SPY,0.12,,\n"
        "2026-08-31T15:00:00,SPY,0.19,,\n"    # later same-day reading wins
        "2026-08-31T15:00:01,QQQ,0.30,,\n"    # other ticker ignored
        "2026-09-01T10:00:00,SPY,0.13,,\n"
    )
    assert d.read_iv_history("SPY", log_path=str(p)) == [0.11, 0.19, 0.13]
    assert d.read_iv_history("QQQ", log_path=str(p)) == [0.30]
    assert d.read_iv_history("TLT", log_path=str(p)) == []


def test_read_iv_history_tolerates_blank_iv(tmp_path) -> None:
    p = tmp_path / "iv.csv"
    p.write_text(
        "timestamp,symbol,iv,rv,spread\n"
        "2026-08-31T10:00:00,SPY,,,\n"        # blank iv -> skipped
        "2026-09-01T10:00:00,SPY,0.13,,\n"
    )
    assert d.read_iv_history("SPY", log_path=str(p)) == [0.13]
