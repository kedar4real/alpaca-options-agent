"""Offline tests for offhours.py — the Off-Hours Intelligence layer.

Three timed behaviours, all pure functions here (scheduling/wiring lives in
main.py and is tested in test_main.py):

  * Heartbeat        — hourly "alive" line, market open or closed
  * Morning Brief     — pre-market gap scan 09:00-09:30 ET
  * Nightly Post-Mortem — end-of-day pipeline funnel digest

No network, no clock dependence beyond the datetimes passed in.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trading_agent.offhours import (
    DailyActivity,
    Heartbeat,
    TickerGap,
    accumulate_activity,
    build_heartbeat,
    count_iv_readings,
    dominant_regime,
    in_morning_brief_window,
    interval_elapsed,
    morning_brief_text,
    post_mortem_text,
)

ET = ZoneInfo("America/New_York")


# ======================================================================= #
# count_iv_readings
# ======================================================================= #
def test_count_iv_readings_counts_data_rows_not_header_or_blanks(tmp_path) -> None:
    p = tmp_path / "iv_history.csv"
    p.write_text(
        "timestamp,symbol,iv,rv,spread\n"
        "2026-09-01T01:10:15,SPY,0.10,0.07,0.03\n"
        "2026-09-01T01:10:19,QQQ,0.14,0.13,0.01\n"
        "\n"
    )
    assert count_iv_readings(str(p)) == 2


def test_count_iv_readings_zero_when_file_missing(tmp_path) -> None:
    assert count_iv_readings(str(tmp_path / "nope.csv")) == 0


# ======================================================================= #
# Heartbeat
# ======================================================================= #
def test_heartbeat_render_matches_the_required_format() -> None:
    hb = build_heartbeat(
        datetime(2026, 9, 1, 14, 30, tzinfo=ET),
        market_open=False, connectivity_ok=True, iv_readings=6,
    )
    assert hb.render() == (
        "[2026-09-01 14:30] HEARTBEAT: Status: Idle | "
        "Connectivity: OK | Memory: 6 IV readings stored."
    )


def test_heartbeat_status_is_active_when_market_open() -> None:
    hb = build_heartbeat(datetime(2026, 9, 1, 10, 0, tzinfo=ET),
                         market_open=True, connectivity_ok=True, iv_readings=0)
    assert hb.status == "Active"


def test_heartbeat_connectivity_error_when_not_ok() -> None:
    hb = build_heartbeat(datetime(2026, 9, 1, 10, 0, tzinfo=ET),
                         market_open=False, connectivity_ok=False, iv_readings=3)
    assert hb.connectivity == "Error"
    assert "Connectivity: Error" in hb.render()


# ======================================================================= #
# interval_elapsed — the hourly gate
# ======================================================================= #
def test_interval_elapsed_true_when_no_previous_stamp() -> None:
    assert interval_elapsed("", datetime(2026, 9, 1, 10, 0, tzinfo=ET)) is True


def test_interval_elapsed_false_within_the_hour() -> None:
    last = datetime(2026, 9, 1, 10, 0, tzinfo=ET).isoformat()
    assert interval_elapsed(last, datetime(2026, 9, 1, 10, 45, tzinfo=ET)) is False


def test_interval_elapsed_true_after_the_hour() -> None:
    last = datetime(2026, 9, 1, 10, 0, tzinfo=ET).isoformat()
    assert interval_elapsed(last, datetime(2026, 9, 1, 11, 1, tzinfo=ET)) is True


def test_interval_elapsed_true_on_unparseable_stamp() -> None:
    assert interval_elapsed("garbage", datetime(2026, 9, 1, 11, 0, tzinfo=ET)) is True


def test_interval_elapsed_honours_a_custom_gap() -> None:
    last = datetime(2026, 9, 1, 10, 0, tzinfo=ET).isoformat()
    now = datetime(2026, 9, 1, 10, 20, tzinfo=ET)
    assert interval_elapsed(last, now, min_gap_seconds=900) is True
    assert interval_elapsed(last, now, min_gap_seconds=3600) is False


# ======================================================================= #
# Morning Brief
# ======================================================================= #
def test_morning_brief_window_is_0900_to_0930_et() -> None:
    assert in_morning_brief_window(datetime(2026, 9, 1, 9, 0, tzinfo=ET)) is True
    assert in_morning_brief_window(datetime(2026, 9, 1, 9, 29, tzinfo=ET)) is True
    assert in_morning_brief_window(datetime(2026, 9, 1, 9, 30, tzinfo=ET)) is False
    assert in_morning_brief_window(datetime(2026, 9, 1, 8, 59, tzinfo=ET)) is False


def test_ticker_gap_pct_and_significance() -> None:
    g = TickerGap("SPY", prev_close=600.0, premarket=606.0)
    assert g.gap_pct == 1.0
    assert g.is_significant() is True

    flat = TickerGap("QQQ", prev_close=500.0, premarket=501.0)
    assert flat.gap_pct == 0.2
    assert flat.is_significant() is False


def test_morning_brief_flags_a_gap_as_trending() -> None:
    gaps = [
        TickerGap("SPY", 600.0, 604.5),   # +0.75% -> significant
        TickerGap("QQQ", 500.0, 500.5),   # +0.10% -> flat
    ]
    text = morning_brief_text(gaps, et_date=date(2026, 9, 1))
    assert "Pre-Market Brief" in text
    assert "SPY" in text and "+0.75%" in text
    assert "PRE-MARKET ALERT" in text
    assert "TRENDING" in text
    # the flat name must not raise an alert
    assert text.count("*") == 1


def test_morning_brief_all_flat_says_range_bound_intact() -> None:
    gaps = [TickerGap("SPY", 600.0, 600.3), TickerGap("TLT", 90.0, 90.1)]
    text = morning_brief_text(gaps, et_date=date(2026, 9, 1))
    assert "PRE-MARKET ALERT" not in text
    assert "RANGE-BOUND" in text


def test_morning_brief_handles_no_quotes() -> None:
    text = morning_brief_text([], et_date=date(2026, 9, 1))
    assert "no pre-market quotes" in text.lower()


# ======================================================================= #
# Nightly Post-Mortem
# ======================================================================= #
def _decision(stage, outcome, regime=None):
    plan = SimpleNamespace(regime=regime) if regime is not None else None
    return SimpleNamespace(stage=stage, outcome=outcome, plan=plan)


def test_accumulate_activity_counts_the_pipeline_funnel() -> None:
    act = DailyActivity(date="2026-09-01", basket_size=4)
    decisions = [
        _decision("executor", "executed", "Regime A: High Volatility -> Iron Condor"),
        _decision("risk_manager", "blocked", "Regime A: High Volatility -> Iron Condor"),
        _decision("risk_officer", "vetoed", "Regime B: Low IV / Range-Bound -> Long Strangle"),
        _decision("strategy", "skipped", "No trade"),
    ]
    accumulate_activity(act, decisions)
    assert act.ticker_scans == 4
    assert act.proposed == 3          # reached risk_manager or later
    assert act.approved == 1
    assert act.rm_vetoes == 1
    assert act.ro_vetoes == 1
    assert act.regimes["No trade"] == 1
    assert act.regimes["Regime A: High Volatility -> Iron Condor"] == 2


def test_accumulate_activity_is_cumulative_across_cycles() -> None:
    act = DailyActivity(date="2026-09-01", basket_size=2)
    accumulate_activity(act, [_decision("strategy", "skipped", "No trade")])
    accumulate_activity(act, [_decision("strategy", "skipped", "No trade")])
    assert act.ticker_scans == 2
    assert act.regimes["No trade"] == 2


def test_accumulate_activity_tolerates_decisions_without_a_plan() -> None:
    act = DailyActivity(date="2026-09-01")
    accumulate_activity(act, [_decision("precheck", "error"), _decision("precheck", "halted")])
    assert act.ticker_scans == 2
    assert act.proposed == 0
    assert act.regimes == {}


def test_dominant_regime_picks_the_most_frequent_and_buckets_it() -> None:
    regimes = {
        "Regime B: Low IV / Range-Bound -> Long Strangle": 12,
        "Regime A: High Volatility -> Iron Condor": 3,
    }
    out = dominant_regime(regimes)
    assert "Overall Range-Bound" in out
    assert "12" in out


def test_dominant_regime_handles_no_data() -> None:
    assert "n/a" in dominant_regime({})


def test_post_mortem_text_is_a_digest_with_the_funnel() -> None:
    act = DailyActivity(date="2026-09-01", basket_size=4, ticker_scans=104,
                        proposed=6, approved=2, rm_vetoes=1, ro_vetoes=3,
                        regimes={"Regime B: Low IV / Range-Bound -> Long Strangle": 80,
                                 "No trade": 24})
    text = post_mortem_text(act, et_date=date(2026, 9, 1),
                            open_positions=2, unrealized_pnl=145.0)
    assert "Nightly Post-Mortem" in text
    assert "104" in text                     # ticker scans
    assert "Trades proposed" in text and "6" in text
    assert "Trades approved" in text and "2" in text
    assert "risk_manager" in text and "risk_officer" in text
    assert "$+145" in text
    assert "Overall Range-Bound" in text


def test_post_mortem_text_handles_no_open_positions() -> None:
    act = DailyActivity(date="2026-09-01", basket_size=4, ticker_scans=40)
    text = post_mortem_text(act, et_date=date(2026, 9, 1),
                            open_positions=0, unrealized_pnl=None)
    assert "Open positions:" in text and "0" in text
    assert "n/a" in text                      # unrealized P&L unknown


def test_daily_activity_round_trips_through_dict() -> None:
    act = DailyActivity(date="2026-09-01", basket_size=4, ticker_scans=10,
                        proposed=2, approved=1, rm_vetoes=1, ro_vetoes=0,
                        regimes={"No trade": 8})
    back = DailyActivity.from_dict(act.to_dict())
    assert back == act
