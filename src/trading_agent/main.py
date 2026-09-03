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
  AGENT_ACTIVITY_LOG            off-hours-intelligence log file  (default logs/agent_activity.log)
  AGENT_HEARTBEAT_MINUTES       cadence of the HEARTBEAT line    (default 60)
  AGENT_GAP_ALERT_PCT           pre-market gap -> PRE-MARKET ALERT (default 0.5)

Off-hours intelligence (``offhours.py``), around the trade loop, never blocking it:

  * Heartbeat          — every AGENT_HEARTBEAT_MINUTES, market open or closed, a
    ``[ts] HEARTBEAT: Status ... | Connectivity ... | Memory N IV readings`` line.
  * Morning Brief       — 09:00-09:30 ET, once/day: the basket's pre-market gap vs
    the prior close; a gap over AGENT_GAP_ALERT_PCT logs a PRE-MARKET ALERT.
  * Nightly Post-Mortem — at the close, once/day: the pipeline funnel (scans ->
    proposed -> approved, vetoes per gate), open unrealized P&L, dominant regime.

  These also stream to ``AGENT_ACTIVITY_LOG`` for a standalone audit trail.
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

from . import alerts
from . import mcp_client
from . import context_gatherer
from . import intelligence_hub
from . import offhours
from . import risk_officer
from .alpaca_trader import fetch_recent_news, intraday_realized_vol
from .data import IV_HISTORY_PATH, STATIC_IV_THRESHOLD, get_market_snapshot
from .risk_manager import (
    DAILY_LOSS_HALT_PCT,
    MAX_CONCURRENT_POSITIONS,
    TOTAL_DRAWDOWN_FLOOR_PCT,
    AccountState,
    OpenPosition,
    OrderLeg,
    check_order,
    flag_expiring_positions,
    macro_risk_multiplier,
)
from .strategy import (
    HARVEST_SHORT_DELTA,
    HARVEST_SPREAD_WIDTH,
    MIN_CREDIT_TO_WIDTH,
    MIN_IV_RV_SPREAD,
    RANGE_BOUND_ER,
    build_strategy_plan,
    efficiency_ratio,
    rank_basket,
)
from . import executor as executor_mod

# Structures whose entry cost is a net debit (max loss = premium paid), so
# decide_exit() scores them on % of the debit rather than % of a credit.
DEBIT_STRUCTURES = frozenset({"long_strangle"})
DEFAULT_TICKERS = ("SPY", "QQQ", "IWM", "TLT")
# Step 4 — the multi-instrument scan universe (liquid, optionable ETFs across
# equity / rates / credit / commodity / EM sleeves).
DEFAULT_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV",
    "TLT", "XLF", "XLE", "XLK", "EEM", "HYG",
)

log = logging.getLogger("agent")
offhours_log = logging.getLogger("agent.offhours")   # also -> agent_activity.log
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


def _parse_et_wallclock(s: str):
    """``"YYYY-MM-DD HH:MM"`` -> an ET-aware datetime, or None for blank/garbage."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except ValueError:
        log.warning("AGENT_TRADE_NOT_BEFORE_ET %r not 'YYYY-MM-DD HH:MM' — ignored", s)
        return None


def parse_universe(raw: str) -> tuple[str, ...]:
    """``"spy, qqq ,, IWM"`` -> ``("SPY", "QQQ", "IWM")``. Upper-cased, trimmed,
    de-duplicated (first occurrence wins), blanks dropped."""
    out: list[str] = []
    for tok in (raw or "").replace("\n", ",").split(","):
        sym = tok.strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return tuple(out)


@dataclass(frozen=True)
class Config:
    loop_interval_s: int = 300                  # competition window: 5-min cadence
    log_level: str = "INFO"
    env_file: str | None = None
    session_file: str = "session.json"
    log_file: str = "logs/agent.log"
    review_timeout_s: float = 45.0
    profit_target_fraction: float = 0.35        # competition window: take credit at 35%
    stop_loss_multiple: float = 2.0
    stop_loss_max_loss_fraction: float | None = None  # credit stop at this fraction of defined max loss
    debit_stop_fraction: float = 0.50           # close a debit trade down this much of the premium
    trail_arm_fraction: float = 0.25            # start trailing once favourable P&L reaches this
    trail_giveback_fraction: float = 0.10       # exit if P&L falls this far below its peak
    tickers: tuple[str, ...] = DEFAULT_TICKERS  # scan universe evaluated each cycle
    scan_time_box_s: int = 150                  # per-cycle budget for gathering snapshots
    # off-hours intelligence (observability only — never touches the trade path)
    activity_log_file: str = "logs/agent_activity.log"
    heartbeat_minutes: int = 60                 # cadence of the hourly HEARTBEAT line
    gap_alert_pct: float = 0.5                  # pre-market gap over this -> PRE-MARKET ALERT
    debate_enabled: bool = True                 # run the Bull/Bear/Judge debate on the top pick
    self_correction: bool = True                # post_trade_analysis -> lessons_learned.json
    hard_stop_et: str = "2026-09-04 10:30"      # competition hard stop (ET wall clock)
    trade_not_before_et: "datetime | None" = None  # skip new-trade eval until this ET wall clock
    halt_file: str = "HALT"                     # if this file exists: manage only, no new trades
    audit_file: str | None = None               # append debate transcripts here (final-session evidence)
    disable_macro_danger: bool = False          # final-session override: suppress the MACRO_DANGER long-vol force
    harvest_mode: bool = False                  # final-session: force bull puts on bullish-sentiment names
    harvest_sentiment_min: float = 0.2          # news score above this -> harvest a bull put
    panic_flatten_equity: float | None = None   # absolute equity line: at/below -> panic flatten + halt
    mcp_enabled: bool = True                    # try the Alpaca MCP server for reads
    mcp_server_dir: str = "C:/alpaca-hackathon/alpaca-mcp-server"

    @classmethod
    def from_env(cls) -> "Config":
        # AGENT_UNIVERSE is the Step-4 scan universe; AGENT_TICKERS is kept as a
        # back-compatible alias. Either one, comma-separated; empty -> the 12-name
        # default universe.
        raw = os.environ.get("AGENT_UNIVERSE") or os.environ.get("AGENT_TICKERS", "")
        tickers = parse_universe(raw) or DEFAULT_UNIVERSE
        return cls(
            scan_time_box_s=_env_int("AGENT_SCAN_TIMEBOX_SECONDS", 150),
            loop_interval_s=_env_int("AGENT_LOOP_INTERVAL_SECONDS", 300),
            log_level=os.environ.get("AGENT_LOG_LEVEL", "INFO").upper(),
            env_file=os.environ.get("AGENT_ENV_FILE") or None,
            session_file=os.environ.get("AGENT_SESSION_FILE", "session.json"),
            log_file=os.environ.get("AGENT_LOG_FILE", "logs/agent.log"),
            review_timeout_s=_env_float("AGENT_REVIEW_TIMEOUT_SECONDS", 45.0),
            profit_target_fraction=_env_float("AGENT_PROFIT_TARGET_FRACTION", 0.35),
            stop_loss_multiple=_env_float("AGENT_STOP_LOSS_MULTIPLE", 2.0),
            stop_loss_max_loss_fraction=(
                _env_float("AGENT_STOP_LOSS_MAX_LOSS_FRACTION", 0.0) or None
            ),
            debit_stop_fraction=_env_float("AGENT_DEBIT_STOP_FRACTION", 0.50),
            trail_arm_fraction=_env_float("AGENT_TRAIL_ARM_FRACTION", 0.25),
            trail_giveback_fraction=_env_float("AGENT_TRAIL_GIVEBACK_FRACTION", 0.10),
            tickers=tickers,
            activity_log_file=os.environ.get("AGENT_ACTIVITY_LOG", "logs/agent_activity.log"),
            heartbeat_minutes=_env_int("AGENT_HEARTBEAT_MINUTES", 60),
            gap_alert_pct=_env_float("AGENT_GAP_ALERT_PCT", 0.5),
            debate_enabled=os.environ.get("AGENT_DEBATE", "true").strip().lower()
            not in ("0", "false", "no", "off"),
            self_correction=os.environ.get("AGENT_SELF_CORRECTION", "true").strip().lower()
            not in ("0", "false", "no", "off"),
            hard_stop_et=os.environ.get("AGENT_HARD_STOP_ET", "2026-09-04 10:30"),
            trade_not_before_et=_parse_et_wallclock(
                os.environ.get("AGENT_TRADE_NOT_BEFORE_ET", "")
            ),
            halt_file=os.environ.get("AGENT_HALT_FILE", "HALT"),
            audit_file=os.environ.get("AGENT_AUDIT_FILE") or None,
            disable_macro_danger=os.environ.get("AGENT_DISABLE_MACRO_DANGER", "false")
            .strip().lower() in ("1", "true", "yes", "on"),
            harvest_mode=os.environ.get("AGENT_HARVEST_MODE", "false")
            .strip().lower() in ("1", "true", "yes", "on"),
            harvest_sentiment_min=_env_float("AGENT_HARVEST_SENTIMENT_MIN", 0.2),
            panic_flatten_equity=(_env_float("AGENT_PANIC_FLATTEN_EQUITY", 0.0) or None),
            mcp_enabled=os.environ.get("AGENT_MCP", "true").strip().lower()
            not in ("0", "false", "no", "off"),
            mcp_server_dir=os.environ.get(
                "AGENT_MCP_SERVER_DIR", "C:/alpaca-hackathon/alpaca-mcp-server"),
        )


_LOGGING_READY = False


def setup_logging(level: str, log_file: str, activity_log_file: str | None = None) -> None:
    """Console + file handler for everything, plus an extra file handler that
    captures just the off-hours-intelligence events (heartbeat / morning brief /
    post-mortem) to ``activity_log_file``. Safe to call more than once."""
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

    if activity_log_file:
        # agent.offhours records land here AND (via propagation) in the main log.
        Path(activity_log_file).parent.mkdir(parents=True, exist_ok=True)
        act_h = logging.FileHandler(activity_log_file, encoding="utf-8")
        act_h.setFormatter(fmt)
        offhours_log.addHandler(act_h)
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


def load_env_early() -> None:
    """Load ``.env`` BEFORE ``Config.from_env()`` reads ``os.environ`` — otherwise
    ``AGENT_*`` keys placed in ``.env`` are silently ignored (only the lazily
    loaded LLM keys would take effect). ``AGENT_ENV_FILE`` (an explicit path)
    wins; otherwise default discovery walks up from the CWD. Never overrides a
    value already exported into the real environment."""
    from dotenv import find_dotenv, load_dotenv

    explicit = os.environ.get("AGENT_ENV_FILE")
    if explicit and Path(explicit).is_file():
        load_dotenv(explicit, override=False)
    else:
        # usecwd=True: discover from the working directory (the agent runs from
        # the repo root), not from this module's location.
        load_dotenv(find_dotenv(usecwd=True), override=False)


# --------------------------------------------------------------------------- #
# Tracked positions + session state
# --------------------------------------------------------------------------- #
@dataclass
class TrackedCondor:
    """A position this agent opened and is now managing. Name kept for
    compatibility; ``structure`` may be a condor, strangle or vertical spread."""

    id: str
    expiry: date
    quantity: int
    entry_credit: float                       # $ per spread; negative = net debit paid
    legs: tuple[OrderLeg, ...] = ()           # 2 or 4 legs, each with an OCC symbol
    opened_at: str = ""
    symbol: str = "SPY"                       # basket ticker
    structure: str = "iron_condor"           # iron_condor | long_strangle | bull_put | bear_call
    peak_gain_fraction: float = 0.0          # high-water mark of favourable P&L fraction (for the trail)

    def as_open_position(self) -> OpenPosition:
        # OpenPosition.symbol carries our tracking id so flag_expiring_positions
        # round-trips it back to us; underlying carries the basket ticker for the
        # correlation guard; entry_credit / structure feed the long-vol cap.
        return OpenPosition(self.id, self.expiry, self.quantity, self.legs,
                            underlying=self.symbol, entry_credit=self.entry_credit,
                            structure=self.structure)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "structure": self.structure,
            "expiry": self.expiry.isoformat(),
            "quantity": self.quantity,
            "entry_credit": self.entry_credit,
            "opened_at": self.opened_at,
            "peak_gain_fraction": self.peak_gain_fraction,
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
            symbol=d.get("symbol", "SPY"),
            structure=d.get("structure", "iron_condor"),
            expiry=date.fromisoformat(d["expiry"]),
            quantity=int(d["quantity"]),
            entry_credit=float(d["entry_credit"]),
            opened_at=d.get("opened_at", ""),
            peak_gain_fraction=float(d.get("peak_gain_fraction", 0.0)),
            legs=tuple(
                OrderLeg(lg["action"], lg["right"], int(lg["quantity"]), lg.get("symbol"))
                for lg in d.get("legs", [])
            ),
        )


@dataclass
class PendingOrder:
    """An order submitted to the broker but NOT yet confirmed filled.

    It counts toward the position cap (it is exposure-in-waiting), is promoted to
    a ``TrackedCondor`` once the broker reports ``filled``, and is cancelled +
    dropped if it sits unfilled past ``STALE_ORDER_CYCLES`` cycles. This is the
    fix for the "recorded OPEN on submit, never filled" phantom-position bug.
    """

    order_id: str
    symbol: str
    structure: str
    expiry: date
    quantity: int
    entry_credit: float                       # $ per spread; negative = net debit
    legs: tuple[OrderLeg, ...] = ()
    submitted_at: str = ""
    cycles_waited: int = 0

    def as_open_position(self) -> OpenPosition:
        return OpenPosition(self.order_id, self.expiry, self.quantity, self.legs,
                            underlying=self.symbol, entry_credit=self.entry_credit,
                            structure=self.structure)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "structure": self.structure,
            "expiry": self.expiry.isoformat(),
            "quantity": self.quantity,
            "entry_credit": self.entry_credit,
            "legs": [
                {"action": lg.action, "right": lg.right,
                 "quantity": lg.quantity, "symbol": lg.symbol}
                for lg in self.legs
            ],
            "submitted_at": self.submitted_at,
            "cycles_waited": self.cycles_waited,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingOrder":
        return cls(
            order_id=d["order_id"],
            symbol=d.get("symbol", "SPY"),
            structure=d.get("structure", "iron_condor"),
            expiry=date.fromisoformat(d["expiry"]),
            quantity=int(d["quantity"]),
            entry_credit=float(d["entry_credit"]),
            legs=tuple(
                OrderLeg(lg["action"], lg["right"], int(lg["quantity"]), lg.get("symbol"))
                for lg in d.get("legs", [])
            ),
            submitted_at=d.get("submitted_at", ""),
            cycles_waited=int(d.get("cycles_waited", 0)),
        )


@dataclass
class Session:
    starting_equity: float
    account_id: str = ""
    created_at: str = ""
    trading_halted: bool = False              # sticky, persisted across restarts
    open_condors: list[TrackedCondor] = field(default_factory=list)
    pending_orders: list[PendingOrder] = field(default_factory=list)  # submitted, not yet filled
    history: list[dict] = field(default_factory=list)  # opened/closed events
    last_daily_summary_date: str = ""
    # off-hours intelligence bookkeeping (one marker per timed behaviour)
    last_heartbeat_at: str = ""                        # ISO ts of the last HEARTBEAT
    last_morning_brief_date: str = ""                  # ET date of the last Morning Brief
    last_post_mortem_date: str = ""                    # ET date of the last Post-Mortem
    daily_activity: dict = field(default_factory=dict)  # "YYYY-MM-DD" -> DailyActivity dict
    hard_stop_done: bool = False                       # competition hard stop: book flattened + logged

    # -- persistence ----------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "starting_equity": self.starting_equity,
            "account_id": self.account_id,
            "created_at": self.created_at,
            "trading_halted": self.trading_halted,
            "open_condors": [c.to_dict() for c in self.open_condors],
            "pending_orders": [p.to_dict() for p in self.pending_orders],
            "history": self.history,
            "last_daily_summary_date": self.last_daily_summary_date,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_morning_brief_date": self.last_morning_brief_date,
            "last_post_mortem_date": self.last_post_mortem_date,
            "daily_activity": self.daily_activity,
            "hard_stop_done": self.hard_stop_done,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            starting_equity=float(d["starting_equity"]),
            account_id=d.get("account_id", ""),
            created_at=d.get("created_at", ""),
            trading_halted=bool(d.get("trading_halted", False)),
            open_condors=[TrackedCondor.from_dict(c) for c in d.get("open_condors", [])],
            pending_orders=[PendingOrder.from_dict(p) for p in d.get("pending_orders", [])],
            history=list(d.get("history", [])),
            last_daily_summary_date=d.get("last_daily_summary_date", ""),
            last_heartbeat_at=d.get("last_heartbeat_at", ""),
            last_morning_brief_date=d.get("last_morning_brief_date", ""),
            last_post_mortem_date=d.get("last_post_mortem_date", ""),
            daily_activity=dict(d.get("daily_activity", {})),
            hard_stop_done=bool(d.get("hard_stop_done", False)),
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
    risk_multiplier: float = 1.0,
) -> AccountState:
    """Build the AccountState the gates consume. ``starting_equity`` comes from
    the persisted session; the sticky halt is carried forward. ``risk_multiplier``
    is the macro guard (< 1.0 on a High-Impact macro day) — it only tightens
    gate 1, never loosens it."""
    return AccountState(
        starting_equity=session.starting_equity,
        current_equity=float(current_equity),
        day_start_equity=float(day_start_equity),
        # Pending (submitted, not-yet-filled) orders are exposure-in-waiting and
        # occupy a slot under the concurrent-position cap + correlation guard.
        open_positions=(
            tuple(c.as_open_position() for c in session.open_condors)
            + tuple(p.as_open_position() for p in session.pending_orders)
        ),
        trading_halted=session.trading_halted,
        risk_multiplier=float(risk_multiplier),
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


def update_sticky_halt(
    session: Session, account: AccountState, *, flatten_fn=None, panic_equity: float | None = None
) -> bool:
    """Latch the competition-level halt once the drawdown floor is breached.
    Returns True if the state changed (caller should persist).

    Two triggers, whichever fires first:
      * the 5% total-drawdown floor (fraction of *starting* equity); and
      * ``panic_equity`` — an absolute equity line (e.g. $95,100), so the final
        session can hold a tighter, explicit stop than the 5% rule implies.

    ``flatten_fn`` (the panic button): called once, on the latching cycle, to
    close every open position. Breaching the floor is not "stop opening", it is
    "get flat now and realise the loss rather than ride it lower". Any exception
    from the flatten is logged and swallowed so the halt still latches.
    """
    if session.trading_halted:
        return False
    total_dd = account.starting_equity - account.current_equity
    floor_breach = total_dd >= TOTAL_DRAWDOWN_FLOOR_PCT * account.starting_equity
    panic_breach = panic_equity is not None and account.current_equity <= panic_equity
    if floor_breach or panic_breach:
        session.trading_halted = True
        why = (
            f"equity ${account.current_equity:,.0f} <= panic line ${panic_equity:,.0f}"
            if panic_breach and not floor_breach
            else f"total drawdown ${total_dd:,.0f} breached the 5% floor"
        )
        log.warning("STICKY HALT LATCHED — %s; no new trades for the rest of the competition", why)
        if flatten_fn is not None:
            try:
                result = flatten_fn()
                log.warning("PANIC FLATTEN — %s (%s)", why, result)
            except Exception as exc:  # noqa: BLE001 - the halt must latch regardless
                log.error("PANIC FLATTEN failed (%s) — halt still latched", exc)
        alerts.notify("halt", reason=why)
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
        """Fraction of the entry credit kept: 1.0 = full max profit, negative =
        losing. Credit structures only (entry_credit > 0)."""
        c = self.condor.entry_credit
        return self.pnl_per_spread / c if c else 0.0

    @property
    def gain_fraction(self) -> float:
        """P&L as a fraction of the premium paid — for debit structures
        (entry_credit < 0). +0.5 = up 50% on the debit; -0.5 = down 50%."""
        debit = abs(self.condor.entry_credit)
        return self.pnl_per_spread / debit if debit else 0.0


def _spread_width(condor: "TrackedCondor") -> float | None:
    """Wing width in dollars from the OCC leg strikes — the wider side for a
    4-leg condor, the single gap for a 2-leg vertical. None if the legs cannot
    be parsed."""
    from .alpaca_trader import parse_occ_symbol

    try:
        by_right: dict[str, list[float]] = {}
        for lg in condor.legs:
            _root, _exp, right, strike = parse_occ_symbol(lg.symbol or "")
            by_right.setdefault(right, []).append(strike)
        widths = [max(v) - min(v) for v in by_right.values() if len(v) >= 2]
        return max(widths) if widths else None
    except (ValueError, TypeError):
        return None


def _max_loss_per_spread(condor: "TrackedCondor") -> float | None:
    """Per-share worst case for a defined-risk credit structure:
    ``wing_width - entry_credit``. None if the width can't be derived."""
    w = _spread_width(condor)
    if w is None:
        return None
    return w - condor.entry_credit


def decide_exit(
    valuation: CondorValuation,
    *,
    is_expiring: bool,
    profit_target_fraction: float = 0.35,
    stop_loss_multiple: float = 2.0,
    stop_loss_max_loss_fraction: float | None = None,
    debit_stop_fraction: float = 0.50,
    catalyst_hold: bool = False,
    trail_arm_fraction: float = 0.25,
    trail_giveback_fraction: float = 0.10,
) -> str | None:
    """Should this position be closed now? Checked in the spec's order:
    profit target, then stop loss, then expiry. First match wins.

    Credit structures (iron condor, verticals): profit at ``profit_target_fraction``
    of the credit captured; stop at a loss of ``stop_loss_multiple`` x the credit,
    OR — when ``stop_loss_max_loss_fraction`` is set — at that fraction of the
    structure's defined max loss (``wing_width - credit``), whichever trips first.
    Debit structures (long strangle): profit at +``profit_target_fraction`` of the
    premium; stop at -``debit_stop_fraction`` of the premium (you can never lose
    more than the premium, so the credit "2x" stop does not apply).

    ``catalyst_hold``: this is a long-vol position still alive for the
    MACRO_DANGER catalyst it was bought for — the -50% stop is suspended (the
    whole thesis is a large move *at* the event), but the profit target and the
    hard expiry / hard-stop flatten still apply.

    An ``orphan_leg`` (a single broker leg adopted by ``reconcile_open_book`` —
    e.g. one side of a strangle that filled alone) has no meaningful entry price,
    so it is closed on expiry only, never on P&L math."""
    if valuation.condor.structure == "orphan_leg":
        return "expiry" if is_expiring else None

    peak = valuation.condor.peak_gain_fraction

    def _trailed(current: float) -> bool:
        # armed once the position has shown `trail_arm_fraction` of favourable
        # P&L; fires when it gives back `trail_giveback_fraction` from that peak
        # while still in profit — locks a spike that faded below the fixed target
        # between cycles.
        return (peak >= trail_arm_fraction and current > 0.0
                and (peak - current) >= trail_giveback_fraction)

    if valuation.condor.structure in DEBIT_STRUCTURES:
        gain = valuation.gain_fraction
        if gain >= profit_target_fraction:
            return "profit-target"
        if _trailed(gain):
            return "trailing-take-profit"
        if not catalyst_hold and gain <= -debit_stop_fraction:
            return "stop-loss"
    else:
        captured = valuation.captured_fraction
        if captured >= profit_target_fraction:
            return "profit-target"
        if _trailed(captured):
            return "trailing-take-profit"
        if valuation.pnl_per_spread <= -stop_loss_multiple * valuation.condor.entry_credit:
            return "stop-loss"
        if stop_loss_max_loss_fraction is not None:
            ml = _max_loss_per_spread(valuation.condor)
            if ml is not None and valuation.pnl_per_spread <= -stop_loss_max_loss_fraction * ml:
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
    catalyst_date: date | None = None,
) -> list[dict]:
    """Close every condor that hits a trigger. Mutates ``session`` (removes the
    condor, appends a history event). Returns the close events.

    ``catalyst_date`` (set only under MACRO_DANGER): a long-vol position whose
    expiry is on/after it has its -50% stop suspended — "catalyst hold" — and is
    logged rather than closed on a stop-worthy mark."""
    closed: list[dict] = []
    for val in valuations:
        metric = (val.gain_fraction if val.condor.structure in DEBIT_STRUCTURES
                  else val.captured_fraction)
        if metric > val.condor.peak_gain_fraction:
            val.condor.peak_gain_fraction = round(metric, 4)

        hold = (
            catalyst_date is not None
            and val.condor.structure in DEBIT_STRUCTURES
            and val.condor.expiry >= catalyst_date
        )
        reason = decide_exit(
            val,
            is_expiring=val.condor.id in expiring_ids,
            profit_target_fraction=config.profit_target_fraction,
            stop_loss_multiple=config.stop_loss_multiple,
            stop_loss_max_loss_fraction=config.stop_loss_max_loss_fraction,
            debit_stop_fraction=config.debit_stop_fraction,
            catalyst_hold=hold,
            trail_arm_fraction=config.trail_arm_fraction,
            trail_giveback_fraction=config.trail_giveback_fraction,
        )
        if reason is None:
            if hold and val.gain_fraction <= -config.debit_stop_fraction:
                log.info(
                    "catalyst hold — %s %s at %.0f%% of premium; -%.0f%% stop "
                    "suspended until the %s catalyst clears",
                    val.condor.symbol, val.condor.id, val.gain_fraction * 100,
                    config.debit_stop_fraction * 100, catalyst_date,
                )
            continue
        try:
            close_fn(val.condor)
        except Exception as exc:  # noqa: BLE001 - never let one close crash the cycle
            log.error("failed to close %s %s (%s): %s",
                      val.condor.symbol, val.condor.id, reason, exc)
            continue

        event = {
            "kind": "closed", "at": now_iso, "id": val.condor.id,
            "symbol": val.condor.symbol, "structure": val.condor.structure,
            "reason": reason, "pnl": round(val.total_pnl, 2),
            "quantity": val.condor.quantity,
        }
        session.history.append(event)
        session.open_condors = [c for c in session.open_condors if c.id != val.condor.id]
        closed.append(event)
        log.info(
            "CLOSED %s %s [%s] — %s — P&L $%s (%d spread(s))",
            val.condor.symbol, val.condor.id, val.condor.structure, reason,
            f"{val.total_pnl:,.2f}", val.condor.quantity,
        )
        alerts.notify("trade_closed", symbol=val.condor.symbol,
                      structure=val.condor.structure, reason=reason,
                      pnl=val.total_pnl)
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
    market_context: str = ""          # the synthesis string handed to the risk_officer
    debate: str = ""                  # Bull/Bear/Judge transcript (top-ranked candidate)

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
    struct = getattr(plan, "structure", "iron_condor")
    tkr = getattr(plan, "symbol", None) or "?"
    prem = order.net_credit
    prem_s = f"debit ${abs(prem):.2f}" if prem < 0 else f"credit ${prem:.2f}"
    return (f"{order.quantity}x {tkr} {struct} exp {exp.isoformat() if exp else '?'} "
            f"{prem_s} width ${order.wing_width:.2f} [{syms}]")


def evaluate_new_trade(
    snapshot: dict,
    account: AccountState,
    *,
    config: Config,
    today: date | None = None,
    market_context: str = "",
    context=None,
    plan_fn=None,
    to_order_fn=None,
    check_fn=None,
    review_fn=None,
    submit_fn=None,
    news_fn=None,
    intraday_vol_fn=None,
    call_log: list[str] | None = None,
) -> DecisionSummary:
    """Run strategy -> risk_manager -> risk_officer -> executor **in that order**.
    A rejection at any stage returns immediately and the later stages never run.

    The ``*_fn`` hooks default to the real modules; tests inject spies. Any
    stage a spy is called is appended to ``call_log`` when provided.
    ``market_context`` is the synthesis string (into the risk_officer prompt +
    every summary); ``context`` is the ``MarketContext`` object (its
    MACRO_DANGER / PANIC_REGIME flags steer ``build_strategy_plan``).
    """
    _harvest_kw = (
        {"harvest_mode": True, "harvest_sentiment_min": config.harvest_sentiment_min}
        if config.harvest_mode else {}
    )
    plan_fn = plan_fn or (
        lambda snap, today=None: build_strategy_plan(
            snap, today=today, context=context, **_harvest_kw
        )
    )
    to_order_fn = to_order_fn or executor_mod.from_plan
    _clusters = tuple(getattr(context, "correlation_clusters", ()) or ())
    check_fn = check_fn or (lambda o, a: check_order(o, a, correlation_clusters=_clusters))
    review_fn = review_fn or risk_officer.review_trade
    submit_fn = submit_fn or executor_mod.submit_iron_condor

    def mark(stage: str) -> None:
        if call_log is not None:
            call_log.append(stage)

    _debate_txt = ""

    def done(summary: DecisionSummary) -> DecisionSummary:
        summary.market_context = market_context
        summary.debate = _debate_txt
        return summary

    # ---- 1. strategy ------------------------------------------------------- #
    mark("strategy")
    plan = plan_fn(snapshot, today=today)
    if not getattr(plan, "eligible", False):
        return done(DecisionSummary(
            True, "strategy", "skipped",
            f"strategy did not propose a trade — {getattr(plan, 'reason', 'ineligible')}",
            plan=plan,
        ))

    mark("to_order")
    order = to_order_fn(plan)
    detail = _describe_order(order, plan)

    # ---- 2. risk_manager ------------------------------------------------- #
    mark("risk_manager")
    decision = check_fn(order, account)
    if not decision.approved:
        return done(DecisionSummary(
            True, "risk_manager", "blocked",
            "risk_manager rejected — " + "; ".join(decision.blocks),
            detail, plan, decision,
        ))

    # ---- 3. risk_officer ----------------------------------------------- #
    # Hand the regime choice + macro context to the reviewer alongside the snapshot.
    # Step 5 — intraday context, fetched ONLY for orders that got this far (they
    # cost API calls). Both are fail-safe and neither is gated anywhere.
    _sym = snapshot.get("symbol") or snapshot.get("underlying") or ""

    def _safe(fn, default):
        if fn is None:
            return default
        try:
            return fn(_sym)
        except Exception as exc:  # noqa: BLE001 - context must never block a trade
            log.warning("intraday context for %s failed: %s", _sym, exc)
            return default

    review_snapshot = {
        **snapshot,
        "structure": getattr(plan, "structure", None),
        "regime": getattr(plan, "regime", None),
        "regime_reason": getattr(plan, "regime_reason", None),
        "market_context": market_context,
        "recent_headlines": _safe(news_fn, []),
        "intraday_rv": _safe(intraday_vol_fn, None),
    }
    log.info("[%s] intraday context — %d headline(s), intraday RV %s",
             _sym, len(review_snapshot["recent_headlines"]),
             review_snapshot["intraday_rv"])
    mark("risk_officer")
    review = review_fn(order, review_snapshot, account, timeout=config.review_timeout_s)
    _debate_txt = getattr(review, "transcript", lambda: "")() or ""
    if not getattr(review, "approved", False):
        return done(DecisionSummary(
            True, "risk_officer", "vetoed",
            f"risk_officer VETO ({getattr(review, 'provider', '?')}) — "
            f"{getattr(review, 'thesis', '')}",
            detail, plan, decision, review,
        ))

    # ---- 4. executor (re-runs check_order internally — the real gate) --- #
    mark("executor")
    result = submit_fn(order, account)
    if getattr(result, "submitted", False):
        return done(DecisionSummary(
            True, "executor", "executed",
            f"submitted order {result.order_id} — {detail}",
            detail, plan, decision, review, result,
        ))
    return done(DecisionSummary(
        True, "executor", "error",
        f"executor did not submit — {getattr(result, 'error', None) or 'blocked at final gate'}",
        detail, plan, decision, review, result,
    ))


def evaluate_cycle_decision(
    snapshot: dict,
    account: AccountState,
    *,
    config: Config,
    today: date | None = None,
    market_context: str = "",
    context=None,
    **pipeline_kwargs,
) -> DecisionSummary:
    """Prechecks (halt, dedup, capacity) then the pipeline. Always returns a summary."""
    halt = halt_status(account)
    if halt is not None:
        return DecisionSummary(False, "precheck", "halted", halt, market_context=market_context)

    # Per-symbol dedup: never stack a second structure on a ticker that already
    # has an open position OR a working order — one fill frees a cap slot and the
    # ranker would otherwise re-pick the same top name.
    sym = snapshot.get("symbol") or snapshot.get("underlying")
    if sym and sym in {op.underlying for op in account.open_positions if op.underlying}:
        return DecisionSummary(
            False, "precheck", "skipped",
            f"already holds a position or working order in {sym}",
            market_context=market_context,
        )

    n_open = len(account.open_positions)
    if n_open >= MAX_CONCURRENT_POSITIONS:
        return DecisionSummary(
            False, "precheck", "skipped",
            f"max positions reached ({n_open}/{MAX_CONCURRENT_POSITIONS})",
            market_context=market_context,
        )

    return evaluate_new_trade(snapshot, account, config=config, today=today,
                              market_context=market_context, context=context,
                              **pipeline_kwargs)


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

    basket = ", ".join(sorted({c.symbol for c in session.open_condors})) or "—"
    regimes_today = [e.get("regime") for e in opened if e.get("regime")]

    lines = [
        f"Multi-Ticker Options Agent — Daily Performance Summary ({et_date:%b %d, %Y})",
        "",
        f"Equity: ${current_equity:,.0f}   (day P&L ${day_pnl:+,.0f} / {day_pct:+.2f}%)",
        f"Since start: ${since_start:+,.0f} / {since_pct:+.2f}% (from ${session.starting_equity:,.0f})",
        f"Realized P&L on closes today: ${realized_today:+,.0f}",
        "",
        f"Trades today: {len(opened)} opened, {len(closed)} closed",
    ]
    for e in closed:
        lines.append(
            f"  - closed {e.get('symbol', '?')} {e['id']} "
            f"[{e.get('structure', 'iron_condor')} / {e['reason']}] "
            f"P&L ${e.get('pnl', 0.0):+,.0f}"
        )
    for e in opened:
        lines.append(
            f"  + opened {e.get('symbol', '?')} {e['id']} "
            f"[{e.get('structure', 'iron_condor')}] {e.get('detail', '')}".rstrip()
        )
    for r in regimes_today:
        lines.append(f"    regime: {r}")

    lines += ["", f"Open positions: {len(session.open_condors)}/3  (tickers: {basket})"]
    for c in session.open_condors:
        kind = "debit" if c.entry_credit < 0 else "credit"
        lines.append(
            f"  * {c.symbol} {c.id}  {c.structure}  exp {c.expiry:%m/%d}  "
            f"x{c.quantity}  (${abs(c.entry_credit):.2f} {kind})"
        )
    if session.trading_halted:
        lines += ["", "NOTE: competition drawdown floor breached — trading is halted."]
    lines += ["", "#options #trading #SPY #QQQ #IWM #TLT #algotrading"]
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
        # Optional MCP read path; attached by startup(). None => pure alpaca-py.
        self.mcp = None

    def account_snapshot(self) -> dict:
        """Per-cycle account read. Served by the MCP server when a session is
        up, otherwise by alpaca-py. Always returns usable floats — an MCP payload
        without a parseable equity degrades to the alpaca-py read."""
        if self.mcp is not None:
            snap = self.mcp.account_info()
            try:
                equity = float(snap["equity"])
            except (KeyError, TypeError, ValueError):
                snap = None
            if snap:
                last = snap.get("last_equity")
                try:
                    last = float(last)
                except (TypeError, ValueError):
                    last = equity
                return {"equity": equity, "last_equity": last}
        acct = self.get_account()
        equity = float(acct.equity)
        return {"equity": equity,
                "last_equity": float(getattr(acct, "last_equity", None) or equity)}

    def news(self, symbol: str, limit: int = 5) -> list[str]:
        """Per-symbol headlines: MCP when available, else alpaca-py."""
        if self.mcp is not None:
            return self.mcp.news(symbol, limit)
        return fetch_recent_news(self.creds, symbol, limit=limit)

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

    def order_status(self, order_id: str) -> str:
        """Lower-case broker status for one order id (``"filled"``, ``"new"``,
        ``"canceled"`` ...). A missing order / API error -> ``"gone"`` so the
        caller drops it rather than tracking it forever."""
        try:
            o = self.trading.get_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("order_status(%s) failed: %s", order_id, exc)
            return "gone"
        s = getattr(o, "status", "")
        return str(getattr(s, "value", s)).split(".")[-1].lower()

    def cancel_order(self, order_id: str) -> None:
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_order(%s) failed: %s", order_id, exc)

    def close_condor(self, condor: TrackedCondor) -> None:
        """Flatten a tracked multi-leg position with ONE reversing MLEG market
        order (see ``executor.build_close_request``). Legging out one
        ``close_position`` call at a time is what left orphan legs and got
        rejected mid-unwind ("account not eligible to trade uncovered option
        contracts"); the atomic combo can't do that.

        Fallback, only if the combo submit itself fails: close leg by leg but
        buy back every SHORT leg before selling any LONG leg, so the position is
        never transiently naked."""
        legs = [leg for leg in condor.legs if leg.symbol]
        if not legs:
            return
        try:
            from .executor import build_close_request

            self.trading.submit_order(build_close_request(legs, condor.quantity))
            return
        except Exception as exc:  # noqa: BLE001 - fall back to a safe legged close
            log.warning("close_condor: atomic MLEG close failed (%s) — legging out shorts-first", exc)

        for leg in sorted(legs, key=lambda leg: 0 if leg.action == "sell" else 1):
            try:
                self.trading.close_position(leg.symbol)
            except Exception as exc:  # noqa: BLE001
                log.error("close_condor: leg %s close failed: %s", leg.symbol, exc)

    def flatten_all(self) -> tuple[int, int]:
        """Market-close every open position and cancel every working order.
        Returns ``(legs_closed, legs_remaining)`` after a short settle."""
        import time as _t

        try:
            self.trading.cancel_orders()
        except Exception as exc:  # noqa: BLE001
            log.warning("flatten: cancel_orders failed: %s", exc)

        before = self.get_positions()
        try:
            self.trading.close_all_positions(cancel_orders=True)
        except Exception as exc:  # noqa: BLE001
            log.error("flatten: close_all_positions failed: %s", exc)
            for p in before:
                try:
                    self.trading.close_position(p.symbol)
                except Exception as exc2:  # noqa: BLE001
                    log.error("flatten: close_position %s failed: %s", p.symbol, exc2)

        for _ in range(6):
            _t.sleep(2)
            if not self.get_positions():
                break
        remaining = len(self.get_positions())
        return len(before) - remaining, remaining

    def value_condors(self, condors: list[TrackedCondor]) -> list[CondorValuation]:
        """Re-price each open position from its own ticker's near-dated chain."""
        from .alpaca_trader import build_contracts, fetch_option_chain

        out: list[CondorValuation] = []
        for c in condors:
            try:
                chain = fetch_option_chain(
                    self.creds, expiry=c.expiry, underlying=c.symbol,
                    spot=None, strike_window_pct=None,
                )
                mids = {ct.symbol: ct.mid for ct in build_contracts(chain)}
            except Exception as exc:  # noqa: BLE001
                log.error("could not price %s %s: %s", c.symbol, c.id, exc)
                continue
            cost = value_condor(c.legs, mids)
            if cost is None:
                log.warning("%s %s — missing a leg quote, skipping management",
                            c.symbol, c.id)
                continue
            out.append(CondorValuation(c, cost))
        return out

    def premarket_gaps(self, tickers) -> list[offhours.TickerGap]:
        """Current reference price vs the prior daily close for each basket
        ticker — feeds the Morning Brief. One bad ticker is skipped, not fatal."""
        from .alpaca_trader import get_daily_closes
        from .data import get_underlying_price

        out: list[offhours.TickerGap] = []
        for t in tickers:
            try:
                closes = get_daily_closes(self.creds, t, sessions=2)
                price = float(get_underlying_price(self.creds, t))
            except Exception as exc:  # noqa: BLE001
                log.error("pre-market gap for %s failed: %s", t, exc)
                continue
            if not closes:
                continue
            out.append(offhours.TickerGap(t, float(closes[-1]), price))
        return out


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
@dataclass
class CycleReport:
    decisions: list[DecisionSummary]      # one per basket ticker evaluated
    closed: list[dict]
    opened: list[dict] = field(default_factory=list)     # pending orders CONFIRMED filled this cycle
    submitted: list[dict] = field(default_factory=list)  # new orders sent this cycle (awaiting fill)

    @property
    def decision(self) -> DecisionSummary | None:
        """The most consequential decision this cycle (executed > vetoed >
        blocked > everything else), for callers that want a single line."""
        if not self.decisions:
            return None
        rank = {"executed": 5, "error": 4, "vetoed": 3, "blocked": 2,
                "halted": 1, "skipped": 0}
        return max(self.decisions, key=lambda d: rank.get(d.outcome, -1))


def _gather_market_context(conn: AlpacaConnection, config: Config,
                           now_et: datetime) -> context_gatherer.MarketContext:
    """Quantamental context pull for the cycle (IntelligenceHub: yfinance primary,
    Alpaca fallback). Fail-safe to 'No Context Available' so a data outage never
    blocks trading."""
    try:
        return intelligence_hub.gather(
            conn.creds, config.tickers, now=now_et,
            macro_danger_enabled=not config.disable_macro_danger,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("context gather failed: %s", exc)
        return context_gatherer.MarketContext.unavailable(str(exc))


def _fmt(x, spec: str) -> str:
    try:
        return format(float(x), spec)
    except (TypeError, ValueError):
        return "  n/a"


def render_scan_table(
    universe: tuple[str, ...],
    snapshots: dict[str, dict],
    scan: dict[str, "DecisionSummary"],
    *,
    min_iv_rv: float = MIN_IV_RV_SPREAD,
    er_threshold: float = RANGE_BOUND_ER,
    static_iv_floor: float = STATIC_IV_THRESHOLD,
    min_ctw: float = MIN_CREDIT_TO_WIDTH,
) -> str:
    """One line per universe symbol: price, ATM IV, RV, IV-RV (pass/fail vs
    ``min_iv_rv``), ER (pass/fail vs ``er_threshold``), the static IV floor
    (pass/fail vs ``static_iv_floor``), best credit/width (vs ``min_ctw`` when a
    condor/vertical was built), and the decision. Observability only."""
    head = (f"SCAN TABLE  (IV-RV>={min_iv_rv:+.3f}  ER<{er_threshold:.2f}  "
            f"floor>{static_iv_floor:.2f}  c/w>={min_ctw:.0%})")
    lines = [head, "-" * len(head)]
    for sym in universe:
        snap = snapshots.get(sym)
        dec = scan.get(sym)
        if snap is None:
            reason = dec.reason if dec else "no snapshot this cycle"
            lines.append(f"  {sym:<4}  {reason}")
            continue
        iv = snap.get("atm_iv")
        rv = snap.get("realized_vol")
        ivrv = snap.get("iv_rv_spread")
        er = efficiency_ratio(snap.get("daily_closes"))
        floor_ok = bool(getattr(snap.get("iv_regime"), "trade_eligible", False))
        ivrv_ok = ivrv is not None and ivrv >= min_iv_rv
        er_ok = er is not None and er < er_threshold
        plan = getattr(dec, "plan", None) if dec else None
        ctw = getattr(plan, "credit_to_width", None)
        ctw_s = f"c/w {ctw:+.0%}{'ok' if ctw is not None and ctw >= min_ctw else 'FAIL'}" \
            if ctw is not None else "c/w   -- "
        decision_s = (f"{dec.outcome} [{dec.stage}] {dec.reason}"[:72]
                      if dec else "not evaluated")
        lines.append(
            f"  {sym:<4}  px {_fmt(snap.get('current_price'), '7.2f')}  "
            f"IV {_fmt(iv, '5.3f')}  RV {_fmt(rv, '5.3f')}  "
            f"IVRV {_fmt(ivrv, '+6.3f')} {'ok  ' if ivrv_ok else 'FAIL'}  "
            f"ER {_fmt(er, '4.2f')} {'ok  ' if er_ok else 'FAIL'}  "
            f"floor {'ok  ' if floor_ok else 'FAIL'}  {ctw_s}  -> {decision_s}"
        )
    return "\n".join(lines)


def halt_file_present(path: str = "HALT") -> bool:
    """True when an operator has dropped a ``HALT`` file at the repo root: keep
    managing open positions and logging, but evaluate no new trades."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def append_audit(path: str, symbol: str, transcript: str, *, decision_line: str, when) -> None:
    """Append one Bull/Bear/Judge transcript to the final-session audit markdown.

    This is the hackathon submission evidence — the multi-agent debate reasoning
    for each trade it weighed, kept in a standalone file rather than buried in
    the activity log. Best-effort; a write failure never touches the cycle.
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fresh = not p.exists()
        with p.open("a", encoding="utf-8") as f:
            if fresh:
                f.write("# Final Session Audit — Multi-Agent Debate Transcripts\n\n")
                f.write("Bull / Bear / Judge reasoning for every ticker the agent "
                        "weighed in the final session. Generated live by the agent.\n\n")
            f.write(f"## {symbol} — {when:%Y-%m-%d %H:%M} ET\n\n")
            f.write(f"**Pipeline outcome:** {decision_line}\n\n")
            f.write("```\n" + transcript.strip() + "\n```\n\n")
    except OSError as exc:  # noqa: BLE001
        log.warning("append_audit(%s) failed: %s", path, exc)


def hard_stop_reached(now_et: datetime, hard_stop_et: str) -> bool:
    """True once ``now_et`` (an ET-aware datetime) is at or past the configured
    ``"YYYY-MM-DD HH:MM"`` ET wall-clock cutoff. Empty / unparseable config ->
    never stop (fail-open so a bad string can't strand the loop)."""
    s = (hard_stop_et or "").strip()
    if not s:
        return False
    try:
        target = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except ValueError:
        log.warning("AGENT_HARD_STOP_ET %r not 'YYYY-MM-DD HH:MM' — hard stop disabled", s)
        return False
    return now_et >= target


def _hard_stop_cycle(conn: AlpacaConnection, session: Session, config: Config,
                     now_et: datetime) -> CycleReport:
    """Competition hard stop: on first entry flatten the book and log a final
    summary; thereafter just confirm the book stays flat. No new trades, ever."""
    if not session.hard_stop_done:
        log.warning("=" * 60)
        log.warning("COMPETITION HARD STOP reached (%s ET) — flattening the book, "
                    "no further trades this session", config.hard_stop_et)
        closed, remaining = conn.flatten_all()
        session.open_condors = []
        session.pending_orders = []      # flatten_all cancels every working order
        try:
            equity = float(conn.get_account().equity)
        except Exception:  # noqa: BLE001
            equity = float("nan")
        pnl = equity - session.starting_equity
        offhours_log.info(
            "HARD STOP FINAL SUMMARY\n%s\n"
            "  cutoff            %s ET\n"
            "  legs closed       %d  (remaining: %d)\n"
            "  ending equity     $%s\n"
            "  net vs start      $%s  (start $%s)\n"
            "  new trades        REFUSED for the rest of the run\n%s",
            "=" * 60, config.hard_stop_et, closed, remaining,
            f"{equity:,.2f}", f"{pnl:+,.2f}", f"{session.starting_equity:,.2f}",
            "=" * 60,
        )
        alerts.notify("hard_stop", legs_closed=closed, remaining=remaining, equity=equity)
        if remaining:
            log.error("HARD STOP: %d position leg(s) did NOT close — retrying next cycle", remaining)
        else:
            session.hard_stop_done = True
        save_session(session, config.session_file)
        return CycleReport(decisions=[], closed=[], opened=[])

    # already flattened — keep running only to confirm the book is flat
    remaining = len(conn.get_positions())
    if remaining:
        log.error("HARD STOP ACTIVE — book NOT flat (%d leg(s)); re-flattening", remaining)
        conn.flatten_all()
    else:
        log.info("HARD STOP ACTIVE — book flat, no new trades")
    return CycleReport(decisions=[], closed=[], opened=[])


def run_cycle(conn: AlpacaConnection, session: Session, config: Config, *,
              now_et: datetime | None = None) -> CycleReport:
    now_et = now_et or datetime.now(ET)
    today = now_et.date()
    now_iso = now_et.isoformat()

    # Competition hard stop: past the cutoff, flatten + confirm flat, never trade.
    if hard_stop_reached(now_et, config.hard_stop_et):
        return _hard_stop_cycle(conn, session, config, now_et)

    acct_snap = conn.account_snapshot()
    current_equity = float(acct_snap["equity"])
    day_start_equity = float(acct_snap.get("last_equity") or current_equity)

    # 0. CONTEXTUAL INTELLIGENCE — one pull per cycle, before anything else.
    market_context = _gather_market_context(conn, config, now_et)
    ctx_str = market_context.synthesis()
    offhours_log.info("MARKET CONTEXT\n%s\n%s\n%s", "=" * 60, ctx_str, "=" * 60)
    if market_context.regime_flags():
        log.warning("REGIME SIGNALS: %s — short-vol structures will be vetoed in "
                    "favour of long-vol this cycle", ", ".join(market_context.regime_flags()))

    # Macro guard: a High-Impact event today halves gate 1's cap for this cycle.
    risk_mult = macro_risk_multiplier(macro_high_impact=market_context.macro_today_high_impact)
    if risk_mult < 1.0:
        log.warning("MACRO GUARD ACTIVE — high-impact event today; per-trade risk "
                    "cap reduced to %.0f%% for this cycle", risk_mult * 100)

    def _account() -> AccountState:
        return reconcile_account_state(
            session, current_equity=current_equity,
            day_start_equity=day_start_equity, risk_multiplier=risk_mult,
        )

    account = _account()

    # 0b. RESOLVE LAST CYCLE'S ORDERS — promote confirmed fills to open positions,
    #     drop dead orders, cancel anything still unfilled past STALE_ORDER_CYCLES.
    #     A position is only ever "open" once the broker says it filled.
    pending_result = reconcile_pending_orders(
        session, status_fn=conn.order_status, cancel_fn=conn.cancel_order,
        now_iso=now_iso,
    )
    promoted = pending_result["promoted"]
    account = _account()

    # 0c. Rebuild the open book from broker truth — drop phantom positions, adopt
    #     orphan legs (one side of a strangle that filled alone). Guarded on a
    #     non-empty position list so an API blip can never nuke the book.
    broker_positions = conn.get_positions()
    if broker_positions:
        reconcile_open_book(session, broker_positions, now_iso)
        account = _account()

    # 1. MANAGE OPEN POSITIONS FIRST — across the whole basket
    expiring = flag_expiring_positions(account.open_positions, today=today)
    expiring_ids = {ep.position.symbol for ep in expiring}
    valuations = conn.value_condors(session.open_condors)
    catalyst_date = (
        market_context.next_macro_event_date()
        if "MACRO_DANGER" in market_context.regime_flags() else None
    )
    closed = manage_open_positions(
        session, valuations, expiring_ids,
        close_fn=conn.close_condor, config=config, now_iso=now_iso,
        catalyst_date=catalyst_date,
    )

    # 2a. self-correction: ask the LLM for a "lesson learned" on each close and
    #     append it to lessons_learned.json (debate_review injects them). Best-
    #     effort — never blocks the cycle.
    if config.self_correction and closed:
        for ev in closed:
            try:
                risk_officer.post_trade_analysis(ev)
            except Exception as exc:  # noqa: BLE001
                log.warning("post_trade_analysis failed for %s: %s", ev.get("id"), exc)

    # 2. sticky halt latch (+ panic flatten on floor breach), then rebuild state
    update_sticky_halt(session, account, flatten_fn=conn.flatten_all,
                       panic_equity=config.panic_flatten_equity)
    account = _account()

    # 2b. operator HALT file, or a pre-window "not before" gate — keep managing
    #     open positions (done above) and logging, but evaluate no new trades.
    decisions: list[DecisionSummary] = []
    submitted: list[dict] = []
    pre_window = (
        config.trade_not_before_et is not None and now_et < config.trade_not_before_et
    )
    if halt_file_present(config.halt_file) or pre_window:
        why = (f"before the {config.trade_not_before_et:%Y-%m-%d %H:%M} ET trade window"
               if pre_window else f"HALT file present ({config.halt_file})")
        log.info("%s — managing open positions only, no new-trade evaluation this cycle", why)
        _accumulate_daily_activity(session, decisions, today=today,
                                   basket_size=len(config.tickers))
        save_session(session, config.session_file)
        return CycleReport(decisions=decisions, closed=closed, opened=promoted,
                           submitted=submitted)

    # 3. SCAN the universe: one narrowed snapshot per symbol, inside a per-cycle
    #    time-box (drop the slowest for this cycle so the loop still fits its
    #    interval). ``scan`` collects one DecisionSummary per symbol for the table.
    scan: dict[str, DecisionSummary] = {}
    snapshots: dict[str, dict] = {}
    scan_start = time.monotonic()
    deferred: list[str] = []
    for symbol in config.tickers:
        if time.monotonic() - scan_start > config.scan_time_box_s:
            deferred.append(symbol)
            scan[symbol] = DecisionSummary(False, "precheck", "skipped",
                                           "deferred — scan time-box", market_context=ctx_str)
            continue
        try:
            snapshots[symbol] = get_market_snapshot(symbol, creds=conn.creds)
        except Exception as exc:  # noqa: BLE001 - one ticker's data must not kill the cycle
            log.error("snapshot for %s failed: %s", symbol, exc)
            d = DecisionSummary(False, "precheck", "error",
                                f"{symbol}: snapshot failed — {exc}", market_context=ctx_str)
            decisions.append(d)
            scan[symbol] = d
    if deferred:
        log.warning("scan time-box %ds hit — deferred %d symbol(s) this cycle: %s",
                    config.scan_time_box_s, len(deferred), ", ".join(deferred))

    # Per-symbol dedup: drop tickers the agent already has exposure to BEFORE
    # ranking — a held name must not be re-selected when a fill frees a slot.
    held = held_underlyings(session)
    for symbol in sorted(set(snapshots) & held):
        d = DecisionSummary(
            False, "precheck", "skipped",
            f"already holds a position or working order in {symbol}",
            market_context=ctx_str,
        )
        decisions.append(d)
        scan[symbol] = d
        log.info("[%s] DECISION SUMMARY — Skipped at [precheck]: already holds a "
                 "position or working order", symbol)
    candidates = [s for s in snapshots if s not in held]

    # Post-filter too: a held name must never reach the eval loop even if the
    # ranker echoes it back.
    ordered = [s for s in rank_basket(candidates, snapshots, market_context)
               if s not in held]
    if ordered:
        log.info("cycle priority order: %s", " > ".join(ordered))

    # 4. evaluate in priority order; exposure stays GLOBAL (rebuild `account`
    #    after every open so the next ticker sees the updated count). The
    #    #1-ranked candidate gets the full Bull/Bear/Judge debate; the rest get
    #    the single-pass review.
    lessons = risk_officer.load_lessons() if config.debate_enabled else []

    def _debate_fn(o, s, a, timeout=None):
        return risk_officer.debate_review(o, s, a, timeout=timeout, lessons=lessons)

    # Step 5 — live intraday context for whatever reaches the officer.
    def _news_fn(sym):
        return conn.news(sym, limit=5)

    def _intraday_fn(sym):
        return intraday_realized_vol(conn.creds, sym)

    for i, symbol in enumerate(ordered):
        kw = {"news_fn": _news_fn, "intraday_vol_fn": _intraday_fn}
        if i == 0 and config.debate_enabled:
            kw["review_fn"] = _debate_fn
        decision = evaluate_cycle_decision(
            snapshots[symbol], account, config=config, today=today,
            market_context=ctx_str, context=market_context, **kw,
        )
        decisions.append(decision)
        scan[symbol] = decision
        log.info("[%s] %s", symbol, decision.render())
        if decision.debate:
            offhours_log.info("DEBATE [%s]\n%s\n%s\n%s", symbol, "=" * 60,
                              decision.debate, "=" * 60)
            if config.audit_file:
                append_audit(config.audit_file, symbol, decision.debate,
                             decision_line=decision.render(), when=now_et)

        if decision.outcome == "executed":
            submitted.append(_record_pending(session, decision, now_iso))
            account = _account()

    offhours_log.info("%s\n%s\n%s", "=" * 60,
                      render_scan_table(config.tickers, snapshots, scan), "=" * 60)

    _accumulate_daily_activity(session, decisions, today=today, basket_size=len(config.tickers))
    save_session(session, config.session_file)
    return CycleReport(decisions=decisions, closed=closed, opened=promoted,
                       submitted=submitted)


_ACTIVITY_RETAIN_DAYS = 10


def _accumulate_daily_activity(session: Session, decisions: list[DecisionSummary],
                               *, today: date, basket_size: int) -> None:
    """Fold this cycle's decisions into the day's running funnel totals (used by
    the Nightly Post-Mortem). Observability only — reads decisions, writes the
    session's ``daily_activity`` map."""
    iso = today.isoformat()
    activity = offhours.DailyActivity.from_dict(
        session.daily_activity.get(iso, {"date": iso})
    )
    activity.basket_size = basket_size
    offhours.accumulate_activity(activity, decisions)
    session.daily_activity[iso] = activity.to_dict()
    # keep the map small — only the last few days matter
    if len(session.daily_activity) > _ACTIVITY_RETAIN_DAYS:
        for stale in sorted(session.daily_activity)[:-_ACTIVITY_RETAIN_DAYS]:
            del session.daily_activity[stale]


# Submitted orders sitting unfilled longer than this many cycles are cancelled
# and the slot is freed (a mid-priced limit that hasn't filled in ~10 min won't).
STALE_ORDER_CYCLES = 2
_FILLED_STATUSES = frozenset({"filled"})
_DEAD_STATUSES = frozenset({
    "canceled", "cancelled", "expired", "rejected", "replaced",
    "done_for_day", "gone",
})


def held_underlyings(session: Session) -> set[str]:
    """Basket tickers the agent already has exposure to — an open position or a
    working (submitted) order. These are skipped by the new-trade scan."""
    return ({c.symbol for c in session.open_condors}
            | {p.symbol for p in session.pending_orders})


def _tracked_from_pending(p: PendingOrder, now_iso: str) -> TrackedCondor:
    return TrackedCondor(
        id=p.order_id, symbol=p.symbol, structure=p.structure, expiry=p.expiry,
        quantity=p.quantity, entry_credit=p.entry_credit, legs=p.legs,
        opened_at=now_iso,
    )


def reconcile_pending_orders(
    session: Session,
    *,
    status_fn,
    cancel_fn,
    now_iso: str,
    stale_after_cycles: int = STALE_ORDER_CYCLES,
) -> dict:
    """Resolve every submitted-but-unconfirmed order against the broker:

    * ``filled``                         -> promote to an open position;
    * ``canceled`` / ``rejected`` / gone -> drop from tracking;
    * still working past ``stale_after_cycles`` cycles -> cancel + drop (free the
      slot; the next cycle's pipeline can re-propose if the edge still exists);
    * status check raised                -> leave in place, try again next cycle.

    Mutates ``session``. Returns ``{"promoted": [...], "abandoned": [...],
    "stale_cancelled": [...]}`` (lists of history events)."""
    promoted: list[dict] = []
    abandoned: list[dict] = []
    stale_cancelled: list[dict] = []
    keep: list[PendingOrder] = []

    for p in session.pending_orders:
        try:
            status = str(status_fn(p.order_id) or "").strip().lower()
        except Exception as exc:  # noqa: BLE001 - a data blip must not lose the order
            log.warning("pending %s %s: status check failed (%s) — keeping",
                        p.symbol, p.order_id, exc)
            keep.append(p)
            continue

        if status in _FILLED_STATUSES:
            tc = _tracked_from_pending(p, now_iso)
            session.open_condors.append(tc)
            ev = {"kind": "opened", "at": now_iso, "id": tc.id, "symbol": tc.symbol,
                  "structure": tc.structure, "regime": None,
                  "detail": f"fill confirmed — {p.quantity}x {tc.symbol} {tc.structure}"}
            session.history.append(ev)
            promoted.append(ev)
            log.info("FILLED %s %s [%s] — promoted from pending to open position",
                     tc.symbol, tc.id, tc.structure)
            alerts.notify("trade_opened", symbol=tc.symbol, structure=tc.structure,
                          detail=ev["detail"])
        elif status in _DEAD_STATUSES:
            ev = {"kind": "order_abandoned", "at": now_iso, "id": p.order_id,
                  "symbol": p.symbol, "structure": p.structure, "reason": status}
            session.history.append(ev)
            abandoned.append(ev)
            log.warning("ORDER %s %s ended unfilled (%s) — dropped from tracking",
                        p.symbol, p.order_id, status)
        else:  # new / accepted / pending_new / partially_filled / held / ...
            p.cycles_waited += 1
            if p.cycles_waited >= stale_after_cycles:
                try:
                    cancel_fn(p.order_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("could not cancel stale order %s: %s", p.order_id, exc)
                ev = {"kind": "order_stale_cancelled", "at": now_iso,
                      "id": p.order_id, "symbol": p.symbol,
                      "structure": p.structure, "cycles": p.cycles_waited}
                session.history.append(ev)
                stale_cancelled.append(ev)
                log.warning("ORDER %s %s unfilled after %d cycle(s) — cancelled, "
                            "slot freed", p.symbol, p.order_id, p.cycles_waited)
            else:
                keep.append(p)

    session.pending_orders = keep
    return {"promoted": promoted, "abandoned": abandoned,
            "stale_cancelled": stale_cancelled}


def reconcile_open_book(session: Session, broker_positions, now_iso: str) -> dict:
    """Rebuild the tracked open book from broker truth — the belt-and-suspenders
    guarantee that phantom positions cannot persist:

    * drop any tracked position whose legs are **all** absent from the broker;
    * a position with *some* legs still live is kept but logged as a PARTIAL
      (management / the expiry gate resolve it);
    * adopt any broker option leg that neither a tracked position nor a pending
      order owns as a 1-leg ``orphan_leg`` — the expiry gate then closes it.

    Mutates ``session``. Returns ``{"dropped": [...], "adopted": [...]}``. The
    caller should only invoke this with a **non-empty** ``broker_positions`` (an
    empty list may just be an API blip — never nuke the book on that)."""
    from .alpaca_trader import parse_occ_symbol

    held: set[str] = {
        str(getattr(p, "symbol", "") or "") for p in (broker_positions or ())
    }
    held.discard("")

    dropped: list[dict] = []
    kept: list[TrackedCondor] = []
    for c in session.open_condors:
        leg_syms = {lg.symbol for lg in c.legs if lg.symbol}
        if leg_syms and leg_syms.isdisjoint(held):
            ev = {"kind": "position_dropped", "at": now_iso, "id": c.id,
                  "symbol": c.symbol, "structure": c.structure,
                  "reason": "broker holds none of its legs"}
            session.history.append(ev)
            dropped.append(ev)
            log.warning("RECONCILE — dropped %s %s [%s]: broker holds none of its "
                        "legs (phantom)", c.symbol, c.id, c.structure)
        else:
            if leg_syms and not leg_syms.issubset(held):
                log.warning("RECONCILE — %s %s is a PARTIAL: %d/%d legs live at "
                            "the broker", c.symbol, c.id,
                            len(leg_syms & held), len(leg_syms))
            kept.append(c)
    session.open_condors = kept

    owned = {lg.symbol for c in session.open_condors for lg in c.legs if lg.symbol}
    owned |= {lg.symbol for p in session.pending_orders for lg in p.legs if lg.symbol}

    adopted: list[dict] = []
    by_symbol = {str(getattr(p, "symbol", "")): p for p in (broker_positions or ())}
    for sym in sorted(held - owned):
        try:
            root, expiry, right, _strike = parse_occ_symbol(sym)
        except ValueError:
            continue                       # not an option leg this agent can manage
        raw_qty = getattr(by_symbol.get(sym), "qty", 1)
        try:
            qty = max(1, int(abs(float(raw_qty))))
        except (TypeError, ValueError):
            qty = 1
        orphan = TrackedCondor(
            id=f"orphan:{sym}", symbol=root, structure="orphan_leg", expiry=expiry,
            quantity=qty, entry_credit=0.0,
            legs=(OrderLeg("buy", right, qty, sym),), opened_at=now_iso,
        )
        session.open_condors.append(orphan)
        ev = {"kind": "orphan_adopted", "at": now_iso, "id": orphan.id,
              "symbol": root, "structure": "orphan_leg", "detail": sym}
        session.history.append(ev)
        adopted.append(ev)
        log.warning("RECONCILE — adopted orphan broker leg %s (%dx) as %s; the "
                    "expiry gate will close it", sym, qty, orphan.id)

    return {"dropped": dropped, "adopted": adopted}


def _record_pending(session: Session, decision: DecisionSummary, now_iso: str) -> dict:
    """A trade cleared every gate and the executor submitted it. Track it as
    PENDING — not open — until the broker confirms the fill (see
    :func:`reconcile_pending_orders`)."""
    plan = decision.plan
    result = decision.result
    qty = int(plan.suggested_contracts)
    symbol = getattr(plan, "symbol", None) or "SPY"
    structure = getattr(plan, "structure", "iron_condor")
    order_id = str(getattr(result, "order_id", None) or f"{symbol}-{now_iso[:19]}")
    legs = tuple(
        OrderLeg(cl.action, cl.right, qty, cl.contract.symbol) for cl in plan.legs
    )
    session.pending_orders.append(PendingOrder(
        order_id=order_id, symbol=symbol, structure=structure, expiry=plan.expiry,
        quantity=qty, entry_credit=float(plan.net_credit), legs=legs,
        submitted_at=now_iso,
    ))
    rd = decision.decision            # risk_manager.RiskDecision
    rv = decision.review              # risk_officer review
    event = {"kind": "submitted", "at": now_iso, "id": order_id, "symbol": symbol,
             "structure": structure, "regime": getattr(plan, "regime", None),
             "detail": decision.order_detail,
             "quantity": qty,
             "entry_credit": float(plan.net_credit),
             "expiry": plan.expiry.isoformat() if plan.expiry else None,
             "legs": [lg.symbol for lg in legs],
             # gate values at entry (journal)
             "gates": {
                 "iv_regime_mode": getattr(plan, "iv_regime_mode", None),
                 "underlying_price": getattr(plan, "underlying_price", None),
                 "iv_rv_spread": getattr(plan, "iv_rv_spread", None),
                 "credit_to_width": getattr(plan, "credit_to_width", None),
                 "max_loss_per_contract": getattr(plan, "max_loss_per_contract", None),
                 "order_risk": getattr(rd, "order_risk", None),
                 "max_risk_allowed": getattr(rd, "max_risk_allowed", None),
             },
             # officer verdict (journal)
             "officer": {
                 "provider": getattr(rv, "provider", None),
                 "approved": getattr(rv, "approved", None),
                 "thesis": getattr(rv, "thesis", None),
             }}
    session.history.append(event)
    log.info("SUBMITTED %s %s [%s] — pending fill — %s",
             symbol, order_id, structure, decision.order_detail)
    return event


# --------------------------------------------------------------------------- #
# Off-hours intelligence — timed, non-blocking, observability only
# --------------------------------------------------------------------------- #
def _maybe_heartbeat(
    session: Session,
    config: Config,
    *,
    now_et: datetime,
    market_open: bool,
    connectivity_ok: bool,
    iv_path: str | None = None,
) -> offhours.Heartbeat | None:
    """Emit one HEARTBEAT line per ``config.heartbeat_minutes`` — market open or
    closed — so the audit trail is continuous. Returns the heartbeat if it fired."""
    if not offhours.interval_elapsed(
        session.last_heartbeat_at, now_et,
        min_gap_seconds=config.heartbeat_minutes * 60,
    ):
        return None
    hb = offhours.build_heartbeat(
        now_et,
        market_open=market_open,
        connectivity_ok=connectivity_ok,
        iv_readings=offhours.count_iv_readings(iv_path or IV_HISTORY_PATH),
    )
    offhours_log.info("%s", hb.render())
    session.last_heartbeat_at = now_et.isoformat()
    save_session(session, config.session_file)
    return hb


def _maybe_morning_brief(
    session: Session, conn: AlpacaConnection, config: Config, *, now_et: datetime
) -> str | None:
    """Between 09:00 and 09:30 ET, once per day: scan the basket's pre-market gap
    vs the prior close and log the brief (+ a PRE-MARKET ALERT on a >gap move)."""
    et_date = now_et.date()
    if session.last_morning_brief_date == et_date.isoformat():
        return None
    if not offhours.in_morning_brief_window(now_et):
        return None
    try:
        gaps = conn.premarket_gaps(config.tickers)
    except Exception as exc:  # noqa: BLE001 - the brief must never crash the loop
        log.error("morning brief: could not fetch pre-market gaps: %s", exc)
        gaps = []
    text = offhours.morning_brief_text(gaps, et_date=et_date, threshold=config.gap_alert_pct)
    offhours_log.info("MORNING BRIEF\n%s\n%s\n%s", "=" * 60, text, "=" * 60)
    session.last_morning_brief_date = et_date.isoformat()
    save_session(session, config.session_file)
    return text


def _maybe_post_mortem(
    session: Session, conn: AlpacaConnection, config: Config, *, now_et: datetime
) -> str | None:
    """At/after 16:00 ET, once per day: synthesise the day's pipeline funnel,
    open-position unrealized P&L and dominant regime into a shareable digest."""
    et_date = now_et.date()
    iso = et_date.isoformat()
    if session.last_post_mortem_date == iso:
        return None
    if now_et.time() < MARKET_CLOSE_ET:
        return None

    activity = offhours.DailyActivity.from_dict(
        session.daily_activity.get(iso, {"date": iso, "basket_size": len(config.tickers)})
    )
    open_n = len(session.open_condors)
    unrealized: float | None = 0.0
    if open_n:
        unrealized = None
        try:
            vals = conn.value_condors(session.open_condors)
            if vals:
                unrealized = round(sum(v.total_pnl for v in vals), 2)
        except Exception as exc:  # noqa: BLE001
            log.error("post-mortem: could not value open positions: %s", exc)

    text = offhours.post_mortem_text(
        activity, et_date=et_date, open_positions=open_n, unrealized_pnl=unrealized
    )
    offhours_log.info("NIGHTLY POST-MORTEM\n%s\n%s\n%s", "=" * 60, text, "=" * 60)
    session.last_post_mortem_date = iso
    save_session(session, config.session_file)
    return text


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


def attach_mcp(conn: "AlpacaConnection", config: Config) -> None:
    """Try the Alpaca MCP server for the per-cycle account + news reads.

    Optional by design: if it is disabled, the SDK is missing, or the server will
    not start, ``conn.mcp`` stays ``None`` and every read is served by alpaca-py.
    The chosen path is logged at startup and again per call.
    """
    if not config.mcp_enabled:
        log.info("MCP disabled (AGENT_MCP=off) — all reads served by alpaca-py")
        return
    # The server reads credentials from its OWN environment, so hand it the same
    # ones the agent uses (merged over os.environ so uv/PATH still resolve).
    server_env = {
        **os.environ,
        "ALPACA_API_KEY": conn.creds.api_key,
        "ALPACA_SECRET_KEY": conn.creds.secret_key,
        "ALPACA_PAPER_TRADE": "true" if conn.creds.paper else "false",
    }
    session = mcp_client.connect_session(
        command="uv",
        args=["run", "--directory", config.mcp_server_dir, "alpaca-mcp-server"],
        cwd=config.mcp_server_dir,
        env=server_env,
    )
    conn.mcp = mcp_client.MCPBridge(
        session=session,
        account_fallback=lambda: {
            "equity": float(conn.get_account().equity),
            "last_equity": float(
                getattr(conn.get_account(), "last_equity", None)
                or conn.get_account().equity
            ),
        },
        news_fallback=lambda sym, limit: fetch_recent_news(conn.creds, sym, limit=limit),
    )
    log.info("Alpaca read path — %s", conn.mcp.describe())


def startup(config: Config) -> tuple[AlpacaConnection, Session]:
    setup_logging(config.log_level, config.log_file, config.activity_log_file)
    load_env_file(config.env_file)

    conn = AlpacaConnection()
    attach_mcp(conn, config)
    acct = conn.get_account()
    account_id = str(getattr(acct, "account_number", None) or getattr(acct, "id", ""))
    live_equity = float(acct.equity)

    session = load_or_init_session(
        config.session_file, account_id=account_id, live_equity=live_equity
    )

    risk_officer.warm_up()

    log.info(
        "STARTUP %s — account %s — starting_equity $%s — current equity $%s — "
        "basket %s — loop every %ds — heartbeat every %dm — activity log %s",
        datetime.now(ET).isoformat(), account_id, f"{session.starting_equity:,.2f}",
        f"{live_equity:,.2f}", ",".join(config.tickers), config.loop_interval_s,
        config.heartbeat_minutes, config.activity_log_file,
    )

    # Narrative marker for the submission report: what mode this run is in.
    mode_bits = ["atomic MLEG close (no more leg-by-leg unwinds)"]
    if config.trade_not_before_et:
        mode_bits.append(f"new trades gated until {config.trade_not_before_et:%Y-%m-%d %H:%M} ET")
    if config.disable_macro_danger:
        mode_bits.append("MACRO_DANGER override OFF")
    if config.harvest_mode:
        m = config.harvest_sentiment_min
        mode_bits.append(
            f"HARVEST directional mode ON on {', '.join(config.tickers)} — news score > +{m} "
            f"forces a bull put, < -{m} a bear call, "
            f"{HARVEST_SHORT_DELTA:.2f}-delta short / ${HARVEST_SPREAD_WIDTH:.0f}-wide"
        )
    if config.stop_loss_max_loss_fraction:
        mode_bits.append(
            f"exits: TP {config.profit_target_fraction:.0%} of credit / "
            f"stop {config.stop_loss_max_loss_fraction:.0%} of max loss"
        )
    if config.panic_flatten_equity:
        mode_bits.append(f"panic flatten + halt at ${config.panic_flatten_equity:,.0f} equity")
    offhours_log.info(
        "RUN MODE\n%s\n  hard stop  %s ET\n  %s\n%s",
        "=" * 60, config.hard_stop_et, "\n  ".join(mode_bits), "=" * 60,
    )
    return conn, session


def run_forever(config: Config | None = None) -> None:
    config = config or Config.from_env()
    conn, session = startup(config)
    prev_is_open = False

    while True:
        now_et = datetime.now(ET)
        clock = None
        connectivity_ok = True
        try:
            clock = conn.get_clock()
        except KeyboardInterrupt:
            log.info("interrupted — shutting down")
            return
        except Exception as exc:  # noqa: BLE001 - still emit a heartbeat so the trail is unbroken
            connectivity_ok = False
            log.exception("get_clock failed — emitting an Error heartbeat: %s", exc)

        try:
            market_open = bool(clock and clock.is_open)
            if market_open:
                run_cycle(conn, session, config, now_et=clock.timestamp.astimezone(ET))
            elif clock is not None:
                log.info("market closed — next open %s", getattr(clock, "next_open", "?"))

            # Off-hours intelligence — runs every loop, gated internally by time.
            # Cheap no-ops until each behaviour is actually due; never blocks a cycle.
            _maybe_heartbeat(session, config, now_et=now_et,
                             market_open=market_open, connectivity_ok=connectivity_ok)
            if clock is not None:
                _maybe_morning_brief(session, conn, config, now_et=now_et)
                _maybe_daily_summary(session, conn, clock, config, prev_is_open=prev_is_open)
                _maybe_post_mortem(session, conn, config, now_et=now_et)
                prev_is_open = clock.is_open
        except KeyboardInterrupt:
            log.info("interrupted — shutting down")
            return
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not crash the loop
            log.exception("cycle failed (continuing next cycle): %s", exc)
            alerts.notify("cycle_error", error=str(exc))

        time.sleep(config.loop_interval_s)


def main(argv: list[str] | None = None) -> int:
    load_env_early()
    run_forever(Config.from_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
