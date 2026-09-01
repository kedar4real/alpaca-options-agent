"""
executor.py — Submit a risk-approved iron condor to Alpaca as ONE multi-leg
(``OrderClass.MLEG``) limit order.

Hard rule: every submission runs through ``risk_manager.check_order()`` first.
If the resulting ``RiskDecision`` is not approved, nothing is sent. There is no
bypass parameter and no code path around the gates. Every attempt — approved or
blocked — is logged with the full ``RiskDecision.describe()``.

This module talks to the broker. It does not decide *what* to trade (that's
``strategy.py``) or *whether* it's allowed (that's ``risk_manager.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from .alpaca_trader import AlpacaCredentials, load_credentials
from .risk_manager import AccountState, OrderLeg, ProposedOrder, RiskDecision, check_order

if TYPE_CHECKING:  # avoid a runtime import cycle; only needed for type hints
    from .strategy import IronCondorPlan

log = logging.getLogger("executor")

TIME_IN_FORCE = TimeInForce.DAY
_SIDE = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass
class ExecutionResult:
    submitted: bool
    decision: RiskDecision
    order: object | None = None                 # alpaca-py Order (or raw dict) on success
    submitted_request: LimitOrderRequest | None = None
    error: str | None = None                    # validation / API error, if any

    @property
    def order_id(self):
        return getattr(self.order, "id", None)


# --------------------------------------------------------------------------- #
# Plan -> order
# --------------------------------------------------------------------------- #
def from_iron_condor_plan(plan: "IronCondorPlan") -> ProposedOrder:
    """Translate a ``strategy.IronCondorPlan`` into a ``risk_manager.ProposedOrder``
    carrying the four OCC option symbols and the suggested contract count.

    Raises ``ValueError`` if the plan is not eligible (strategy rejected it) or
    has no legs / no sizing. A strategy-rejected plan is never convertible into
    an order, regardless of caller — there is no override.
    """
    if not plan.eligible:
        raise ValueError(
            f"strategy did not approve this plan; it cannot become an order: {plan.reason}"
        )
    if not plan.legs or not plan.suggested_contracts:
        raise ValueError("plan has no legs / no suggested_contracts; nothing to submit")

    qty = int(plan.suggested_contracts)
    legs = tuple(
        OrderLeg(
            action=leg.action,
            right=leg.right,
            quantity=qty,
            symbol=leg.contract.symbol,
        )
        for leg in plan.legs
    )
    return ProposedOrder(
        wing_width=plan.wing_width or 0.0,
        net_credit=plan.net_credit,
        quantity=qty,
        legs=legs,
        # The strategy already computed the true per-contract worst case; hand it
        # to risk_manager directly so gate 1 is correct for debit structures too.
        max_loss=plan.max_loss_per_contract,
        underlying=getattr(plan, "symbol", None),
    )


# Regime-aware alias — any structure the switch builds (condor, strangle,
# vertical) is an IronCondorPlan and converts the same way.
from_plan = from_iron_condor_plan


def _build_mleg_request(order: ProposedOrder) -> LimitOrderRequest:
    """Turn a 2- or 4-leg ``ProposedOrder`` into an Alpaca MLEG limit order.

    2 legs = a vertical spread or a long strangle; 4 legs = an iron condor.
    """
    if len(order.legs) not in (2, 4):
        raise ValueError(f"expected a 2- or 4-leg order, got {len(order.legs)}")
    if order.quantity < 1:
        raise ValueError(f"order quantity must be >= 1, got {order.quantity}")

    option_legs = []
    for leg in order.legs:
        if not leg.symbol:
            raise ValueError(f"leg {leg.action} {leg.right} has no OCC symbol")
        if leg.action not in _SIDE:
            raise ValueError(f"leg has invalid action: {leg.action!r}")
        option_legs.append(
            OptionLegRequest(symbol=leg.symbol, side=_SIDE[leg.action], ratio_qty=1)
        )

    # For an MLEG order the limit price is the positive net premium; Alpaca infers
    # credit vs debit from the legs. Options quote in $0.01 increments.
    limit_price = round(abs(order.net_credit), 2)

    return LimitOrderRequest(
        qty=order.quantity,
        order_class=OrderClass.MLEG,
        time_in_force=TIME_IN_FORCE,
        limit_price=limit_price,
        legs=option_legs,
    )


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #
def _trading_client(creds: AlpacaCredentials) -> TradingClient:
    return TradingClient(creds.api_key, creds.secret_key, paper=creds.paper)


def submit_iron_condor(
    order: ProposedOrder,
    account: AccountState,
    *,
    client: TradingClient | None = None,
    creds: AlpacaCredentials | None = None,
) -> ExecutionResult:
    """Risk-check ``order`` and, only if approved, submit it to Alpaca as one
    MLEG limit order.

    Always returns an ``ExecutionResult``. Never submits an order that
    ``risk_manager.check_order()`` did not approve — there is deliberately no
    argument that skips this.
    """
    decision = check_order(order, account)
    summary = decision.describe()

    if not decision.approved:
        log.warning("ORDER BLOCKED — not submitting\n%s", summary)
        return ExecutionResult(submitted=False, decision=decision)

    log.info("ORDER APPROVED — building MLEG submission\n%s", summary)

    try:
        request = _build_mleg_request(order)
    except ValueError as exc:
        log.error("ORDER APPROVED but could not be built: %s", exc)
        return ExecutionResult(submitted=False, decision=decision, error=str(exc))

    trading = client or _trading_client(creds or load_credentials())
    try:
        confirmation = trading.submit_order(request)
    except Exception as exc:  # noqa: BLE001 - surface any broker/API failure
        log.error("ORDER APPROVED and built but submission failed: %s", exc)
        return ExecutionResult(
            submitted=False, decision=decision, submitted_request=request, error=str(exc)
        )

    log.info(
        "ORDER SUBMITTED — id=%s status=%s qty=%s limit=%s",
        getattr(confirmation, "id", "?"),
        getattr(confirmation, "status", "?"),
        request.qty,
        request.limit_price,
    )
    return ExecutionResult(
        submitted=True,
        decision=decision,
        order=confirmation,
        submitted_request=request,
    )


# --------------------------------------------------------------------------- #
# Demo (no network, never submits)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    condor = (
        OrderLeg("sell", "put", 4, "SPY260901P00762000"),
        OrderLeg("buy", "put", 4, "SPY260901P00758000"),
        OrderLeg("sell", "call", 4, "SPY260901C00770000"),
        OrderLeg("buy", "call", 4, "SPY260901C00772000"),
    )
    healthy = AccountState(100_000.0, 99_000.0, 99_400.0, open_positions=())

    # 1) risk cap busted -> the gate blocks it (nothing would be sent)
    print("\n--- oversized order (40 contracts) ---")
    print(check_order(ProposedOrder(4.0, 1.0, 40, condor), healthy).describe())

    # 2) sane order -> the gate approves, and here is the MLEG request that
    #    submit_iron_condor() would hand to Alpaca:
    print("\n--- sane order (4 contracts) ---")
    ok = ProposedOrder(4.0, 1.2, 4, condor)
    print(check_order(ok, healthy).describe())
    print(_build_mleg_request(ok).model_dump(exclude_none=True))
