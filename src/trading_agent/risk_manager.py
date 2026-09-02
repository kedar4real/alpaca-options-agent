"""
risk_manager.py — Pre-trade risk gates + open-position monitoring for the SPY
options agent.

Six hard limits — gates 1-5 run on a proposed order *before* it is sent, gate 6
runs over the open positions:

  1. Max risk per trade   <= 1.5% of *current* equity
                             risk = (wing_width - net_credit) * 100 * quantity
  2. Daily loss halt       >= 2.5% of *starting* equity  -> no new trades today
  3. Total drawdown floor  >= 5%  of *starting* equity  -> halt for the comp
  4. Max concurrent positions = 3
  4b. Correlation guard: a >0.8 (10-day) correlated cluster of basket tickers
      counts as ONE slot toward the cap (optional; needs IntelligenceHub data)
  5. Defined-risk invariant: per right, long-leg contracts == short-leg contracts
  6. Expiration auto-close: flag positions within 1 trading day of expiry

Pure and deterministic: no network, no wall clock. Pass ``today`` for the date
logic. This module *decides*; it never places or cancels orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .alpaca_trader import trading_sessions

# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
MAX_RISK_PER_TRADE_PCT = 0.02      # 2.0% of current equity — the absolute cap
DAILY_LOSS_HALT_PCT = 0.035        # 3.5% of starting equity
TOTAL_DRAWDOWN_FLOOR_PCT = 0.05    # 5% of starting equity
MAX_CONCURRENT_POSITIONS = 4
EXPIRY_CLOSE_TRADING_DAYS = 1      # flag when this close to expiry
CONTRACT_MULTIPLIER = 100

# Macro guard: on a High-Impact macro day (FOMC / CPI / NFP) the caller sets
# AccountState.risk_multiplier to this, so gate 1's *effective* cap is halved for
# that cycle. It can only tighten — a multiplier is clamped to <= 1.0 in
# check_order — so the 1.5% line above stays the source of truth.
MACRO_RISK_REDUCTION = 0.5


def is_macro_safe(*, macro_high_impact: bool) -> bool:
    """False when a High-Impact macro event lands today. The caller then applies
    :data:`MACRO_RISK_REDUCTION` to ``AccountState.risk_multiplier``."""
    return not macro_high_impact


def macro_risk_multiplier(*, macro_high_impact: bool) -> float:
    """1.0 normally; :data:`MACRO_RISK_REDUCTION` on a High-Impact macro day."""
    return MACRO_RISK_REDUCTION if macro_high_impact else 1.0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrderLeg:
    action: str                  # "buy" | "sell"
    right: str                   # "put" | "call"
    quantity: int                # absolute contract count for this leg
    symbol: str | None = None    # OCC option symbol (needed by executor.py)


@dataclass(frozen=True)
class ProposedOrder:
    wing_width: float                 # $, the wider wing (drives max loss)
    net_credit: float                 # $ per spread (mid-price estimate; negative = net debit)
    quantity: int                     # number of spreads / condors
    legs: tuple[OrderLeg, ...] = ()
    # Per-CONTRACT worst-case loss in $ (already x100). Set by debit structures
    # (e.g. a long strangle) where "(wing_width - net_credit)" does not express
    # risk. When None, the classic credit-spread formula below is used. This does
    # not change any limit — gate 1 still caps risk at 1.5% of current equity.
    max_loss: float | None = None
    underlying: str | None = None     # basket ticker (for the correlation guard)

    @property
    def risk_dollars(self) -> float:
        if self.max_loss is not None:
            return self.max_loss * self.quantity
        return (self.wing_width - self.net_credit) * CONTRACT_MULTIPLIER * self.quantity


@dataclass(frozen=True)
class OpenPosition:
    symbol: str                       # caller's tracking id (round-tripped as-is)
    expiry: date
    quantity: int
    legs: tuple[OrderLeg, ...] = ()
    underlying: str | None = None     # basket ticker (for the correlation guard)


@dataclass(frozen=True)
class AccountState:
    starting_equity: float            # equity at the start of the competition
    current_equity: float             # equity right now
    day_start_equity: float           # equity at the start of today's session
    open_positions: tuple[OpenPosition, ...] = ()
    trading_halted: bool = False      # sticky comp-level halt (persisted by caller)
    risk_multiplier: float = 1.0      # macro guard: <1.0 tightens gate 1 for a cycle


@dataclass
class RiskDecision:
    approved: bool
    blocks: list[str] = field(default_factory=list)      # every failed gate
    checks: dict[str, bool] = field(default_factory=dict)  # gate -> passed?
    # computed numbers, for logging / display
    order_risk: float | None = None
    max_risk_allowed: float | None = None
    daily_loss: float | None = None
    daily_loss_limit: float | None = None
    total_drawdown: float | None = None
    drawdown_limit: float | None = None
    open_position_count: int | None = None

    def describe(self) -> str:
        head = "APPROVED" if self.approved else "REJECTED"
        lines = [f"{head}"]
        for gate, ok in self.checks.items():
            lines.append(f"  [{'ok ' if ok else 'FAIL'}] {gate}")
        for msg in self.blocks:
            lines.append(f"  - {msg}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ExpiringPosition:
    position: OpenPosition
    trading_days_to_expiry: int


# --------------------------------------------------------------------------- #
# Gate 5 — defined-risk invariant
# --------------------------------------------------------------------------- #
def is_defined_risk(legs: tuple[OrderLeg, ...]) -> bool:
    """True when the position cannot lose more than a known, bounded amount.

    Two shapes qualify:

    * **All-long** — every leg is ``buy`` (e.g. a long strangle / straddle /
      reverse spread). The most that can be lost is the premium paid, so it is
      inherently defined-risk and never naked short. A single ``sell`` leg drops
      out of this branch and must satisfy the matched-legs rule below.
    * **Matched legs** — for every option right present, total bought contracts
      equal total sold contracts, so every short leg is covered (iron condor,
      vertical credit spread).

    This does not loosen anything for spreads: any position containing a short
    leg still goes through the unchanged matched-legs check.
    """
    if not legs:
        return False

    if all(leg.action == "buy" and leg.quantity > 0 for leg in legs):
        return True

    tally: dict[str, list[int]] = {}  # right -> [long_qty, short_qty]
    for leg in legs:
        if leg.action not in ("buy", "sell") or leg.quantity <= 0:
            return False
        pair = tally.setdefault(leg.right, [0, 0])
        pair[0 if leg.action == "buy" else 1] += leg.quantity
    return all(long_q == short_q and long_q > 0 for long_q, short_q in tally.values())


# --------------------------------------------------------------------------- #
# Gates 1-5 — pre-trade order check
# --------------------------------------------------------------------------- #
def check_order(
    order: ProposedOrder,
    account: AccountState,
    *,
    correlation_clusters: tuple[frozenset[str], ...] = (),
) -> RiskDecision:
    """Run gates 1-5 against a proposed order. Collects *every* failed gate.

    ``correlation_clusters`` (from the IntelligenceHub, optional) are groups of
    basket tickers whose 10-day returns are correlated > 0.8. When the proposed
    order's ``underlying`` is in a cluster that already holds an open position,
    gate 4b blocks it — three condors on SPY/QQQ/IWM are one leveraged bet on
    equity risk, not a diversified book. Absent clusters, behaviour is unchanged.
    """
    blocks: list[str] = []
    checks: dict[str, bool] = {}

    # (5) defined-risk invariant
    ok = is_defined_risk(order.legs)
    checks["defined_risk"] = ok
    if not ok:
        blocks.append(
            "not defined-risk: long/short leg contracts do not match per right"
        )

    # (3) total drawdown floor / sticky competition halt
    total_drawdown = account.starting_equity - account.current_equity
    drawdown_limit = TOTAL_DRAWDOWN_FLOOR_PCT * account.starting_equity
    ok = (not account.trading_halted) and total_drawdown < drawdown_limit
    checks["total_drawdown_floor"] = ok
    if account.trading_halted:
        blocks.append("trading halted for the competition (drawdown floor already breached)")
    elif total_drawdown >= drawdown_limit:
        blocks.append(
            f"total drawdown ${total_drawdown:,.0f} >= ${drawdown_limit:,.0f} "
            f"(5% of starting equity): halt all trading"
        )

    # (2) daily loss halt
    daily_loss = account.day_start_equity - account.current_equity
    daily_loss_limit = DAILY_LOSS_HALT_PCT * account.starting_equity
    ok = daily_loss < daily_loss_limit
    checks["daily_loss_halt"] = ok
    if not ok:
        blocks.append(
            f"daily loss ${daily_loss:,.0f} >= ${daily_loss_limit:,.0f} "
            f"(2.5% of starting equity): no new trades today"
        )

    # (4) max concurrent positions
    n_open = len(account.open_positions)
    ok = n_open < MAX_CONCURRENT_POSITIONS
    checks["max_concurrent_positions"] = ok
    if not ok:
        blocks.append(
            f"{n_open} open positions >= max {MAX_CONCURRENT_POSITIONS} concurrent"
        )

    # (4b) correlation guard — a >0.8 (10-day) correlated cluster gets ONE slot
    #      toward the cap; a second name in an already-occupied cluster is blocked.
    new_sym = order.underlying
    if correlation_clusters and new_sym:
        open_underlyings = {
            p.underlying for p in account.open_positions if p.underlying
        }
        ok = True
        for cluster in correlation_clusters:
            if new_sym in cluster and len(cluster) >= 2:
                clash = sorted(open_underlyings & (cluster - {new_sym}))
                if clash:
                    ok = False
                    blocks.append(
                        f"correlation guard: {new_sym} is >0.8 correlated (10d) with "
                        f"open {', '.join(clash)} — that cluster already holds its "
                        f"one slot toward the {MAX_CONCURRENT_POSITIONS}-position cap"
                    )
                break
        checks["correlation_guard"] = ok

    # (1) max risk per trade — the 1.5% cap. A macro-guard multiplier can only
    #     tighten it (clamped to <= 1.0); it can never raise the ceiling.
    order_risk = order.risk_dollars
    macro_mult = min(account.risk_multiplier, 1.0)
    max_risk_allowed = MAX_RISK_PER_TRADE_PCT * account.current_equity * macro_mult
    ok = order_risk <= max_risk_allowed
    checks["max_risk_per_trade"] = ok
    if not ok:
        note = "1.5% of current equity" if macro_mult == 1.0 else (
            f"1.5% of current equity x {macro_mult:.2f} — macro guard"
        )
        blocks.append(
            f"trade risk ${order_risk:,.0f} > ${max_risk_allowed:,.0f} ({note})"
        )

    return RiskDecision(
        approved=not blocks,
        blocks=blocks,
        checks=checks,
        order_risk=order_risk,
        max_risk_allowed=max_risk_allowed,
        daily_loss=daily_loss,
        daily_loss_limit=daily_loss_limit,
        total_drawdown=total_drawdown,
        drawdown_limit=drawdown_limit,
        open_position_count=n_open,
    )


# --------------------------------------------------------------------------- #
# Gate 6 — expiration auto-close
# --------------------------------------------------------------------------- #
def trading_days_until(target: date, *, today: date | None = None) -> int:
    """NYSE sessions strictly after ``today`` up to and including ``target``.

    ``0`` if ``target`` is on or before ``today`` (already at/past expiry).
    """
    start = today or date.today()
    if target <= start:
        return 0
    return len(trading_sessions(start + timedelta(days=1), target))


def flag_expiring_positions(
    positions: tuple[OpenPosition, ...],
    *,
    today: date | None = None,
    within_trading_days: int = EXPIRY_CLOSE_TRADING_DAYS,
) -> list[ExpiringPosition]:
    """Positions ``within_trading_days`` (or fewer) trading days from expiry —
    i.e. that must be force-closed rather than held into expiration."""
    flagged: list[ExpiringPosition] = []
    for position in positions:
        dte = trading_days_until(position.expiry, today=today)
        if dte <= within_trading_days:
            flagged.append(ExpiringPosition(position, dte))
    return flagged


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    condor = tuple(
        OrderLeg(a, r, 5)
        for a, r in (("buy", "put"), ("sell", "put"), ("sell", "call"), ("buy", "call"))
    )
    order = ProposedOrder(wing_width=5.0, net_credit=2.0, quantity=4, legs=condor)
    account = AccountState(
        starting_equity=100_000.0,
        current_equity=98_800.0,
        day_start_equity=99_400.0,
        open_positions=(),
    )
    decision = check_order(order, account)
    print(decision.describe())
    print(f"\norder risk ${decision.order_risk:,.0f} / cap ${decision.max_risk_allowed:,.0f}")

    expiring = flag_expiring_positions(
        (OpenPosition("SPY 09/08 condor", date(2026, 9, 8), 4, condor),),
        today=date(2026, 9, 4),
    )
    for flag in expiring:
        print(
            f"force-close: {flag.position.symbol} "
            f"({flag.trading_days_to_expiry} trading day(s) to expiry)"
        )
