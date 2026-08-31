"""Offline tests for executor.py — gate-before-submit, MLEG build, logging."""

from __future__ import annotations

import inspect
import logging
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import executor as ex
from alpaca.trading.enums import OrderClass, OrderSide
from trading_agent.risk_manager import AccountState, OpenPosition, OrderLeg, ProposedOrder

PUT_SHORT = "SPY260901P00762000"
PUT_LONG = "SPY260901P00758000"
CALL_SHORT = "SPY260901C00770000"
CALL_LONG = "SPY260901C00772000"

CONDOR = (
    OrderLeg("sell", "put", 3, PUT_SHORT),
    OrderLeg("buy", "put", 3, PUT_LONG),
    OrderLeg("sell", "call", 3, CALL_SHORT),
    OrderLeg("buy", "call", 3, CALL_LONG),
)


class FakeTradingClient:
    """Records submissions; optionally raises to simulate an API failure."""

    def __init__(self, *, raise_exc: Exception | None = None):
        self.submitted: list = []
        self.raise_exc = raise_exc

    def submit_order(self, order_data):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.submitted.append(order_data)
        return SimpleNamespace(id="ord_123", status="accepted", legs=order_data.legs)


def order(wing=4.0, credit=1.2, qty=3, legs=CONDOR) -> ProposedOrder:
    # per-contract risk = (4.0 - 1.2) * 100 = $280;  * 3 = $840  (< $1,500 cap)
    return ProposedOrder(wing, credit, qty, legs)


def account(current=100_000.0, *, starting=100_000.0, day_start=None, positions=(), halted=False):
    return AccountState(
        starting_equity=starting,
        current_equity=current,
        day_start_equity=current if day_start is None else day_start,
        open_positions=positions,
        trading_halted=halted,
    )


def _pos():
    return OpenPosition("SPY_OPEN", date(2027, 1, 15), 1, ())


# --------------------------------------------------------------------------- #
# Blocked orders are never sent
# --------------------------------------------------------------------------- #
def test_blocked_order_is_not_submitted() -> None:
    fake = FakeTradingClient()
    res = ex.submit_iron_condor(order(), account(positions=(_pos(), _pos(), _pos())), client=fake)
    assert res.submitted is False
    assert res.order is None and res.order_id is None
    assert res.decision.approved is False
    assert fake.submitted == []  # broker never touched


def test_blocked_by_sticky_halt_is_not_submitted() -> None:
    fake = FakeTradingClient()
    res = ex.submit_iron_condor(order(), account(halted=True), client=fake)
    assert res.submitted is False
    assert fake.submitted == []


def test_blocked_order_logs_full_reasoning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="executor"):
        ex.submit_iron_condor(order(), account(positions=(_pos(), _pos(), _pos())), client=FakeTradingClient())
    assert "ORDER BLOCKED" in caplog.text
    # the full RiskDecision.describe() is in the log
    assert "REJECTED" in caplog.text
    assert "max_concurrent_positions" in caplog.text
    assert "open positions >= max 3" in caplog.text


# --------------------------------------------------------------------------- #
# Approved orders are submitted as one MLEG limit order
# --------------------------------------------------------------------------- #
def test_approved_order_is_submitted_as_mleg() -> None:
    fake = FakeTradingClient()
    res = ex.submit_iron_condor(order(), account(), client=fake)

    assert res.submitted is True
    assert res.order_id == "ord_123"
    assert len(fake.submitted) == 1

    req = fake.submitted[0]
    assert req.order_class == OrderClass.MLEG
    assert req.qty == 3
    assert round(req.limit_price, 2) == 1.2
    assert [leg.symbol for leg in req.legs] == [PUT_SHORT, PUT_LONG, CALL_SHORT, CALL_LONG]
    assert [leg.side for leg in req.legs] == [
        OrderSide.SELL, OrderSide.BUY, OrderSide.SELL, OrderSide.BUY,
    ]
    assert all(leg.ratio_qty == 1 for leg in req.legs)


def test_approved_order_logs_reasoning_and_submission(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="executor"):
        ex.submit_iron_condor(order(), account(), client=FakeTradingClient())
    assert "ORDER APPROVED" in caplog.text
    assert "APPROVED" in caplog.text and "defined_risk" in caplog.text  # describe() body
    assert "ORDER SUBMITTED" in caplog.text
    assert "id=ord_123" in caplog.text


def test_submission_api_failure_is_reported_not_raised() -> None:
    fake = FakeTradingClient(raise_exc=RuntimeError("alpaca 403 forbidden"))
    res = ex.submit_iron_condor(order(), account(), client=fake)
    assert res.submitted is False
    assert res.decision.approved is True          # gate said yes
    assert "alpaca 403 forbidden" in res.error
    assert res.submitted_request is not None       # we did build it


def test_approved_but_unbuildable_order_is_not_submitted() -> None:
    # legs pass the defined-risk gate (qty matches per right) but carry no symbols
    no_symbols = (
        OrderLeg("sell", "put", 3), OrderLeg("buy", "put", 3),
        OrderLeg("sell", "call", 3), OrderLeg("buy", "call", 3),
    )
    fake = FakeTradingClient()
    res = ex.submit_iron_condor(order(legs=no_symbols), account(), client=fake)
    assert res.decision.approved is True
    assert res.submitted is False
    assert "no OCC symbol" in res.error
    assert fake.submitted == []


# --------------------------------------------------------------------------- #
# No bypass
# --------------------------------------------------------------------------- #
def test_submit_has_no_gate_bypass_parameter() -> None:
    params = set(inspect.signature(ex.submit_iron_condor).parameters)
    for banned in ("force", "skip_checks", "skip_risk", "bypass", "no_check", "override"):
        assert banned not in params
    assert params == {"order", "account", "client", "creds"}


def test_check_order_is_always_invoked(monkeypatch) -> None:
    calls = []
    real = ex.check_order

    def spy(order_, account_):
        calls.append((order_, account_))
        return real(order_, account_)

    monkeypatch.setattr(ex, "check_order", spy)
    ex.submit_iron_condor(order(), account(), client=FakeTradingClient())
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# MLEG request builder
# --------------------------------------------------------------------------- #
def test_build_mleg_request_rejects_non_four_leg_order() -> None:
    with pytest.raises(ValueError):
        ex._build_mleg_request(ProposedOrder(4.0, 1.2, 1, CONDOR[:3]))


def test_build_mleg_request_rejects_zero_quantity() -> None:
    with pytest.raises(ValueError):
        ex._build_mleg_request(ProposedOrder(4.0, 1.2, 0, CONDOR))


def test_build_mleg_request_uses_absolute_net_credit_rounded() -> None:
    req = ex._build_mleg_request(ProposedOrder(4.0, 1.234, 2, CONDOR))
    assert req.limit_price == 1.23


# --------------------------------------------------------------------------- #
# IronCondorPlan -> ProposedOrder
# --------------------------------------------------------------------------- #
def _plan(*, legs=True, size=3, eligible=True):
    from trading_agent.alpaca_trader import OptionContract
    from trading_agent.strategy import CondorLeg, IronCondorPlan

    def contract(symbol, strike, right):
        return OptionContract(
            symbol=symbol, underlying="SPY", expiry=date(2026, 9, 1), right=right,
            strike=strike, bid=1.0, ask=1.1, bid_size=1, ask_size=1, mid=1.05,
            spread=0.1, spread_pct=9.5, delta=0.2, abs_delta=0.2, implied_volatility=0.2,
        )

    plan_legs = (
        [
            CondorLeg("sell", "put", contract(PUT_SHORT, 762.0, "put")),
            CondorLeg("buy", "put", contract(PUT_LONG, 758.0, "put")),
            CondorLeg("sell", "call", contract(CALL_SHORT, 770.0, "call")),
            CondorLeg("buy", "call", contract(CALL_LONG, 772.0, "call")),
        ]
        if legs
        else []
    )
    return IronCondorPlan(
        eligible=eligible,
        reason="ok" if eligible else "credit/width 17.8% below 25% target",
        expiry=date(2026, 9, 1), legs=plan_legs,
        net_credit=1.2, wing_width=4.0, credit_to_width=0.3,
        max_loss_per_contract=280.0, suggested_contracts=size,
    )


def test_from_iron_condor_plan_maps_symbols_size_and_sides() -> None:
    po = ex.from_iron_condor_plan(_plan(size=3))
    assert po.quantity == 3
    assert po.wing_width == 4.0 and po.net_credit == 1.2
    assert [(leg.action, leg.right, leg.symbol, leg.quantity) for leg in po.legs] == [
        ("sell", "put", PUT_SHORT, 3),
        ("buy", "put", PUT_LONG, 3),
        ("sell", "call", CALL_SHORT, 3),
        ("buy", "call", CALL_LONG, 3),
    ]


def test_from_iron_condor_plan_round_trips_through_submit() -> None:
    fake = FakeTradingClient()
    po = ex.from_iron_condor_plan(_plan(size=3))
    res = ex.submit_iron_condor(po, account(), client=fake)
    assert res.submitted is True
    assert [leg.symbol for leg in fake.submitted[0].legs] == [
        PUT_SHORT, PUT_LONG, CALL_SHORT, CALL_LONG,
    ]


@pytest.mark.parametrize("kw", [{"legs": False}, {"size": 0}, {"size": None}])
def test_from_iron_condor_plan_rejects_incomplete_plan(kw) -> None:
    with pytest.raises(ValueError):
        ex.from_iron_condor_plan(_plan(**kw))


def test_from_iron_condor_plan_rejects_ineligible_plan_even_with_legs_and_size() -> None:
    # strategy-rejected plan still carries legs + sizing (e.g. credit/width block);
    # it must never become an order regardless of that.
    bad = _plan(eligible=False, legs=True, size=3)
    assert bad.legs and bad.suggested_contracts  # would have passed the old check
    with pytest.raises(ValueError, match="did not approve"):
        ex.from_iron_condor_plan(bad)


# --------------------------------------------------------------------------- #
# Multi-structure support: 2-leg MLEG + from_plan + max_loss passthrough
# --------------------------------------------------------------------------- #
def _strangle_plan(*, size=2, debit_per_contract=210.0):
    from trading_agent.alpaca_trader import OptionContract
    from trading_agent.strategy import CondorLeg, IronCondorPlan

    def contract(symbol, strike, right, mid):
        return OptionContract(
            symbol=symbol, underlying="QQQ", expiry=date(2026, 9, 1), right=right,
            strike=strike, bid=mid - 0.05, ask=mid + 0.05, bid_size=1, ask_size=1,
            mid=mid, spread=0.1, spread_pct=5.0, delta=0.25, abs_delta=0.25,
            implied_volatility=0.2,
        )

    debit = debit_per_contract / 100.0
    return IronCondorPlan(
        eligible=True, reason="long strangle test",
        expiry=date(2026, 9, 1),
        legs=[
            CondorLeg("buy", "put", contract("QQQ...P", 480.0, "put", debit / 2)),
            CondorLeg("buy", "call", contract("QQQ...C", 500.0, "call", debit / 2)),
        ],
        net_credit=-debit, wing_width=0.0, credit_to_width=None,
        max_loss_per_contract=debit_per_contract, suggested_contracts=size,
        structure="long_strangle", symbol="QQQ",
    )


def test_from_plan_is_the_alias_for_from_iron_condor_plan() -> None:
    assert ex.from_plan is ex.from_iron_condor_plan


def test_from_plan_carries_max_loss_for_a_debit_structure() -> None:
    po = ex.from_plan(_strangle_plan(size=2, debit_per_contract=210.0))
    assert po.quantity == 2
    assert po.net_credit < 0                    # net debit
    assert po.max_loss == 210.0
    assert po.risk_dollars == pytest.approx(420.0)
    assert [(lg.action, lg.right) for lg in po.legs] == [("buy", "put"), ("buy", "call")]


def test_two_leg_strangle_builds_a_valid_mleg_request() -> None:
    po = ex.from_plan(_strangle_plan(size=2, debit_per_contract=210.0))
    req = ex._build_mleg_request(po)
    assert req.order_class == OrderClass.MLEG
    assert len(req.legs) == 2
    assert [leg.side for leg in req.legs] == [OrderSide.BUY, OrderSide.BUY]
    assert req.limit_price == pytest.approx(2.10)   # abs(net debit)


def test_build_mleg_request_still_rejects_three_leg_orders() -> None:
    with pytest.raises(ValueError):
        ex._build_mleg_request(ProposedOrder(4.0, 1.2, 1, CONDOR[:3]))


def test_strangle_round_trips_through_submit() -> None:
    fake = FakeTradingClient()
    po = ex.from_plan(_strangle_plan(size=2))
    res = ex.submit_iron_condor(po, account(), client=fake)
    assert res.submitted is True
    assert len(fake.submitted[0].legs) == 2
