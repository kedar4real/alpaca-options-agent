"""
offhours.py - Off-Hours Intelligence: observability + proactive behaviour that
runs *around* the trading loop, not inside it.

Nothing here touches the trade path (strategy / risk_manager / risk_officer /
executor) or any risk limit. It only reads state and emits log lines:

  1. Heartbeat          - an hourly "still alive" line, market open or closed, so
                          the audit trail is continuous 24/7.
  2. Morning Brief       - 09:00-09:30 ET, scan the basket's pre-market gap vs the
                          prior close; a gap > 0.5% raises a PRE-MARKET ALERT with
                          a Trending-vs-Range read.
  3. Nightly Post-Mortem - at the close, a digest of the day's pipeline funnel
                          (scans -> proposed -> approved, vetoes by each gate),
                          open-position unrealized P&L, and the dominant regime.

All three are pure functions of the data handed to them; the scheduling (once per
hour / once per day / inside the window) and the IO live in ``main.py``.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

# --- tunables (main.py's Config can override the ones it exposes) ------------ #
HEARTBEAT_MIN_GAP_S = 3600.0          # one heartbeat per hour
MORNING_BRIEF_START = dtime(9, 0)     # ET - 30 min before the NYSE open
MORNING_BRIEF_END = dtime(9, 30)
GAP_ALERT_PCT = 0.5                   # |pre-market gap| over this -> PRE-MARKET ALERT


# --------------------------------------------------------------------------- #
# Shared helper
# --------------------------------------------------------------------------- #
def count_iv_readings(path: str) -> int:
    """Raw rows in ``iv_history.csv`` (the agent's accumulated IV memory).

    Counts data rows only - the header and any trailing blank line are ignored.
    Missing file -> 0.
    """
    if not os.path.isfile(path):
        return 0
    with open(path, "r", newline="") as f:
        return sum(1 for row in csv.DictReader(f) if (row.get("timestamp") or "").strip())


def interval_elapsed(
    last_iso: str, now: datetime, *, min_gap_seconds: float = HEARTBEAT_MIN_GAP_S
) -> bool:
    """True if at least ``min_gap_seconds`` have passed since ``last_iso`` (an
    ISO timestamp), or if there is no valid previous stamp. Used to gate the
    hourly heartbeat off a marker persisted in the session."""
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
        return (now - last).total_seconds() >= min_gap_seconds
    except (ValueError, TypeError):
        return True


# --------------------------------------------------------------------------- #
# 1. Heartbeat
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Heartbeat:
    at: datetime
    status: str          # "Active" (market open) | "Idle" (closed)
    connectivity: str    # "OK" | "Error"
    iv_readings: int

    def render(self) -> str:
        return (
            f"[{self.at:%Y-%m-%d %H:%M}] HEARTBEAT: "
            f"Status: {self.status} | Connectivity: {self.connectivity} | "
            f"Memory: {self.iv_readings} IV readings stored."
        )


def build_heartbeat(
    now: datetime, *, market_open: bool, connectivity_ok: bool, iv_readings: int
) -> Heartbeat:
    return Heartbeat(
        at=now,
        status="Active" if market_open else "Idle",
        connectivity="OK" if connectivity_ok else "Error",
        iv_readings=iv_readings,
    )


# --------------------------------------------------------------------------- #
# 2. Morning Brief
# --------------------------------------------------------------------------- #
def in_morning_brief_window(
    now_et: datetime, *, start: dtime = MORNING_BRIEF_START, end: dtime = MORNING_BRIEF_END
) -> bool:
    return start <= now_et.time() < end


@dataclass(frozen=True)
class TickerGap:
    symbol: str
    prev_close: float
    premarket: float

    @property
    def gap_pct(self) -> float:
        if not self.prev_close:
            return 0.0
        return round((self.premarket - self.prev_close) / self.prev_close * 100.0, 2)

    def is_significant(self, threshold: float = GAP_ALERT_PCT) -> bool:
        return abs(self.gap_pct) > threshold

    def regime_hint(self, threshold: float = GAP_ALERT_PCT) -> str:
        if not self.is_significant(threshold):
            return (
                f"flat open ({self.gap_pct:+.2f}%) - consistent with a RANGE-BOUND "
                f"regime; premium-selling bias intact"
            )
        direction = "up" if self.gap_pct > 0 else "down"
        return (
            f"gapped {direction} {self.gap_pct:+.2f}% (> {threshold:.1f}%) - a directional "
            f"pre-market suggests a TRENDING regime; expect the switch to favour "
            f"credit spreads over range-bound structures"
        )


def morning_brief_text(
    gaps: list[TickerGap], *, et_date, threshold: float = GAP_ALERT_PCT
) -> str:
    """Pre-market digest for the basket. Lists every ticker's gap vs prior close;
    a gap over ``threshold`` percent produces a PRE-MARKET ALERT block with the
    Trending-vs-Range read."""
    lines = [
        f"Pre-Market Brief - {et_date:%b %d, %Y}  (NYSE opens 09:30 ET)",
        "",
    ]
    if not gaps:
        lines.append("  (no pre-market quotes available this morning)")
        return "\n".join(lines)

    for g in gaps:
        lines.append(
            f"  {g.symbol:<5} prev close ${g.prev_close:,.2f} -> "
            f"pre-market ${g.premarket:,.2f}   ({g.gap_pct:+.2f}%)"
        )

    alerts = [g for g in gaps if g.is_significant(threshold)]
    lines.append("")
    if alerts:
        lines.append(
            f"PRE-MARKET ALERT - {len(alerts)} of {len(gaps)} ticker(s) gapped "
            f"more than {threshold:.1f}%:"
        )
        for g in alerts:
            lines.append(f"  * {g.symbol}: {g.regime_hint(threshold)}")
    else:
        lines.append(
            f"No ticker gapped more than {threshold:.1f}% - basket opening flat, "
            f"RANGE-BOUND bias intact."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 3. Nightly Post-Mortem
# --------------------------------------------------------------------------- #
@dataclass
class DailyActivity:
    """Per-ET-day accumulator for the pipeline funnel. Updated once per cycle
    from that cycle's ``DecisionSummary`` list and persisted in the session."""

    date: str
    basket_size: int = 0
    ticker_scans: int = 0        # DecisionSummary objects evaluated today
    proposed: int = 0            # strategy produced an eligible plan (reached risk_manager+)
    approved: int = 0            # executed
    rm_vetoes: int = 0           # blocked at risk_manager
    ro_vetoes: int = 0           # vetoed at risk_officer
    regimes: dict = field(default_factory=dict)   # regime label -> scan count

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "basket_size": self.basket_size,
            "ticker_scans": self.ticker_scans,
            "proposed": self.proposed,
            "approved": self.approved,
            "rm_vetoes": self.rm_vetoes,
            "ro_vetoes": self.ro_vetoes,
            "regimes": dict(self.regimes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DailyActivity":
        return cls(
            date=d.get("date", ""),
            basket_size=int(d.get("basket_size", 0)),
            ticker_scans=int(d.get("ticker_scans", 0)),
            proposed=int(d.get("proposed", 0)),
            approved=int(d.get("approved", 0)),
            rm_vetoes=int(d.get("rm_vetoes", 0)),
            ro_vetoes=int(d.get("ro_vetoes", 0)),
            regimes=dict(d.get("regimes", {})),
        )


_PROPOSED_STAGES = ("risk_manager", "risk_officer", "executor")


def accumulate_activity(activity: DailyActivity, decisions) -> DailyActivity:
    """Fold one cycle's decisions into the day's running totals. Mutates and
    returns ``activity``. Tolerates decisions with no ``plan`` (prechecks, data
    errors)."""
    for d in decisions:
        activity.ticker_scans += 1

        regime = getattr(getattr(d, "plan", None), "regime", None)
        if regime:
            activity.regimes[regime] = activity.regimes.get(regime, 0) + 1

        stage = getattr(d, "stage", None)
        outcome = getattr(d, "outcome", None)
        if stage in _PROPOSED_STAGES:
            activity.proposed += 1
        if outcome == "executed":
            activity.approved += 1
        elif outcome == "blocked" and stage == "risk_manager":
            activity.rm_vetoes += 1
        elif outcome == "vetoed" and stage == "risk_officer":
            activity.ro_vetoes += 1
    return activity


def dominant_regime(regimes: dict) -> str:
    """The most-scanned regime label of the day, bucketed to a headline phrase
    ("Overall Range-Bound" etc.) for the digest / social post."""
    if not regimes:
        return "n/a (no scans)"
    label, n = max(regimes.items(), key=lambda kv: kv[1])
    low = label.lower()
    if "range-bound" in low:
        bucket = "Range-Bound"
    elif "trending" in low:
        bucket = "Trending"
    elif "high volatility" in low:
        bucket = "High-Volatility"
    else:
        bucket = "Neutral / No-Trade"
    return f"Overall {bucket}  ({label} - {n} scans)"


def post_mortem_text(
    activity: DailyActivity,
    *,
    et_date,
    open_positions: int,
    unrealized_pnl: float | None,
) -> str:
    """End-of-day digest: the pipeline funnel, open exposure, dominant regime.
    Copy-pasteable for a social post or a judge review."""
    upl = "n/a" if unrealized_pnl is None else f"${unrealized_pnl:+,.0f}"
    lines = [
        f"Nightly Post-Mortem - {et_date:%b %d, %Y}",
        "",
        f"Ticker scans today:       {activity.ticker_scans}  "
        f"({activity.basket_size}-ticker basket)",
        f"Trades proposed:          {activity.proposed}",
        f"Trades approved:          {activity.approved}",
        f"Vetoed by risk_manager:   {activity.rm_vetoes}",
        f"Vetoed by risk_officer:   {activity.ro_vetoes}",
        f"Open positions:           {open_positions}",
        f"Unrealized P&L (open):    {upl}",
        f"Dominant regime:          {dominant_regime(activity.regimes)}",
    ]
    if activity.regimes:
        lines += ["", "Regime breakdown:"]
        for label, n in sorted(activity.regimes.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  - {label}: {n}")
    lines += ["", "#options #trading #algotrading #tradingbot"]
    return "\n".join(lines)
