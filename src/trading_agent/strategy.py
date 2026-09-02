"""
strategy.py — Iron condor construction for the SPY adaptive options agent.

Pipeline
--------
1. Pull a market snapshot from ``data.py``.
2. IV-regime gate: use the IV percentile once >= 10 days of history exist,
   otherwise fall back to Hackathon Mode (static threshold, ATM IV > 12%).
3. Pick the nearest listed expiry 1-3 trading days out (via ``nth_trading_day``).
4. Short legs at ~0.20-0.25 delta (target 0.225).
5. Long legs at ~0.10 delta, else $5 further OTM than the matching short.
6. Require net credit >= 20% of the wing width.
7. Size the position so max loss <= 1.5% of equity ($1,500) — CLAUDE.md hard rule.
   The canonical fraction lives in ``risk_manager.MAX_RISK_PER_TRADE_PCT``;
   ``risk_manager.check_order()`` re-checks it against *live* equity before send.

This module *proposes* a defined-risk iron condor. It does NOT place orders.

Run directly::

    python strategy.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .alpaca_trader import OptionContract, build_contracts, nth_trading_day
from .data import get_market_snapshot
from .risk_manager import MAX_RISK_PER_TRADE_PCT

log = logging.getLogger("strategy")

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
DTE_MIN_TRADING_DAYS = 1
DTE_MAX_TRADING_DAYS = 3

SHORT_DELTA_TARGET = 0.275
SHORT_DELTA_MIN = 0.25
SHORT_DELTA_MAX = 0.30

# IV-relative delta scaling: the short (premium-capture) legs move INVERSELY with
# the vol level. When IV is high the wings move fast, so push strikes FURTHER OTM
# (lower delta) to lift probability-of-profit; when IV is low/crushed, move CLOSER
# to ATM (higher delta) so the credit is still worth taking.
DYN_DELTA_LOW_IV = 0.30          # target delta when ATM IV is low  (closer to ATM, keep credit)
DYN_DELTA_HIGH_IV = 0.25         # target delta when ATM IV is high (further OTM) - clamped to the 0.25-0.30 competition band
DYN_IV_LOW = 0.15               # ATM IV at/below this is "low vol"
DYN_IV_HIGH = 0.30             # ATM IV at/above this is "high vol"

LONG_DELTA_TARGET = 0.10
LONG_DELTA_TOLERANCE = 0.05   # accept 0.05-0.15 for the long leg's delta
LONG_OTM_OFFSET = 5.0         # $ wing width when no ~0.10-delta strike is available

MIN_CREDIT_TO_WIDTH = 0.20    # net credit must be >= 20% of the wing width

# IV must sit at least this far (annualized vol points) above 10-day realized vol,
# i.e. options are pricing in more movement than the underlying has actually made.
# None in the snapshot (not enough price history) -> the check is skipped.
MIN_IV_RV_SPREAD = 0.015

# --- Dynamic market-regime switch -------------------------------------------- #
# "IV >> RV" (rich) -> Regime A ;  "IV << RV" (cheap) -> Regime B / C.
LOW_IV_RV_SPREAD = -0.02          # IV at least this far BELOW RV counts as "IV << RV"
EFFICIENCY_RATIO_WINDOW = 10      # trading days for the Kaufman efficiency ratio
RANGE_BOUND_ER = 0.45            # ER < this -> range-bound ; ER >= this -> trending

# ADX trend-strength filter (context-supplied; Wilder 14). ER can be noisy, so a
# confirmed strong trend hard-disables the iron condor:
#   ADX >= ADX_TREND_HIGH  -> strong trend: condor OFF, directional credit spread only
#   ADX <  ADX_RANGE_LOW   -> dead market: condor welcome (ER logic unchanged)
#   in between             -> indeterminate: fall back to the Kaufman ER call
ADX_TREND_HIGH = 25.0
ADX_RANGE_LOW = 20.0
STRANGLE_DELTA_TARGET = 0.25      # long strangle legs: ~0.25 delta each side

REGIME_IRON_CONDOR = "iron_condor"
REGIME_LONG_STRANGLE = "long_strangle"
REGIME_BULL_PUT = "bull_put"
REGIME_BEAR_CALL = "bear_call"
REGIME_NONE = "none"

# Sizing budget for the proposal: 1.5% of the nominal $100k paper account.
# MAX_RISK_PER_TRADE_PCT is the single source of truth (shared with risk_manager),
# so this figure can't drift from the pre-trade gate.
NOMINAL_EQUITY = 100_000.0
MAX_RISK_PER_TRADE = MAX_RISK_PER_TRADE_PCT * NOMINAL_EQUITY  # $2,000 (2.0%)
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
    max_loss_per_contract: float | None = None  # $ per contract, true worst case
    suggested_contracts: int | None = None   # within MAX_RISK_PER_TRADE
    # --- market-regime switch tags (set by build_strategy_plan) --- #
    structure: str = REGIME_IRON_CONDOR       # iron_condor | long_strangle | bull_put | bear_call
    regime: str | None = None                 # human label, e.g. "Regime B: Low IV / Range-Bound"
    regime_reason: str | None = None          # quantitative detail (logged, sent to risk_officer)
    symbol: str | None = None                 # basket ticker this plan is for
    direction: str | None = None              # "up" | "down" for credit spreads

    def describe(self) -> str:
        lines = [
            f"eligible:      {self.eligible}",
            f"reason:        {self.reason}",
            f"structure:     {self.structure}" + (f"  ({self.symbol})" if self.symbol else ""),
        ]
        if self.regime:
            lines.append(f"regime:        {self.regime}")
        if self.regime_reason:
            lines.append(f"regime detail: {self.regime_reason}")
        lines.append(f"IV mode:       {self.iv_regime_mode}")
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
        if self.net_credit is not None and self.credit_to_width is not None:
            lines.append(
                f"net credit:    {self.net_credit:.2f}  "
                f"({self.credit_to_width:.1%} of {self.wing_width:.2f} wing; "
                f"target {MIN_CREDIT_TO_WIDTH:.0%})"
            )
        elif self.net_credit is not None:
            kind = "debit" if self.net_credit < 0 else "credit"
            lines.append(f"net {kind}:     {abs(self.net_credit):.2f}")
        if self.max_loss_per_contract is not None:
            lines.append(
                f"max loss:      ${self.max_loss_per_contract:.0f}/contract  ->  "
                f"{self.suggested_contracts} contract(s) within ${MAX_RISK_PER_TRADE:.0f} cap"
            )
        return "\n".join(lines)


# ``IronCondorPlan`` is now the shared result type for every structure the
# regime switch can pick. Alias for readability in new code.
StrategyPlan = IronCondorPlan


# --------------------------------------------------------------------------- #
# Range-bound filter — Kaufman efficiency ratio
# --------------------------------------------------------------------------- #
def efficiency_ratio(prices, window: int = EFFICIENCY_RATIO_WINDOW):
    """ER = |net change over window| / sum(|daily absolute changes|).

    1.0 = a straight line (perfectly trending); ~0 = lots of back-and-forth with
    little net progress (range-bound). Returns ``None`` if fewer than
    ``window + 1`` prices are available; ``0.0`` for a perfectly flat series.
    """
    if prices is None or len(prices) < window + 1:
        return None
    seg = list(prices)[-(window + 1):]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    if path == 0:
        return 0.0
    return round(net / path, 4)


def is_range_bound(prices, window: int = 10, threshold: float = 0.3):
    """``True`` when ER < ``threshold`` (range-bound), ``False`` when ER >=
    ``threshold`` (trending), ``None`` when there isn't enough price history."""
    er = efficiency_ratio(prices, window)
    if er is None:
        return None
    return er < threshold


def trend_direction(prices, window: int = EFFICIENCY_RATIO_WINDOW):
    """``"up"`` / ``"down"`` / ``"flat"`` over the window; ``None`` if too short."""
    if prices is None or len(prices) < window + 1:
        return None
    seg = list(prices)[-(window + 1):]
    change = seg[-1] - seg[0]
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "flat"


# --------------------------------------------------------------------------- #
# Dynamic market-regime switch
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegimeDecision:
    regime: str                       # a REGIME_* structure, or REGIME_NONE
    label: str                        # human-readable, for logs / daily summary
    reason: str                       # quantitative detail, sent to the risk_officer
    efficiency_ratio: float | None = None
    direction: str | None = None      # "up" | "down" for Regime C


SHORT_VOL_STRUCTURES = frozenset({REGIME_IRON_CONDOR, REGIME_BULL_PUT, REGIME_BEAR_CALL})


def _ctx_adx(context, snapshot) -> tuple[float | None, str | None]:
    """(adx, direction) for this snapshot's symbol from the MarketContext, or
    (None, None) when no context / no ADX is available (tolerant of the plain
    SimpleNamespace contexts used in tests)."""
    if context is None:
        return None, None
    sym = snapshot.get("symbol") or snapshot.get("underlying")
    getter = getattr(context, "adx_for", None)
    if not (getter and sym):
        return None, None
    adx = getter(sym)
    dir_getter = getattr(context, "adx_direction_for", None)
    return adx, (dir_getter(sym) if dir_getter else None)


def select_regime(
    snapshot: dict,
    *,
    min_iv_rv: float = MIN_IV_RV_SPREAD,
    low_iv_rv: float = LOW_IV_RV_SPREAD,
    er_threshold: float = RANGE_BOUND_ER,
    context=None,
) -> RegimeDecision:
    """Quantitative regime (see :func:`_quant_regime`), then a **contextual
    override**: when the IntelligenceHub flags ``MACRO_DANGER`` or
    ``PANIC_REGIME``, any *short-volatility* selection (iron condor / credit
    spread) is vetoed and swapped for a **long strangle** — don't sell premium
    into a known event or an inverted VIX curve. A quant "No trade" is left
    alone: the override never manufactures a position."""
    base = _quant_regime(
        snapshot, min_iv_rv=min_iv_rv, low_iv_rv=low_iv_rv, er_threshold=er_threshold
    )
    flags = context.regime_flags() if context is not None else []
    if flags and base.regime in SHORT_VOL_STRUCTURES:
        return RegimeDecision(
            REGIME_LONG_STRANGLE,
            f"Regime OVERRIDE: {'/'.join(flags)} -> Long Strangle (vetoed short-vol {base.regime})",
            f"{'/'.join(flags)} active — {base.reason}",
            base.efficiency_ratio,
            base.direction,
        )

    # ADX trend-strength filter: a confirmed strong trend is the #1 iron-condor
    # killer (selling a range into a breakout). Disable the condor and demand a
    # directional credit spread aligned with the trend; if the trend has no
    # clear side, stand aside. ADX in the 20-25 band is left to the ER call.
    adx, adx_dir = _ctx_adx(context, snapshot)
    if adx is not None and adx >= ADX_TREND_HIGH and base.regime == REGIME_IRON_CONDOR:
        direction = adx_dir or trend_direction(snapshot.get("daily_closes"))
        if direction == "up":
            return RegimeDecision(
                REGIME_BULL_PUT,
                f"ADX OVERRIDE: ADX {adx:.1f} >= {ADX_TREND_HIGH:.0f} (strong up-trend) -> Bull Put (condor disabled)",
                f"strong trend (ADX {adx:.1f}); {base.reason}",
                base.efficiency_ratio, "up",
            )
        if direction == "down":
            return RegimeDecision(
                REGIME_BEAR_CALL,
                f"ADX OVERRIDE: ADX {adx:.1f} >= {ADX_TREND_HIGH:.0f} (strong down-trend) -> Bear Call (condor disabled)",
                f"strong trend (ADX {adx:.1f}); {base.reason}",
                base.efficiency_ratio, "down",
            )
        return RegimeDecision(
            REGIME_NONE,
            f"ADX OVERRIDE: ADX {adx:.1f} >= {ADX_TREND_HIGH:.0f} (strong trend, no clear direction) -> stand aside",
            f"strong trend, condor disabled; {base.reason}",
            base.efficiency_ratio,
        )
    return base


def _quant_regime(
    snapshot: dict,
    *,
    min_iv_rv: float = MIN_IV_RV_SPREAD,
    low_iv_rv: float = LOW_IV_RV_SPREAD,
    er_threshold: float = RANGE_BOUND_ER,
) -> RegimeDecision:
    """Pick the structure for the current quantitative regime.

    * **Regime A** — IV > RV (spread >= ``min_iv_rv``) *and* IV elevated
      (``iv_regime.trade_eligible``) -> **Iron Condor**.
    * **Regime B** — IV << RV (spread <= ``low_iv_rv``) *and* range-bound
      (ER < ``er_threshold``) -> **Long Strangle** (bet on vol mean-reversion).
    * **Regime C** — IV << RV *and* trending (ER >= ``er_threshold``) ->
      **Bull Put** if the trend is up, **Bear Call** if it is down.
    * Anything else -> no trade this cycle.
    """
    atm_iv = snapshot.get("atm_iv")
    spread = snapshot.get("iv_rv_spread")
    iv_regime = snapshot.get("iv_regime")
    iv_elevated = bool(getattr(iv_regime, "trade_eligible", False))
    closes = snapshot.get("daily_closes")
    er = efficiency_ratio(closes)

    if atm_iv is None or spread is None:
        return RegimeDecision(REGIME_NONE, "No trade", "ATM IV or realized vol unavailable", er)

    if spread >= min_iv_rv and iv_elevated:
        return RegimeDecision(
            REGIME_IRON_CONDOR,
            "Regime A: High Volatility -> Iron Condor",
            f"IV {atm_iv:.3f} > RV (spread {spread:+.3f} >= {min_iv_rv:+.3f}) and IV elevated",
            er,
        )

    if spread <= low_iv_rv:
        rb = is_range_bound(closes, threshold=er_threshold)
        if rb is None:
            return RegimeDecision(
                REGIME_NONE, "No trade",
                f"IV << RV (spread {spread:+.3f}) but not enough price history for the efficiency ratio",
                er,
            )
        if rb:
            return RegimeDecision(
                REGIME_LONG_STRANGLE,
                "Regime B: Low IV / Range-Bound -> Long Strangle",
                f"IV {atm_iv:.3f} << RV (spread {spread:+.3f} <= {low_iv_rv:+.3f}); "
                f"ER {er:.3f} < {er_threshold} (range-bound); betting on volatility mean-reversion",
                er,
            )
        direction = trend_direction(closes)
        if direction not in ("up", "down"):
            return RegimeDecision(
                REGIME_NONE, "No trade",
                f"IV << RV and trending (ER {er:.3f} >= {er_threshold}) but no clear direction",
                er,
            )
        structure = REGIME_BULL_PUT if direction == "up" else REGIME_BEAR_CALL
        name = "Bull Put" if structure == REGIME_BULL_PUT else "Bear Call"
        return RegimeDecision(
            structure,
            f"Regime C: Low IV / Trending {direction} -> {name} Credit Spread",
            f"IV {atm_iv:.3f} << RV (spread {spread:+.3f} <= {low_iv_rv:+.3f}); "
            f"ER {er:.3f} >= {er_threshold} (trending {direction})",
            er, direction,
        )

    return RegimeDecision(
        REGIME_NONE, "No trade",
        f"neutral volatility regime (IV-RV spread {spread:+.3f} between "
        f"{low_iv_rv:+.3f} and {min_iv_rv:+.3f})",
        er,
    )


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


def select_leg_near_delta(
    legs: list[OptionContract],
    target: float,
    *,
    lo: float | None = None,
    hi: float | None = None,
) -> OptionContract | None:
    """Graded contract whose ``abs_delta`` is closest to ``target``. If ``lo``/``hi``
    are given, contracts inside that band are preferred (but not required)."""
    graded = [c for c in legs if c.abs_delta is not None]
    if not graded:
        return None
    if lo is not None and hi is not None:
        band = [c for c in graded if lo <= c.abs_delta <= hi]
        graded = band or graded
    return min(graded, key=lambda c: abs(c.abs_delta - target))


def dynamic_short_delta(atm_iv: float | None) -> float:
    """IV-relative short-leg delta target (moves *inversely* with vol):

    * ``atm_iv`` at/below ``DYN_IV_LOW``  -> ``DYN_DELTA_LOW_IV``  (0.25, closer to
      ATM: vol is crushed, reach a little to keep a worthwhile credit);
    * ``atm_iv`` at/above ``DYN_IV_HIGH`` -> ``DYN_DELTA_HIGH_IV`` (0.15, further
      OTM: the wings move fast, buy probability-of-profit);
    * anything in the normal band (or ``None``) -> the unchanged
      ``SHORT_DELTA_TARGET`` (0.225).
    """
    if atm_iv is None:
        return SHORT_DELTA_TARGET
    if atm_iv <= DYN_IV_LOW:
        return DYN_DELTA_LOW_IV
    if atm_iv >= DYN_IV_HIGH:
        return DYN_DELTA_HIGH_IV
    return SHORT_DELTA_TARGET


def select_short_leg(
    legs: list[OptionContract], *, target: float | None = None
) -> OptionContract | None:
    """Contract nearest the short-delta target. With no ``target`` it uses the
    fixed ``SHORT_DELTA_TARGET`` and the 0.20-0.25 band; with a dynamic ``target``
    it prefers a +/-0.05 band around it."""
    if target is None:
        return select_leg_near_delta(
            legs, SHORT_DELTA_TARGET, lo=SHORT_DELTA_MIN, hi=SHORT_DELTA_MAX
        )
    return select_leg_near_delta(legs, target, lo=target - 0.05, hi=target + 0.05)


def rank_basket(symbols, snapshots: dict, context) -> list[str]:
    """Relative-value optimiser: order the basket best-first — richest IV-RV
    spread wins, news-sentiment score breaks ties. Tickers with no spread sink to
    the bottom. Mirrors ``context_gatherer.prioritize`` but lives with the
    strategy so ``main`` asks the strategy layer "what should I trade first?"."""
    def key(sym: str):
        spread = (snapshots.get(sym) or {}).get("iv_rv_spread")
        spread = spread if spread is not None else float("-inf")
        tc = context.ticker(sym) if context is not None else None
        news = getattr(tc, "news_score", 0) if tc else 0
        return (spread, news)

    return sorted(symbols, key=key, reverse=True)


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

    delta_target = dynamic_short_delta(getattr(iv_regime, "atm_iv", None))
    tgt = None if delta_target == SHORT_DELTA_TARGET else delta_target
    short_put = select_short_leg(puts, target=tgt)
    short_call = select_short_leg(calls, target=tgt)
    if short_put is None or short_call is None:
        return result(
            False, f"could not find short legs near {delta_target:.2f} delta", expiry=expiry
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
    """Fetch a market snapshot (if not supplied) and propose an iron condor.

    Kept for callers that want a condor unconditionally. The regime-aware entry
    point is :func:`build_strategy_plan`.
    """
    snap = snapshot or get_market_snapshot()
    contracts = [c for c in build_contracts(snap["chain"]) if c.abs_delta is not None]
    return plan_iron_condor(
        contracts,
        underlying_price=snap.get("current_price"),
        iv_regime=snap["iv_regime"],
        iv_rv_spread=snap.get("iv_rv_spread"),
        today=today,
    )


# --------------------------------------------------------------------------- #
# Regime B — long strangle (net debit; inherently defined-risk = premium paid)
# --------------------------------------------------------------------------- #
def plan_long_strangle(
    contracts: list[OptionContract],
    *,
    underlying_price: float | None,
    iv_regime,
    iv_rv_spread: float | None = None,
    today: date | None = None,
) -> IronCondorPlan:
    """Buy a ~0.25-delta OTM put and a ~0.25-delta OTM call. Max loss = net debit
    paid; sized so that stays within ``MAX_RISK_PER_TRADE`` (1.5%)."""
    mode = getattr(iv_regime, "mode", None)

    def result(eligible: bool, reason: str, **kw) -> IronCondorPlan:
        return IronCondorPlan(
            eligible=eligible, reason=reason, underlying_price=underlying_price,
            iv_regime_mode=mode, iv_rv_spread=iv_rv_spread,
            structure=REGIME_LONG_STRANGLE, **kw,
        )

    expiry = pick_expiry(contracts, today=today)
    if expiry is None:
        return result(False, "no listed expiry in the 1-3 trading-day window")

    at_expiry = [c for c in contracts if c.expiry == expiry]
    long_put = select_leg_near_delta(
        [c for c in at_expiry if c.right == "put"], STRANGLE_DELTA_TARGET
    )
    long_call = select_leg_near_delta(
        [c for c in at_expiry if c.right == "call"], STRANGLE_DELTA_TARGET
    )
    if long_put is None or long_call is None:
        return result(False, "could not find ~0.25-delta strangle legs", expiry=expiry)

    legs = [CondorLeg("buy", "put", long_put), CondorLeg("buy", "call", long_call)]
    debit = long_put.mid + long_call.mid                 # $ per spread paid
    max_loss = debit * CONTRACT_MULTIPLIER               # per contract worst case
    n = int(MAX_RISK_PER_TRADE // max_loss) if max_loss > 0 else 0
    tag = (
        f"expiry {expiry.isoformat()}; {long_put.strike:.0f}P / {long_call.strike:.0f}C; "
        f"debit {debit:.2f}"
    )
    priced = dict(
        expiry=expiry, legs=legs, net_credit=-debit, wing_width=0.0,
        max_loss_per_contract=max_loss, suggested_contracts=n,
    )
    if debit <= 0:
        return result(False, f"strangle debit <= 0 — bad quotes ({tag})", **priced)
    if n < 1:
        return result(
            False,
            f"strangle debit ${max_loss:.0f}/contract exceeds ${MAX_RISK_PER_TRADE:.0f} risk cap ({tag})",
            **priced,
        )
    return result(True, f"long strangle — betting on volatility expansion ({tag})", **priced)


# --------------------------------------------------------------------------- #
# Regime C — vertical credit spreads (bull put / bear call)
# --------------------------------------------------------------------------- #
def _plan_vertical(
    contracts: list[OptionContract],
    right: str,
    structure: str,
    *,
    underlying_price: float | None,
    iv_regime,
    iv_rv_spread: float | None = None,
    today: date | None = None,
) -> IronCondorPlan:
    mode = getattr(iv_regime, "mode", None)

    def result(eligible: bool, reason: str, **kw) -> IronCondorPlan:
        return IronCondorPlan(
            eligible=eligible, reason=reason, underlying_price=underlying_price,
            iv_regime_mode=mode, iv_rv_spread=iv_rv_spread, structure=structure, **kw,
        )

    expiry = pick_expiry(contracts, today=today)
    if expiry is None:
        return result(False, "no listed expiry in the 1-3 trading-day window")

    legs_for_right = [c for c in contracts if c.expiry == expiry and c.right == right]
    delta_target = dynamic_short_delta(getattr(iv_regime, "atm_iv", None))
    tgt = None if delta_target == SHORT_DELTA_TARGET else delta_target
    short = select_short_leg(legs_for_right, target=tgt)
    if short is None:
        return result(False, f"no short {right} near {delta_target:.2f} delta", expiry=expiry)
    long_leg, rule = select_long_leg(legs_for_right, short, right)
    if long_leg is None:
        return result(False, f"no protective long {right} ({rule})", expiry=expiry)

    legs = [CondorLeg("sell", right, short), CondorLeg("buy", right, long_leg)]
    credit = short.mid - long_leg.mid
    width = abs(short.strike - long_leg.strike)
    ctw = credit / width if width > 0 else 0.0
    max_loss = (width - credit) * CONTRACT_MULTIPLIER
    n = int(MAX_RISK_PER_TRADE // max_loss) if max_loss > 0 else 0
    tag = (
        f"expiry {expiry.isoformat()}; {short.strike:.0f}/{long_leg.strike:.0f} ({rule}); "
        f"credit {credit:.2f} / width {width:.2f} = {ctw:.1%}"
    )
    priced = dict(
        expiry=expiry, legs=legs, net_credit=credit, wing_width=width,
        credit_to_width=ctw, max_loss_per_contract=max_loss, suggested_contracts=n,
    )
    if credit <= 0:
        return result(False, f"net credit <= 0 ({tag})", **priced)
    if ctw < MIN_CREDIT_TO_WIDTH:
        return result(
            False, f"credit/width {ctw:.1%} below {MIN_CREDIT_TO_WIDTH:.0%} target ({tag})", **priced
        )
    if n < 1:
        return result(
            False,
            f"max loss ${max_loss:.0f}/contract exceeds ${MAX_RISK_PER_TRADE:.0f} risk cap ({tag})",
            **priced,
        )
    kind = "bull put" if structure == REGIME_BULL_PUT else "bear call"
    return result(True, f"{kind} credit spread — meets credit and risk criteria ({tag})", **priced)


def plan_bull_put(contracts, *, underlying_price, iv_regime, iv_rv_spread=None, today=None):
    """Sell a put spread below the market — used when the trend is up."""
    return _plan_vertical(
        contracts, "put", REGIME_BULL_PUT, underlying_price=underlying_price,
        iv_regime=iv_regime, iv_rv_spread=iv_rv_spread, today=today,
    )


def plan_bear_call(contracts, *, underlying_price, iv_regime, iv_rv_spread=None, today=None):
    """Sell a call spread above the market — used when the trend is down."""
    return _plan_vertical(
        contracts, "call", REGIME_BEAR_CALL, underlying_price=underlying_price,
        iv_regime=iv_regime, iv_rv_spread=iv_rv_spread, today=today,
    )


# --------------------------------------------------------------------------- #
# Regime-aware entry point
# --------------------------------------------------------------------------- #
_PLAN_FOR_REGIME = {
    REGIME_IRON_CONDOR: plan_iron_condor,
    REGIME_LONG_STRANGLE: plan_long_strangle,
    REGIME_BULL_PUT: plan_bull_put,
    REGIME_BEAR_CALL: plan_bear_call,
}


def build_strategy_plan(
    snapshot: dict | None = None, *, today: date | None = None, context=None
) -> IronCondorPlan:
    """Detect the market regime for ``snapshot`` and build the matching structure.

    Regime A -> Iron Condor, B -> Long Strangle, C -> Bull Put / Bear Call.
    ``context`` is the IntelligenceHub ``MarketContext``: a ``MACRO_DANGER`` /
    ``PANIC_REGIME`` flag vetoes a short-vol selection and forces a long strangle.
    The regime choice is logged explicitly (picked up by the daily summary) and
    attached to the returned plan for the risk_officer prompt. The downstream
    flow is unchanged: this returns an ``IronCondorPlan`` that
    ``executor.from_plan`` / ``risk_manager.check_order`` consume as before.
    """
    snap = snapshot or get_market_snapshot()
    symbol = snap.get("symbol") or snap.get("underlying") or "?"
    decision = select_regime(snap, context=context)

    log.info(
        "REGIME [%s]: %s | %s", symbol, decision.label, decision.reason
    )
    if "OVERRIDE" in decision.label:
        log.warning("REGIME OVERRIDE [%s]: %s", symbol, decision.label)

    if decision.regime == REGIME_NONE:
        plan = IronCondorPlan(
            eligible=False,
            reason=f"no tradeable regime — {decision.reason}",
            underlying_price=snap.get("current_price"),
            iv_regime_mode=getattr(snap.get("iv_regime"), "mode", None),
            iv_rv_spread=snap.get("iv_rv_spread"),
            structure=REGIME_NONE,
        )
    else:
        contracts = [c for c in build_contracts(snap["chain"]) if c.abs_delta is not None]
        plan = _PLAN_FOR_REGIME[decision.regime](
            contracts,
            underlying_price=snap.get("current_price"),
            iv_regime=snap["iv_regime"],
            iv_rv_spread=snap.get("iv_rv_spread"),
            today=today,
        )

    plan.regime = decision.label
    plan.regime_reason = decision.reason
    plan.symbol = symbol
    plan.direction = decision.direction

    verb = "selected" if plan.eligible else "not eligible"
    log.info("STRATEGY [%s]: %s %s — %s", symbol, plan.structure, verb, plan.reason)
    return plan


if __name__ == "__main__":
    import sys

    sym = sys.argv[1].upper() if len(sys.argv) > 1 else None
    snap = get_market_snapshot(sym) if sym else get_market_snapshot()
    print(build_strategy_plan(snap).describe())
