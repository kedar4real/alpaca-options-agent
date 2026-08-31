"""
main.py — the autonomous trading loop.

Runs one cycle every ``AGENT_LOOP_INTERVAL_SECONDS`` (default 900 = 15 min).
Each cycle, during market hours only:

  1. Refresh the market snapshot (``data.py``) and the live ``AccountState``.
  2. **Manage open positions first** — for each open condor, close it if it has
     hit the profit target, hit the stop, or is within one trading day of expiry
     (``risk_manager.flag_expiring_positions``). Protect before seeking more.
  3. Re-check the risk halts against the **persisted** ``starting_equity``. If
     trading is halted (daily loss / total drawdown), skip new-trade evaluation.
  4. If there is room (< 3 open) and no halt, run the pipeline in strict order:
     ``strategy.plan_iron_condor`` -> ``risk_manager.check_order`` ->
     ``risk_officer.review_trade`` (45 s) -> ``executor.submit_iron_condor``.
     A rejection at any stage skips the rest.
  5. Log a full "Decision Summary" regardless of outcome.

At market close (>= 4:00 PM ET) it prints a copy-pasteable "Daily Performance
Summary". Every cycle is wrapped in try/except — one bad cycle logs and the loop
continues; it never crashes the process. Logs go to console **and**
``logs/agent.log``.

Startup persists ``starting_equity`` to ``session.json`` on first run and never
re-derives it from current equity again (that would silently corrupt the 5%
drawdown floor across restarts).

Config (environment variables, never hardcoded):

  AGENT_LOOP_INTERVAL_SECONDS   loop cadence in seconds        (default 900)
  AGENT_LOG_LEVEL               DEBUG / INFO / WARNING / ...    (default INFO)
  AGENT_ENV_FILE                path to the .env to load first  (default: auto)
  AGENT_SESSION_FILE            session state path              (default session.json)
  AGENT_LOG_FILE                log file path                   (default logs/agent.log)
  AGENT_REVIEW_TIMEOUT_SECONDS  risk_officer LLM timeout        (default 45)
  AGENT_PROFIT_TARGET_FRACTION  close at this fraction of credit (default 0.50)
  AGENT_STOP_LOSS_MULTIPLE      close at loss = N x credit       (default 2.0)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import risk_officer
from .data import get_market_snapshot
from .risk_manager import (
    DAILY_LOSS_HALT_PCT,
    MAX_CONCURRENT_POSITIONS,
    TOTAL_DRAWDOWN_FLOOR_PCT,
    AccountState,
    OpenPosition,
    OrderLeg,
    check_order,
    flag_expiring_positions,
)
from .strategy import build_iron_condor
from . import executor as executor_mod

log = logging.getLogger("agent")
ET = ZoneInfo("America/New_York")
MARKET_CLOSE_ET = dtime(16, 0)
CONTRACT_MULTIPLIER = 100


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    loop_interval_s: int = 900
    log_level: str = "INFO"
    env_file: str | None = None
    session_file: str = "session.json"
    log_file: str = "logs/agent.log"
    review_timeout_s: float = 45.0
    profit_target_fraction: float = 0.50
    stop_loss_multiple: float = 2.0

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            loop_interval_s=_env_int("AGENT_LOOP_INTERVAL_SECONDS", 900),
            log_level=os.environ.get("AGENT_LOG_LEVEL", "INFO").upper(),
            env_file=os.environ.get("AGENT_ENV_FILE") or None,
            session_file=os.environ.get("AGENT_SESSION_FILE", "session.json"),
            log_file=os.environ.get("AGENT_LOG_FILE", "logs/agent.log"),
            review_timeout_s=_env_float("AGENT_REVIEW_TIMEOUT_SECONDS", 45.0),
            profit_target_fraction=_env_float("AGENT_PROFIT_TARGET_FRACTION", 0.50),
            stop_loss_multiple=_env_float("AGENT_STOP_LOSS_MULTIPLE", 2.0),
        )


_LOGGING_READY = False


def setup_logging(level: str, log_file: str) -> None:
    """Console + rotating-free file handler. Safe to call more than once."""
    global _LOGGING_READY
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    if _LOGGING_READY:
        return
    # Windows consoles default to cp1252 and mangle non-ASCII (em dashes etc.).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_h = logging.FileHandler(log_file, encoding="utf-8")
    con_h = logging.StreamHandler(sys.stdout)
    for handler in (file_h, con_h):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    _LOGGING_READY = True


def load_env_file(path: str | None) -> None:
    """If AGENT_ENV_FILE is set, load it first (it wins over later discovery)."""
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        log.warning("AGENT_ENV_FILE %s not found — falling back to default discovery", p)
        return
    from dotenv import load_dotenv

    load_dotenv(p, override=True)
    log.info("loaded env file: %s", p)


# --------------------------------------------------------------------------- #
# Tracked positions + session state
# --------------------------------------------------------------------------- #
@dataclass
class TrackedCondor:
    """An iron condor this agent opened and is now managing."""

    id: str
    expiry: date
    quantity: int
    entry_credit: float                       # $ per spread received at open
    legs: tuple[OrderLeg, ...] = ()           # 4 legs, each with an OCC symbol
    opened_at: str = ""

    def as_open_position(self) -> OpenPosition:
        # OpenPosition.symbol carries our tracking id so flag_expiring_positions
        # round-trips it back to us.
        return OpenPosition(self.id, self.expiry, self.quantity, self.legs)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "expiry": self.expiry.isoformat(),
            "quantity": self.quantity,
            "entry_credit": self.entry_credit,
            "opened_at": self.opened_at,
            "legs": [
                {"action": lg.action, "right": lg.right,
                 "quantity": lg.quantity, "symbol": lg.symbol}
                for lg in self.legs
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedCondor":
        return cls(
            id=d["id"],
            expiry=date.fromisoformat(d["expiry"]),
            quantity=int(d["quantity"]),
            entry_credit=float(d["entry_credit"]),
            opened_at=d.get("opened_at", ""),
            legs=tuple(
                OrderLeg(lg["action"], lg["right"], int(lg["quantity"]), lg.get("symbol"))
                for lg in d.get("legs", [])
            ),
        )


@dataclass
class Session:
    starting_equity: float
    account_id: str = ""
    created_at: str = ""
    trading_halted: bool = False              # sticky, persisted across restarts
    open_condors: list[TrackedCondor] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # opened/closed events
    last_daily_summary_date: str = ""

    # -- persistence ----------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "starting_equity": self.starting_equity,
            "account_id": self.account_id,
            "created_at": self.created_at,
            "trading_halted": self.trading_halted,
            "open_condors": [c.to_dict() for c in self.open_condors],
            "history": self.history,
            "last_daily_summary_date": self.last_daily_summary_date,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            starting_equity=float(d["starting_equity"]),
            account_id=d.get("account_id", ""),
            created_at=d.get("created_at", ""),
            trading_halted=bool(d.get("trading_halted", False)),
            open_condors=[TrackedCondor.from_dict(c) for c in d.get("open_condors", [])],
            history=list(d.get("history", [])),
            last_daily_summary_date=d.get("last_daily_summary_date", ""),
        )

    def events_on(self, iso_date: str, kind: str) -> list[dict]:
        return [e for e in self.history
                if e.get("kind") == kind and e.get("at", "")[:10] == iso_date]


def save_session(session: Session, path: str) -> None:
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def load_session(path: str) -> Session | None:
    p = Path(path)
    if not p.is_file():
        return None
    return Session.from_dict(json.loads(p.read_text(encoding="utf-8")))


def load_or_init_session(path: str, *, account_id: str, live_equity: float) -> Session:
    """Load session.json, or create it from the REAL current equity on first run.

    On restart the persisted ``starting_equity`` is authoritative — it is never
    re-derived from ``live_equity`` again, or the drawdown floor would drift.
    """
    existing = load_session(path)
    if existing is not None:
        log.info(
            "session.json found — starting_equity=$%s (persisted, NOT re-derived)",
            f"{existing.starting_equity:,.2f}",
        )
        return existing

    session = Session(
        starting_equity=float(live_equity),
        account_id=account_id,
        created_at=datetime.now(ET).isoformat(),
    )
    save_session(session, path)
    log.info(
        "no session.json — created one; starting_equity=$%s (real equity from Alpaca)",
        f"{session.starting_equity:,.2f}",
    )
    return session


# --------------------------------------------------------------------------- #
# Pure logic: account reconciliation + halt status
# --------------------------------------------------------------------------- #
def reconcile_account_state(
    session: Session,
    *,
    current_equity: float,
    day_start_equity: float,
) -> AccountState:
    """Build the AccountState the gates consume. ``starting_equity`` comes from
    the persisted session; the sticky halt is carried forward."""
    return AccountState(
        starting_equity=session.starting_equity,
        current_equity=float(current_equity),
        day_start_equity=float(day_start_equity),
        open_positions=tuple(c.as_open_position() for c in session.open_condors),
        trading_halted=session.trading_halted,
    )


def halt_status(account: AccountState) -> str | None:
    """A human-readable reason if new trading is halted this cycle, else None.
    Uses the SAME thresholds as risk_manager, against persisted starting_equity."""
    if account.trading_halted:
        return "competition drawdown floor already breached — sticky halt in effect"

    total_dd = account.starting_equity - account.current_equity
    dd_limit = TOTAL_DRAWDOWN_FLOOR_PCT * account.starting_equity
    if total_dd >= dd_limit:
        return (f"total drawdown ${total_dd:,.0f} >= ${dd_limit:,.0f} "
                f"(5% of ${account.starting_equity:,.0f} starting equity)")

    daily_loss = account.day_start_equity - account.current_equity
    dl_limit = DAILY_LOSS_HALT_PCT * account.starting_equity
    if daily_loss >= dl_limit:
        return (f"daily loss ${daily_loss:,.0f} >= ${dl_limit:,.0f} "
                f"(2.5% of starting equity) — no new trades today")
    return None


def update_sticky_halt(session: Session, account: AccountState) -> bool:
    """Latch the competition-level halt once the 5% floor is breached. Returns
    True if the state changed (caller should persist)."""
    if session.trading_halted:
        return False
    total_dd = account.starting_equity - account.current_equity
    if total_dd >= TOTAL_DRAWDOWN_FLOOR_PCT * account.starting_equity:
        session.trading_halted = True
        log.warning(
            "STICKY HALT LATCHED — total drawdown $%s breached the 5%% floor; "
            "no new trades for the rest of the competition", f"{total_dd:,.0f}",
        )
        return True
    return False


# --------------------------------------------------------------------------- #
# Pure logic: position management triggers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CondorValuation:
    condor: TrackedCondor
    cost_to_close: float          # $ per spread to buy the structure back now (mid)

    @property
    def pnl_per_spread(self) -> float:
        return self.condor.entry_credit - self.cost_to_close

    @property
    def total_pnl(self) -> float:
        return self.pnl_per_spread * self.condor.quantity * CONTRACT_MULTIPLIER

    @property
    def captured_fraction(self) -> float:
        """Fraction of the entry credit kept: 1.0 = full max profit, negative = losing."""
        c = self.condor.entry_credit
        return self.pnl_per_spread / c if c else 0.0


def decide_exit(
    valuation: CondorValuation,
    *,
    is_expiring: bool,
    profit_target_fraction: float = 0.50,
    stop_loss_multiple: float = 2.0,
) -> str | None:
    """Should this condor be closed now? Checked in the spec's order:
    profit target, then stop loss, then expiry. First match wins."""
    if valuation.captured_fraction >= profit_target_fraction:
        return "profit-target"
    if valuation.pnl_per_spread <= -stop_loss_multiple * valuation.condor.entry_credit:
        return "stop-loss"
    if is_expiring:
        return "expiry"
    return None


def value_condor(legs: tuple[OrderLeg, ...], mid_by_symbol: dict[str, float]) -> float | None:
    """Net mid cost per spread to close the structure: pay the mid to buy back the
    legs originally sold, receive the mid for the legs originally bought.
    Returns None if any leg is missing a quote."""
    total = 0.0
    for leg in legs:
        mid = mid_by_symbol.get(leg.symbol or "")
        if mid is None:
            return None
        total += mid if leg.action == "sell" else -mid
    return round(total, 4)


def manage_open_positions(
    session: Session,
    valuations: list[CondorValuation],
    expiring_ids: set[str],
    *,
    close_fn,
    config: Config,
    now_iso: str,
) -> list[dict]:
    """Close every condor that hits a trigger. Mutates ``session`` (removes the
    condor, appends a history event). Returns the close events."""
    closed: list[dict] = []
    for val in valuations:
        reason = decide_exit(
            val,
            is_expiring=val.condor.id in expiring_ids,
            profit_target_fraction=config.profit_target_fraction,
            stop_loss_multiple=config.stop_loss_multiple,
        )
        if reason is None:
            continue
        try:
            close_fn(val.condor)
        except Exception as exc:  # noqa: BLE001 - never let one close crash the cycle
            log.error("failed to close condor %s (%s): %s", val.condor.id, reason, exc)
            continue

        event = {
            "kind": "closed", "at": now_iso, "id": val.condor.id, "reason": reason,
            "pnl": round(val.total_pnl, 2), "quantity": val.condor.quantity,
        }
        session.history.append(event)
        session.open_condors = [c for c in session.open_condors if c.id != val.condor.id]
        closed.append(event)
        log.info(
            "CLOSED condor %s — %s — P&L $%s (%d spread(s))",
            val.condor.id, reason, f"{val.total_pnl:,.2f}", val.condor.quantity,
        )
    return closed


# --------------------------------------------------------------------------- #
# Pure logic: the new-trade pipeline (strict stage order)
# --------------------------------------------------------------------------- #
@dataclass
class DecisionSummary:
    evaluated: bool
    stage: str          # precheck | strategy | risk_manager | risk_officer | executor
    outcome: str        # skipped | halted | blocked | vetoed | executed | error
    reason: str
    order_detail: str = ""
    plan: object = None
    decision: object = None
    review: object = None
    result: object = None

    def render(self) -> str:
        head = {
            "skipped": "Skipped", "halted": "Halted", "blocked": "Blocked",
            "vetoed": "Vetoed", "executed": "Executed", "error": "Error",
        }.get(self.outcome, self.outcome.title())
        line = f"DECISION SUMMARY — {head} at [{self.stage}]: {self.reason}"
        if self.order_detail:
            line += f"\n    order: {self.order_detail}"
        return line


def _describe_order(order, plan) -> str:
    syms = " ".join(f"{lg.action[0].upper()}{lg.right[0].upper()}:{lg.symbol}" for lg in order.legs)
    exp = getattr(plan, "expiry", None)
    return (f"{order.quantity}x condor exp {exp.isoformat() if exp else '?'} "
            f"credit ${order.net_credit:.2f} width ${order.wing_width:.2f} [{syms}]")


def evaluate_new_trade(
    snapshot: dict,
    account: AccountState,
    *,
    config: Config,
    today: date | None = None,
    plan_fn=None,
    to_order_fn=None,
    check_fn=None,
    review_fn=None,
    submit_fn=None,
    call_log: list[str] | None = None,
) -> DecisionSummary:
    """Run strategy -> risk_manager -> risk_officer -> executor **in that order**.
    A rejection at any stage returns immediately and the later stages never run.

    The ``*_fn`` hooks default to the real modules; tests inject spies. Any
    stage a spy is called is appended to ``call_log`` when provided.
    """
    plan_fn = plan_fn or (lambda snap, today=None: build_iron_condor(snap, today=today))
    to_order_fn = to_order_fn or executor_mod.from_iron_condor_plan
    check_fn = check_fn or check_order
    review_fn = review_fn or risk_officer.review_trade
    submit_fn = submit_fn or executor_mod.submit_iron_condor

    def mark(stage: str) -> None:
        if call_log is not None:
            call_log.append(stage)

    # ---- 1. strategy ------------------------------------------------------- #
    mark("strategy")
    plan = plan_fn(snapshot, today=today)
    if not getattr(plan, "eligible", False):
        return DecisionSummary(
            True, "strategy", "skipped",
            f"strategy did not propose a trade — {getattr(plan, 'reason', 'ineligible')}",
            plan=plan,
        )

    mark("to_order")
    order = to_order_fn(plan)
    detail = _describe_order(order, plan)

    # ---- 2. risk_manager ------------------------------------------------- #
    mark("risk_manager")
    decision = check_fn(order, account)
    if not decision.approved:
        return DecisionSummary(
            True, "risk_manager", "blocked",
            "risk_manager rejected — " + "; ".join(decision.blocks),
            detail, plan, decision,
        )

    # ---- 3. risk_officer ----------------------------------------------- #
    mark("risk_officer")
    review = review_fn(order, snapshot, account, timeout=config.review_timeout_s)
    if not getattr(review, "approved", False):
        return DecisionSummary(
            True, "risk_officer", "vetoed",
            f"risk_officer VETO ({getattr(review, 'provider', '?')}) — "
            f"{getattr(review, 'thesis', '')}",
            detail, plan, decision, review,
        )

    # ---- 4. executor (re-runs check_order internally — the real gate) --- #
    mark("executor")
    result = submit_fn(order, account)
    if getattr(result, "submitted", False):
        return DecisionSummary(
            True, "executor", "executed",
            f"submitted order {result.order_id} — {detail}",
            detail, plan, decision, review, result,
        )
    return DecisionSummary(
        True, "executor", "error",
        f"executor did not submit — {getattr(result, 'error', None) or 'blocked at final gate'}",
        detail, plan, decision, review, result,
    )


def evaluate_cycle_decision(
    snapshot: dict,
    account: AccountState,
    *,
    config: Config,
    today: date | None = None,
    **pipeline_kwargs,
) -> DecisionSummary:
    """Prechecks (halt, capacity) then the pipeline. Always returns a summary."""
    halt = halt_status(account)
    if halt is not None:
        return DecisionSummary(False, "precheck", "halted", halt)

    n_open = len(account.open_positions)
    if n_open >= MAX_CONCURRENT_POSITIONS:
        return DecisionSummary(
            False, "precheck", "skipped",
            f"max positions reached ({n_open}/{MAX_CONCURRENT_POSITIONS})",
        )

    return evaluate_new_trade(snapshot, account, config=config, today=today, **pipeline_kwargs)


# --------------------------------------------------------------------------- #
# Daily performance summary (copy-paste ready)
# --------------------------------------------------------------------------- #
def daily_summary_text(
    session: Session,
    *,
    current_equity: float,
    day_start_equity: float,
    et_date: date,
) -> str:
    iso = et_date.isoformat()
    opened = session.events_on(iso, "opened")
    closed = session.events_on(iso, "closed")
    day_pnl = current_equity - day_start_equity
    day_pct = (day_pnl / day_start_equity * 100.0) if day_start_equity else 0.0
    since_start = current_equity - session.starting_equity
    since_pct = (since_start / session.starting_equity * 100.0) if session.starting_equity else 0.0
    realized_today = sum(e.get("pnl", 0.0) for e in closed)

    lines = [
        f"SPY Iron Condor Agent — Daily Performance Summary ({et_date:%b %d, %Y})",
        "",
        f"Equity: ${current_equity:,.0f}   (day P&L ${day_pnl:+,.0f} / {day_pct:+.2f}%)",
        f"Since start: ${since_start:+,.0f} / {since_pct:+.2f}% (from ${session.starting_equity:,.0f})",
        f"Realized P&L on closes today: ${realized_today:+,.0f}",
        "",
        f"Trades today: {len(opened)} opened, {len(closed)} closed",
    ]
    for e in closed:
        lines.append(f"  - closed {e['id']} [{e['reason']}] P&L ${e.get('pnl', 0.0):+,.0f}")
    for e in opened:
        lines.append(f"  + opened {e['id']} [{e.get('detail', 'iron condor')}]")

    lines += ["", f"Open positions: {len(session.open_condors)}"]
    for c in session.open_condors:
        lines.append(f"  * {c.id}  exp {c.expiry:%m/%d}  x{c.quantity}  (${c.entry_credit:.2f} cr)")
    if session.trading_halted:
        lines += ["", "NOTE: competition drawdown floor breached — trading is halted."]
    lines += ["", "#options #trading #SPY #ironcondor #algotrading"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Live Alpaca adapter
# --------------------------------------------------------------------------- #
class AlpacaConnection:
    """Thin wrapper over alpaca-py's TradingClient — the only IO surface."""

    def __init__(self, creds=None):
        from alpaca.trading.client import TradingClient

        from .alpaca_trader import load_credentials

        self.creds = creds or load_credentials()
        self.trading = TradingClient(
            self.creds.api_key, self.creds.secret_key, paper=self.creds.paper
        )

    def get_clock(self):
        return self.trading.get_clock()

    def get_account(self):
        return self.trading.get_account()

    def get_positions(self):
        try:
            return list(self.trading.get_all_positions())
        except Exception as exc:  # noqa: BLE001
            log.error("get_all_positions failed: %s", exc)
            return []

    def close_condor(self, condor: TrackedCondor) -> None:
        for leg in condor.legs:
            if not leg.symbol:
                continue
            self.trading.close_position(leg.symbol)

    def value_condors(self, condors: list[TrackedCondor], spot: float | None) -> list[CondorValuation]:
        from .alpaca_trader import build_contracts, fetch_option_chain

        out: list[CondorValuation] = []
        for c in condors:
            try:
                chain = fetch_option_chain(
                    self.creds, expiry=c.expiry, spot=spot, strike_window_pct=0.20
                )
                mids = {ct.symbol: ct.mid for ct in build_contracts(chain)}
            except Exception as exc:  # noqa: BLE001
                log.error("could not price condor %s: %s", c.id, exc)
                continue
            cost = value_condor(c.legs, mids)
            if cost is None:
                log.warning("condor %s — missing a leg quote, skipping management", c.id)
                continue
            out.append(CondorValuation(c, cost))
        return out


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
@dataclass
class CycleReport:
    decision: DecisionSummary
    closed: list[dict]
    opened: dict | None = None


def run_cycle(conn: AlpacaConnection, session: Session, config: Config, *,
              now_et: datetime | None = None) -> CycleReport:
    now_et = now_et or datetime.now(ET)
    today = now_et.date()
    now_iso = now_et.isoformat()

    snapshot = get_market_snapshot()
    acct = conn.get_account()
    current_equity = float(acct.equity)
    day_start_equity = float(getattr(acct, "last_equity", None) or current_equity)

    account = reconcile_account_state(
        session, current_equity=current_equity, day_start_equity=day_start_equity
    )

    # 1. manage open positions FIRST
    expiring = flag_expiring_positions(account.open_positions, today=today)
    expiring_ids = {ep.position.symbol for ep in expiring}
    valuations = conn.value_condors(session.open_condors, snapshot.get("current_price"))
    closed = manage_open_positions(
        session, valuations, expiring_ids,
        close_fn=conn.close_condor, config=config, now_iso=now_iso,
    )

    # 2. sticky halt latch, then rebuild state (positions may have changed)
    update_sticky_halt(session, account)
    account = reconcile_account_state(
        session, current_equity=current_equity, day_start_equity=day_start_equity
    )

    # 3. + 4. prechecks and the pipeline
    decision = evaluate_cycle_decision(snapshot, account, config=config, today=today)

    opened_event = None
    if decision.outcome == "executed":
        opened_event = _record_opened(session, decision, now_iso)

    save_session(session, config.session_file)
    log.info("%s", decision.render())
    return CycleReport(decision=decision, closed=closed, opened=opened_event)


def _record_opened(session: Session, decision: DecisionSummary, now_iso: str) -> dict:
    plan = decision.plan
    result = decision.result
    qty = int(plan.suggested_contracts)
    order_id = str(getattr(result, "order_id", None) or f"cond-{now_iso[:19]}")
    legs = tuple(
        OrderLeg(cl.action, cl.right, qty, cl.contract.symbol) for cl in plan.legs
    )
    condor = TrackedCondor(
        id=order_id,
        expiry=plan.expiry,
        quantity=qty,
        entry_credit=float(plan.net_credit),
        legs=legs,
        opened_at=now_iso,
    )
    session.open_condors.append(condor)
    event = {"kind": "opened", "at": now_iso, "id": condor.id,
             "detail": decision.order_detail}
    session.history.append(event)
    log.info("OPENED condor %s — %s", condor.id, decision.order_detail)
    return event


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def _maybe_daily_summary(session: Session, conn: AlpacaConnection, clock, config: Config,
                         *, prev_is_open: bool) -> None:
    """Emit the daily summary right after the close, once per ET day."""
    now_et = clock.timestamp.astimezone(ET)
    et_date = now_et.date()
    already = session.last_daily_summary_date == et_date.isoformat()
    just_closed = prev_is_open and not clock.is_open
    after_close_flat = (not clock.is_open) and now_et.time() >= MARKET_CLOSE_ET
    if already or not (just_closed or after_close_flat):
        return
    try:
        acct = conn.get_account()
        text = daily_summary_text(
            session,
            current_equity=float(acct.equity),
            day_start_equity=float(getattr(acct, "last_equity", None) or acct.equity),
            et_date=et_date,
        )
        log.info("DAILY PERFORMANCE SUMMARY\n%s\n%s\n%s", "=" * 60, text, "=" * 60)
        session.last_daily_summary_date = et_date.isoformat()
        save_session(session, config.session_file)
    except Exception as exc:  # noqa: BLE001
        log.error("daily summary failed: %s", exc)


def startup(config: Config) -> tuple[AlpacaConnection, Session]:
    setup_logging(config.log_level, config.log_file)
    load_env_file(config.env_file)

    conn = AlpacaConnection()
    acct = conn.get_account()
    account_id = str(getattr(acct, "account_number", None) or getattr(acct, "id", ""))
    live_equity = float(acct.equity)

    session = load_or_init_session(
        config.session_file, account_id=account_id, live_equity=live_equity
    )

    risk_officer.warm_up()

    log.info(
        "STARTUP %s — account %s — starting_equity $%s — current equity $%s — "
        "loop every %ds",
        datetime.now(ET).isoformat(), account_id, f"{session.starting_equity:,.2f}",
        f"{live_equity:,.2f}", config.loop_interval_s,
    )
    return conn, session


def run_forever(config: Config | None = None) -> None:
    config = config or Config.from_env()
    conn, session = startup(config)
    prev_is_open = False

    while True:
        try:
            clock = conn.get_clock()
            if clock.is_open:
                run_cycle(conn, session, config,
                          now_et=clock.timestamp.astimezone(ET))
            else:
                log.info("market closed — next open %s", getattr(clock, "next_open", "?"))
            _maybe_daily_summary(session, conn, clock, config, prev_is_open=prev_is_open)
            prev_is_open = clock.is_open
        except KeyboardInterrupt:
            log.info("interrupted — shutting down")
            return
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not crash the loop
            log.exception("cycle failed (continuing next cycle): %s", exc)

        time.sleep(config.loop_interval_s)


def main(argv: list[str] | None = None) -> int:
    run_forever(Config.from_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
