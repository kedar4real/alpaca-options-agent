"""
strategy.py — Iron condor construction for the SPY adaptive options agent.

Pipeline
--------
1. Pull a market snapshot from ``data.py``.
2. IV-regime gate: use the IV percentile once >= 10 days of history exist,
   otherwise fall back to Hackathon Mode (static threshold, ATM IV > 15%).
3. Pick the nearest listed expiry 1-3 trading days out (via ``nth_trading_day``).
4. Short legs at ~0.20-0.25 delta (target 0.225).
5. Long legs at ~0.10 delta, else $5 further OTM than the matching short.
6. Require net credit >= 25% of the wing width.
7. Size the position so max loss <= 1.5% of equity ($1,500) — CLAUDE.md hard rule.
   The canonical fraction lives in ``risk_manager.MAX_RISK_PER_TRADE_PCT``;
   ``risk_manager.check_order()`` re-checks it against *live* equity before send.

This module *proposes* a defined-risk iron condor. It does NOT place orders.

Run directly::

    python strategy.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .alpaca_trader import OptionContract, build_contracts, nth_trading_day
from .data import get_market_snapshot
from .risk_manager import MAX_RISK_PER_TRADE_PCT

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
DTE_MIN_TRADING_DAYS = 1
DTE_MAX_TRADING_DAYS = 3

SHORT_DELTA_TARGET = 0.225
SHORT_DELTA_MIN = 0.20
SHORT_DELTA_MAX = 0.25

LONG_DELTA_TARGET = 0.10
LONG_DELTA_TOLERANCE = 0.05   # accept 0.05-0.15 for the long leg's delta
LONG_OTM_OFFSET = 5.0         # $ wing width when no ~0.10-delta strike is available

MIN_CREDIT_TO_WIDTH = 0.25    # target net credit >= 25% of the wing width

# IV must sit at least this far (annualized vol points) above 10-day realized vol,
# i.e. options are pricing in more movement than the underlying has actually made.
# None in the snapshot (not enough price history) -> the check is skipped.
MIN_IV_RV_SPREAD = 0.02

# Sizing budget for the proposal: 1.5% of the nominal $100k paper account.
# MAX_RISK_PER_TRADE_PCT is the single source of truth (shared with risk_manager),
# so this figure can't drift from the pre-trade gate.
NOMINAL_EQUITY = 100_000.0
MAX_RISK_PER_TRADE = MAX_RISK_PER_TRADE_PCT * NOMINAL_EQUITY  # $1,500
CONTRACT_MULTIPLIER = 100


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CondorLeg:
    action: str  # "sell" | "buy"
    right: str   # "call" | "put"
    contract: OptionContract


@dataclass
class IronCondorPlan:
    eligible: bool
    reason: str
    expiry: date | None = None
    underlying_price: float | None = None
    iv_regime_mode: str | None = None
    iv_rv_spread: float | None = None        # atm_iv - realized_vol (from the snapshot)
    legs: list[CondorLeg] = field(default_factory=list)
    net_credit: float | None = None          # per spread, in $ (mid-price estimate)
    wing_width: float | None = None          # $, the wider of the two wings
    credit_to_width: float | None = None     # net_credit / wing_width
    max_loss_per_contract: float | None = None  # $ = (width - credit) * 100
    suggested_contracts: int | None = None   # within MAX_RISK_PER_TRADE

    def describe(self) -> str:
        lines = [
            f"eligible:      {self.eligible}",
            f"reason:        {self.reason}",
            f"IV mode:       {self.iv_regime_mode}",
        ]
        if self.underlying_price is not None:
            lines.append(f"underlying:    ${self.underlying_price:.2f}")
        if self.iv_rv_spread is not None:
            lines.append(f"IV - RV:       {self.iv_rv_spread:+.4f}  (min {MIN_IV_RV_SPREAD:+.4f})")
        if self.expiry is not None:
            lines.append(f"expiry:        {self.expiry.isoformat()}")
        for leg in self.legs:
            c = leg.contract
            lines.append(
                f"  {leg.action:<4} {leg.right:<4} {c.strike:>8.2f}  "
                f"d{c.abs_delta:0.3f}  bid {c.bid:6.2f}  ask {c.ask:6.2f}  mid {c.mid:6.2f}"
            )
        if self.net_credit is not None and self.wing_width is not None:
            lines.append(
                f"net credit:    {self.net_credit:.2f}  "
                f"({self.credit_to_width:.1%} of {self.wing_width:.2f} wing; "
                f"target {MIN_CREDIT_TO_WIDTH:.0%})"
            )
        if self.max_loss_per_contract is not None:
            lines.append(
                f"max loss:      ${self.max_loss_per_contract:.0f}/contract  ->  "
                f"{self.suggested_contracts} contract(s) within ${MAX_RISK_PER_TRADE:.0f} cap"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Leg selection
# --------------------------------------------------------------------------- #
def pick_expiry(
    contracts: list[OptionContract],
    *,
    dte_min: int = DTE_MIN_TRADING_DAYS,
    dte_max: int = DTE_MAX_TRADING_DAYS,
    today: date | None = None,
) -> date | None:
    """Earliest expiry present in ``contracts`` within the DTE trading-day band."""
    lo = nth_trading_day(dte_min, today)
    hi = nth_trading_day(dte_max, today)
    listed = sorted({c.expiry for c in contracts if lo <= c.expiry <= hi})
    return listed[0] if listed else None


def select_short_leg(legs: list[OptionContract]) -> OptionContract | None:
    """Contract nearest SHORT_DELTA_TARGET, preferring the 0.20-0.25 band."""
    graded = [c for c in legs if c.abs_delta is not None]
    if not graded:
        return None
    band = [c for c in graded if SHORT_DELTA_MIN <= c.abs_delta <= SHORT_DELTA_MAX]
    pool = band or graded
    return min(pool, key=lambda c: abs(c.abs_delta - SHORT_DELTA_TARGET))


def select_long_leg(
    legs: list[OptionContract],
    short_leg: OptionContract,
    right: str,
) -> tuple[OptionContract | None, str]:
    """Protective long: ~0.10 delta if available, else ~$5 further OTM.

    Returns ``(contract, rule)`` where ``rule`` is ``"delta"``, ``"otm-offset"``
    or ``"none-further-otm"``.
    """
    if right == "put":
        further = [c for c in legs if c.strike < short_leg.strike]
    else:
        further = [c for c in legs if c.strike > short_leg.strike]
    if not further:
        return None, "none-further-otm"

    graded = [c for c in further if c.abs_delta is not None]
    near_target = [
        c for c in graded if abs(c.abs_delta - LONG_DELTA_TARGET) <= LONG_DELTA_TOLERANCE
    ]
    if near_target:
        return min(near_target, key=lambda c: abs(c.abs_delta - LONG_DELTA_TARGET)), "delta"

    target_strike = (
        short_leg.strike - LONG_OTM_OFFSET
        if right == "put"
        else short_leg.strike + LONG_OTM_OFFSET
    )
    return min(further, key=lambda c: abs(c.strike - target_strike)), "otm-offset"


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def plan_iron_condor(
    contracts: list[OptionContract],
    *,
    underlying_price: float | None,
    iv_regime,
    iv_rv_spread: float | None = None,
    today: date | None = None,
) -> IronCondorPlan:
    """Build an iron condor proposal from a flat list of graded contracts.

    ``iv_rv_spread`` is ``atm_iv - realized_vol`` from the snapshot. When it is
    not ``None`` it must be >= ``MIN_IV_RV_SPREAD`` — IV has to be pricing in
    more movement than the underlying has actually made, not just be high on its
    own. ``None`` (thin price history) skips the check.
    """
    mode = getattr(iv_regime, "mode", None)

    def result(eligible: bool, reason: str, **kw) -> IronCondorPlan:
        return IronCondorPlan(
            eligible=eligible,
            reason=reason,
            underlying_price=underlying_price,
            iv_regime_mode=mode,
            iv_rv_spread=iv_rv_spread,
            **kw,
        )

    if not getattr(iv_regime, "trade_eligible", False):
        return result(False, f"IV gate blocked: {getattr(iv_regime, 'reason', 'not eligible')}")

    if iv_rv_spread is not None and iv_rv_spread < MIN_IV_RV_SPREAD:
        return result(
            False,
            f"IV-RV spread {iv_rv_spread:+.4f} below {MIN_IV_RV_SPREAD:+.4f} "
            f"(IV not richer than recent realized movement)",
        )

    expiry = pick_expiry(contracts, today=today)
    if expiry is None:
        return result(False, "no listed expiry in the 1-3 trading-day window")

    at_expiry = [c for c in contracts if c.expiry == expiry]
    puts = [c for c in at_expiry if c.right == "put"]
    calls = [c for c in at_expiry if c.right == "call"]

    short_put = select_short_leg(puts)
    short_call = select_short_leg(calls)
    if short_put is None or short_call is None:
        return result(
            False, "could not find short legs near 0.20-0.25 delta", expiry=expiry
        )

    long_put, put_rule = select_long_leg(puts, short_put, "put")
    long_call, call_rule = select_long_leg(calls, short_call, "call")
    if long_put is None or long_call is None:
        return result(
            False,
            f"no protective long legs (put:{put_rule}, call:{call_rule})",
            expiry=expiry,
        )

    legs = [
        CondorLeg("sell", "put", short_put),
        CondorLeg("buy", "put", long_put),
        CondorLeg("sell", "call", short_call),
        CondorLeg("buy", "call", long_call),
    ]

    # Net credit at mid-price; wing width is the wider side (drives max loss).
    net_credit = (short_put.mid + short_call.mid) - (long_put.mid + long_call.mid)
    put_width = short_put.strike - long_put.strike
    call_width = long_call.strike - short_call.strike
    wing_width = max(put_width, call_width)
    ctw = net_credit / wing_width if wing_width > 0 else 0.0
    max_loss = (wing_width - net_credit) * CONTRACT_MULTIPLIER
    contracts_n = int(MAX_RISK_PER_TRADE // max_loss) if max_loss > 0 else 0

    tag = (
        f"expiry {expiry.isoformat()}; longs {put_rule}/{call_rule}; "
        f"credit {net_credit:.2f} / width {wing_width:.2f} = {ctw:.1%}"
    )
    priced = dict(
        expiry=expiry,
        legs=legs,
        net_credit=net_credit,
        wing_width=wing_width,
        credit_to_width=ctw,
    )

    if net_credit <= 0:
        return result(False, f"net credit <= 0 ({tag})", **priced)

    if ctw < MIN_CREDIT_TO_WIDTH:
        return result(
            False,
            f"credit/width {ctw:.1%} below {MIN_CREDIT_TO_WIDTH:.0%} target ({tag})",
            max_loss_per_contract=max_loss,
            suggested_contracts=contracts_n,
            **priced,
        )

    if contracts_n < 1:
        return result(
            False,
            f"max loss ${max_loss:.0f}/contract exceeds ${MAX_RISK_PER_TRADE:.0f} risk cap ({tag})",
            max_loss_per_contract=max_loss,
            suggested_contracts=0,
            **priced,
        )

    return result(
        True,
        f"meets IV, IV-RV, credit, and risk criteria ({tag})",
        max_loss_per_contract=max_loss,
        suggested_contracts=contracts_n,
        **priced,
    )


def build_iron_condor(snapshot: dict | None = None, *, today: date | None = None) -> IronCondorPlan:
    """Fetch a market snapshot (if not supplied) and propose an iron condor."""
    snap = snapshot or get_market_snapshot()
    contracts = [c for c in build_contracts(snap["chain"]) if c.abs_delta is not None]
    return plan_iron_condor(
        contracts,
        underlying_price=snap.get("current_price"),
        iv_regime=snap["iv_regime"],
        iv_rv_spread=snap.get("iv_rv_spread"),
        today=today,
    )


if __name__ == "__main__":
    print(build_iron_condor().describe())
