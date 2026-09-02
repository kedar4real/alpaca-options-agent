"""Offline unit tests for risk_manager.py — one block per gate, with edge cases."""

from __future__ import annotations

from datetime import date

import pytest

from trading_agent import risk_manager as rm
from trading_agent.risk_manager import OrderLeg

# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
CONDOR = (
    OrderLeg("buy", "put", 1),
    OrderLeg("sell", "put", 1),
    OrderLeg("sell", "call", 1),
    OrderLeg("buy", "call", 1),
)
FRIDAY = date(2026, 9, 4)  # Mon 2026-09-07 is Labor Day; next session is Tue 09-08


def condor_legs(qty: int) -> tuple[OrderLeg, ...]:
    return (
        OrderLeg("buy", "put", qty),
        OrderLeg("sell", "put", qty),
        OrderLeg("sell", "call", qty),
        OrderLeg("buy", "call", qty),
    )


def order(wing: float = 5.0, credit: float = 2.0, qty: int = 1,
          underlying: str | None = None) -> rm.ProposedOrder:
    # per-contract risk = (5.0 - 2.0) * 100 = $300
    return rm.ProposedOrder(wing, credit, qty, condor_legs(qty), underlying=underlying)


def account(
    current: float,
    *,
    starting: float = 100_000.0,
    day_start: float | None = None,
    positions: tuple[rm.OpenPosition, ...] = (),
    halted: bool = False,
    risk_mult: float = 1.0,
) -> rm.AccountState:
    return rm.AccountState(
        starting_equity=starting,
        current_equity=current,
        day_start_equity=current if day_start is None else day_start,
        open_positions=positions,
        trading_halted=halted,
        risk_multiplier=risk_mult,
    )


def pos(expiry: date = date(2027, 1, 15), symbol: str = "SPY_CONDOR") -> rm.OpenPosition:
    return rm.OpenPosition(symbol, expiry, 1, CONDOR)


# --------------------------------------------------------------------------- #
# Gate 1 — max risk per trade (2.0% of current equity)
# --------------------------------------------------------------------------- #
def test_max_risk_per_trade_exactly_at_threshold_is_allowed() -> None:
    # 5 contracts * $400 = $2,000 == 2.0% of $100,000
    d = rm.check_order(order(wing=6.0, credit=2.0, qty=5), account(100_000))
    assert d.order_risk == 2_000.0 and d.max_risk_allowed == 2_000.0
    assert d.checks["max_risk_per_trade"] is True
    assert d.approved is True


def test_max_risk_per_trade_one_contract_over_is_blocked() -> None:
    d = rm.check_order(order(qty=7), account(100_000))  # 7 * $300 = $2,100 > $2,000
    assert d.checks["max_risk_per_trade"] is False
    assert d.approved is False
    assert any("trade risk" in b for b in d.blocks)


def test_max_risk_per_trade_scales_with_current_equity() -> None:
    # cap tracks *current* equity, not starting
    d = rm.check_order(order(qty=7), account(90_000))  # $2,100 > 2% of $90k = $1,800
    assert d.checks["max_risk_per_trade"] is False


# --------------------------------------------------------------------------- #
# Gate 1 — macro guard (High-Impact day halves the per-trade cap)
# --------------------------------------------------------------------------- #
def test_is_macro_safe_predicate() -> None:
    assert rm.is_macro_safe(macro_high_impact=False) is True
    assert rm.is_macro_safe(macro_high_impact=True) is False


def test_macro_risk_multiplier_is_half_on_a_high_impact_day() -> None:
    assert rm.macro_risk_multiplier(macro_high_impact=False) == 1.0
    assert rm.macro_risk_multiplier(macro_high_impact=True) == 0.5


def test_default_account_risk_multiplier_is_one_and_unchanged_behaviour() -> None:
    # a trade at exactly 2.0% still passes when no macro reduction is applied
    d = rm.check_order(order(wing=6.0, credit=2.0, qty=5), account(100_000))
    assert d.max_risk_allowed == 2_000.0 and d.approved is True


def test_macro_day_halves_the_effective_cap_without_touching_the_constant() -> None:
    before = rm.MAX_RISK_PER_TRADE_PCT
    # 5 contracts * $300 = $1,500 : fine normally, blocked when the cap is halved to $1,000
    ok = rm.check_order(order(qty=5), account(100_000))
    assert ok.checks["max_risk_per_trade"] is True

    macro = rm.check_order(order(qty=5), account(100_000, risk_mult=0.5))
    assert macro.max_risk_allowed == 1_000.0
    assert macro.checks["max_risk_per_trade"] is False
    assert any("trade risk" in b for b in macro.blocks)
    assert rm.MAX_RISK_PER_TRADE_PCT == before == 0.02      # constant is the source of truth


def test_macro_reduction_never_loosens_a_limit() -> None:
    # a multiplier > 1 is not something the agent sets, but the gate must still
    # never allow more than the 2.0% line even if handed one
    d = rm.check_order(order(qty=7), account(100_000, risk_mult=2.0))
    # $2,100 risk vs a (wrongly) doubled $4,000 cap -> we clamp the multiplier at 1.0
    assert d.max_risk_allowed == 2_000.0
    assert d.checks["max_risk_per_trade"] is False


# --------------------------------------------------------------------------- #
# Gate 2 — daily loss halt (3.5% of starting equity)
# --------------------------------------------------------------------------- #
def test_daily_loss_at_or_over_threshold_halts() -> None:
    d = rm.check_order(order(), account(96_400, day_start=100_000))  # -$3,600 (>= 3.5%)
    assert d.daily_loss == 3_600.0
    assert d.daily_loss_limit == pytest.approx(3_500.0)
    assert d.checks["daily_loss_halt"] is False
    assert d.approved is False
    assert any("no new trades today" in b for b in d.blocks)


def test_daily_loss_one_dollar_under_threshold_passes() -> None:
    d = rm.check_order(order(), account(96_501, day_start=100_000))  # -$3,499
    assert d.checks["daily_loss_halt"] is True
    assert d.approved is True


def test_daily_loss_measured_from_day_start_not_starting_equity() -> None:
    # down $4,000 on the day but the account is *up* overall -> still halted
    d = rm.check_order(order(), account(120_000, day_start=124_000))
    assert d.checks["daily_loss_halt"] is False
    assert d.checks["total_drawdown_floor"] is True


# --------------------------------------------------------------------------- #
# Gate 3 — total drawdown floor (5% of starting equity)
# --------------------------------------------------------------------------- #
def test_total_drawdown_exactly_at_floor_halts() -> None:
    d = rm.check_order(order(), account(95_000))  # -$5,000 vs starting
    assert d.total_drawdown == 5_000.0 and d.drawdown_limit == 5_000.0
    assert d.checks["total_drawdown_floor"] is False
    assert d.approved is False
    assert any("halt all trading" in b for b in d.blocks)


def test_total_drawdown_one_dollar_above_floor_passes() -> None:
    d = rm.check_order(order(), account(95_001))  # -$4,999
    assert d.checks["total_drawdown_floor"] is True
    assert d.approved is True


def test_sticky_competition_halt_blocks_even_when_healthy() -> None:
    d = rm.check_order(order(), account(100_000, halted=True))
    assert d.checks["total_drawdown_floor"] is False
    assert d.approved is False
    assert any("halted for the competition" in b for b in d.blocks)


# --------------------------------------------------------------------------- #
# Gate 4 — max concurrent positions (3)
# --------------------------------------------------------------------------- #
def test_two_open_positions_allows_new_trade() -> None:
    d = rm.check_order(order(), account(100_000, positions=(pos(), pos())))
    assert d.checks["max_concurrent_positions"] is True
    assert d.approved is True


def test_three_open_positions_still_allows_a_fourth() -> None:
    d = rm.check_order(order(), account(100_000, positions=(pos(), pos(), pos())))
    assert d.open_position_count == 3
    assert d.checks["max_concurrent_positions"] is True
    assert d.approved is True


def test_four_open_positions_blocks_the_fifth() -> None:
    d = rm.check_order(order(), account(100_000, positions=(pos(), pos(), pos(), pos())))
    assert d.open_position_count == 4
    assert d.checks["max_concurrent_positions"] is False
    assert d.approved is False


# --------------------------------------------------------------------------- #
# Gate 4b — correlation guard (a >0.8 10d cluster gets one slot)
# --------------------------------------------------------------------------- #
EQUITY_CLUSTER = (frozenset({"SPY", "QQQ", "IWM"}),)


def _pos_u(underlying: str, tid: str = "id") -> rm.OpenPosition:
    return rm.OpenPosition(tid, date(2027, 1, 15), 1, CONDOR, underlying=underlying)


def test_correlation_guard_blocks_a_second_name_in_an_open_cluster() -> None:
    d = rm.check_order(
        order(underlying="QQQ"),
        account(100_000, positions=(_pos_u("SPY"),)),
        correlation_clusters=EQUITY_CLUSTER,
    )
    assert d.checks["correlation_guard"] is False
    assert d.approved is False
    assert any("correlation guard" in b and "SPY" in b for b in d.blocks)


def test_correlation_guard_allows_an_uncorrelated_name() -> None:
    d = rm.check_order(
        order(underlying="TLT"),
        account(100_000, positions=(_pos_u("SPY"),)),
        correlation_clusters=EQUITY_CLUSTER,
    )
    assert d.checks["correlation_guard"] is True
    assert d.approved is True


def test_correlation_guard_allows_the_first_name_in_a_cluster() -> None:
    d = rm.check_order(
        order(underlying="QQQ"),
        account(100_000, positions=(_pos_u("TLT"),)),
        correlation_clusters=EQUITY_CLUSTER,
    )
    assert d.checks["correlation_guard"] is True
    assert d.approved is True


def test_correlation_guard_is_inert_without_clusters() -> None:
    d = rm.check_order(
        order(underlying="QQQ"),
        account(100_000, positions=(_pos_u("SPY"),)),
    )
    assert "correlation_guard" not in d.checks
    assert d.approved is True


# --------------------------------------------------------------------------- #
# Gate 4c — long-volatility concentration (<= 4% debit, <= 2 positions)
# --------------------------------------------------------------------------- #
def _lv_legs(qty: int = 1) -> tuple[OrderLeg, ...]:
    return (OrderLeg("buy", "put", qty), OrderLeg("buy", "call", qty))


def _lv_order(debit_per_spread: float, qty: int) -> rm.ProposedOrder:
    return rm.ProposedOrder(
        wing_width=0.0, net_credit=-debit_per_spread, quantity=qty,
        legs=_lv_legs(qty), max_loss=debit_per_spread * 100,
    )


def _lv_pos(debit_per_spread: float, qty: int, tid: str) -> rm.OpenPosition:
    return rm.OpenPosition(tid, FRIDAY, qty, _lv_legs(qty), underlying="X",
                           entry_credit=-debit_per_spread, structure="long_strangle")


def test_long_vol_position_count_at_the_limit_blocks_the_next_one() -> None:
    at_cap = tuple(_lv_pos(1.0, 1, c) for c in "abc")   # 3 open == MAX_LONG_VOL_POSITIONS
    d = rm.check_order(_lv_order(0.50, 1), account(100_000, positions=at_cap))
    assert d.checks["long_vol_concentration"] is False
    assert any("open long-vol positions" in b for b in d.blocks)
    assert d.approved is False


def test_third_long_vol_position_is_allowed() -> None:
    two = (_lv_pos(1.0, 1, "a"), _lv_pos(1.0, 1, "b"))
    d = rm.check_order(_lv_order(0.50, 1), account(100_000, positions=two))
    assert d.checks["long_vol_concentration"] is True


def test_long_vol_debit_exactly_at_the_4pct_cap_is_allowed() -> None:
    existing = (_lv_pos(3.0, 10, "a"),)                    # 3.0 * 10 * 100 = $3,000
    d = rm.check_order(_lv_order(1.0, 10), account(100_000, positions=existing))  # + $1,000
    assert d.checks["long_vol_concentration"] is True      # $4,000 == 4% of $100k, inclusive


def test_long_vol_debit_one_notch_over_the_cap_is_blocked() -> None:
    existing = (_lv_pos(3.0, 10, "a"),)                    # $3,000
    d = rm.check_order(_lv_order(1.01, 10), account(100_000, positions=existing))  # + $1,010
    assert d.checks["long_vol_concentration"] is False
    assert any("total long-vol debit" in b for b in d.blocks)


def test_concentration_gate_does_not_touch_credit_structures() -> None:
    two_lv = (_lv_pos(1.0, 1, "a"), _lv_pos(1.0, 1, "b"))
    d = rm.check_order(order(wing=5.0, credit=2.0, qty=1),
                       account(100_000, positions=two_lv))
    assert "long_vol_concentration" not in d.checks       # net-credit order is exempt
    assert d.approved is True


# --------------------------------------------------------------------------- #
# Gate 5 — defined-risk invariant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("legs", "expected"),
    [
        (condor_legs(1), True),                                              # iron condor
        ((OrderLeg("buy", "put", 1), OrderLeg("sell", "put", 1)), True),     # vertical
        ((OrderLeg("buy", "put", 2), OrderLeg("sell", "put", 2)), True),     # matched, qty 2
        ((OrderLeg("buy", "put", 3), OrderLeg("buy", "call", 3)), True),     # long strangle (all-long)
        ((OrderLeg("buy", "call", 1),), True),                              # single long call
        ((OrderLeg("buy", "put", 3), OrderLeg("buy", "call", 0)), False),    # all-long but zero qty
        (
            (OrderLeg("buy", "put", 3), OrderLeg("buy", "call", 3),
             OrderLeg("sell", "call", 1)),
            False,                                                          # one short leg -> matched rule, uncovered
        ),
        ((OrderLeg("sell", "put", 1),), False),                             # naked short
        (
            (OrderLeg("sell", "put", 1), OrderLeg("sell", "call", 1), OrderLeg("buy", "call", 1)),
            False,                                                          # put leg uncovered
        ),
        (
            (OrderLeg("buy", "put", 1), OrderLeg("sell", "put", 2),
             OrderLeg("sell", "call", 1), OrderLeg("buy", "call", 1)),
            False,                                                          # qty mismatch on puts
        ),
        ((), False),                                                       # no legs
        ((OrderLeg("open", "put", 1), OrderLeg("sell", "put", 1)), False),  # bad action
        ((OrderLeg("buy", "put", 0), OrderLeg("sell", "put", 0)), False),   # zero-qty legs
    ],
)
def test_is_defined_risk(legs, expected) -> None:
    assert rm.is_defined_risk(tuple(legs)) is expected


def test_check_order_rejects_naked_short_put() -> None:
    naked = rm.ProposedOrder(5.0, 2.0, 1, (OrderLeg("sell", "put", 1),))
    d = rm.check_order(naked, account(100_000))
    assert d.checks["defined_risk"] is False
    assert d.approved is False
    assert any("not defined-risk" in b for b in d.blocks)


# --------------------------------------------------------------------------- #
# Gate 6 — expiration auto-close
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("expiry", "dte"),
    [
        (date(2026, 9, 4), 0),   # today
        (date(2026, 9, 3), 0),   # already past expiry
        (date(2026, 9, 8), 1),   # next session (Mon 09-07 is Labor Day)
        (date(2026, 9, 9), 2),   # two sessions out
        (date(2026, 9, 10), 3),
    ],
)
def test_trading_days_until(expiry, dte) -> None:
    assert rm.trading_days_until(expiry, today=FRIDAY) == dte


def test_flag_expiring_positions_flags_zero_and_one_dte_only() -> None:
    positions = (
        rm.OpenPosition("A", date(2026, 9, 8), 1, CONDOR),    # 1 dte  -> flag
        rm.OpenPosition("B", date(2026, 9, 4), 1, CONDOR),    # 0 dte  -> flag
        rm.OpenPosition("C", date(2026, 9, 9), 1, CONDOR),    # 2 dte  -> keep
        rm.OpenPosition("D", date(2027, 1, 15), 1, CONDOR),   # far    -> keep
    )
    flagged = rm.flag_expiring_positions(positions, today=FRIDAY)
    assert {f.position.symbol for f in flagged} == {"A", "B"}
    assert {f.position.symbol: f.trading_days_to_expiry for f in flagged} == {"A": 1, "B": 0}


def test_flag_expiring_positions_is_holiday_aware() -> None:
    # Fri 09-04 -> a position expiring Tue 09-08 is still only 1 trading day away
    # (4 calendar days) because Mon 09-07 is Labor Day.
    positions = (rm.OpenPosition("X", date(2026, 9, 8), 1, CONDOR),)
    assert len(rm.flag_expiring_positions(positions, today=FRIDAY)) == 1


def test_flag_expiring_positions_empty_when_nothing_close() -> None:
    positions = (rm.OpenPosition("X", date(2026, 9, 11), 1, CONDOR),)  # 4 dte
    assert rm.flag_expiring_positions(positions, today=FRIDAY) == []


# --------------------------------------------------------------------------- #
# Combined
# --------------------------------------------------------------------------- #
def test_clean_order_is_approved_with_no_blocks() -> None:
    d = rm.check_order(order(qty=3), account(100_000))
    assert d.approved is True
    assert d.blocks == []
    assert all(d.checks.values())


def test_every_gate_can_fail_at_once() -> None:
    acct = account(
        50_000,                       # -50% total  -> drawdown floor breached
        starting=100_000,
        day_start=54_000,             # -$4,000 today -> daily loss halt (>= $3,500)
        positions=(pos(), pos(), pos(), pos()),  # 4 open -> max positions
    )
    d = rm.check_order(order(qty=10), acct)  # $3,000 risk vs $1,000 (2% of $50k) cap
    assert d.approved is False
    assert d.checks["max_risk_per_trade"] is False
    assert d.checks["daily_loss_halt"] is False
    assert d.checks["total_drawdown_floor"] is False
    assert d.checks["max_concurrent_positions"] is False
    assert d.checks["defined_risk"] is True
    assert len(d.blocks) == 4


# --------------------------------------------------------------------------- #
# ProposedOrder.max_loss — debit structures (additive; limits unchanged)
# --------------------------------------------------------------------------- #
def test_max_loss_overrides_the_credit_formula_for_debit_structures() -> None:
    # a long strangle: net debit 2.10/spread, 3 contracts -> risk = 210 * 3
    strangle = rm.ProposedOrder(
        wing_width=0.0, net_credit=-2.10, quantity=3,
        legs=(OrderLeg("buy", "put", 3), OrderLeg("buy", "call", 3)),
        max_loss=210.0,
    )
    assert strangle.risk_dollars == pytest.approx(630.0)


def test_max_loss_none_keeps_the_classic_formula() -> None:
    credit = rm.ProposedOrder(wing_width=5.0, net_credit=2.0, quantity=2)
    assert credit.risk_dollars == pytest.approx((5.0 - 2.0) * 100 * 2)


def test_gate_1_uses_max_loss_for_a_long_strangle() -> None:
    ok = rm.ProposedOrder(
        0.0, -2.10, 3,
        (OrderLeg("buy", "put", 3), OrderLeg("buy", "call", 3)),
        max_loss=210.0,
    )                                             # risk 630 vs 1.5% of 100k = 1500
    d = rm.check_order(ok, account(100_000))
    assert d.approved is True
    assert d.checks["defined_risk"] is True
    assert d.checks["max_risk_per_trade"] is True
    assert d.order_risk == pytest.approx(630.0)

    too_big = rm.ProposedOrder(
        0.0, -8.00, 3,
        (OrderLeg("buy", "put", 3), OrderLeg("buy", "call", 3)),
        max_loss=800.0,
    )                                             # risk 2400 > 2000 (2%) cap
    d2 = rm.check_order(too_big, account(100_000))
    assert d2.approved is False
    assert d2.checks["max_risk_per_trade"] is False
