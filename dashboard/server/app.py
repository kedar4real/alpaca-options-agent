"""
app.py - the dashboard's read-only JSON API.

Serves what the trading agent has already written to disk, plus live account
and position reads from Alpaca. There is no endpoint here that mutates
anything: no order placement, no position closing, no writes to session.json
or the agent's logs. The dashboard cannot perturb a running trading loop.

Run it:

    .venv/Scripts/python -m uvicorn app:app --port 8787 --reload
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import readers
from live import LiveClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dashboard")

# The agent's working directory - overridable so this can point at a copy.
ROOT = Path(os.environ.get("AGENT_ROOT", r"C:\alpaca-hackathon\trading-agent"))

SESSION = ROOT / "session.json"
AGENT_LOG = ROOT / "logs" / "agent.log"
ACTIVITY_LOG = ROOT / "logs" / "agent_activity.log"
IV_CSV = ROOT / "iv_history.csv"
LESSONS = ROOT / "lessons_learned.json"
ENV = ROOT / ".env"

# A cycle is 300s; treat the agent as stalled after two missed cycles.
STALE_AFTER_S = 660

app = FastAPI(title="Trading Agent Dashboard", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

client = LiveClient(ENV)


# --------------------------------------------------------------------------- #
# file helpers - every read is best-effort
# --------------------------------------------------------------------------- #
def _text(path: Path, *, tail_bytes: int | None = None) -> str:
    try:
        if tail_bytes and path.stat().st_size > tail_bytes:
            with path.open("rb") as f:
                f.seek(-tail_bytes, os.SEEK_END)
                return f.read().decode("utf-8", errors="replace")
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _session() -> dict:
    return _json(SESSION, {})


def _hard_stop_et() -> str:
    for raw in _text(ENV).splitlines():
        if raw.strip().startswith("AGENT_HARD_STOP_ET="):
            return raw.split("=", 1)[1].strip()
    return "2026-09-04 10:30"


def _agent_health() -> dict:
    """Liveness inferred from how recently the agent touched its log."""
    try:
        age = datetime.now().timestamp() - AGENT_LOG.stat().st_mtime
    except OSError:
        return {"alive": False, "last_log_age_s": None, "last_log_at": None}
    return {
        "alive": age < STALE_AFTER_S,
        "last_log_age_s": round(age, 1),
        "last_log_at": datetime.fromtimestamp(
            AGENT_LOG.stat().st_mtime
        ).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/state")
def state():
    """Headline status: account, equity, market, hard stop, concentration."""
    session = _session()
    account = client.account()
    equity = account.get("equity") or 0.0
    starting = float(session.get("starting_equity") or 0.0)
    positions = list(session.get("open_condors") or [])

    hard_stop = _hard_stop_et()
    seconds_left = None
    try:
        # ET is UTC-4 during the competition window (EDT).
        stop = datetime.strptime(hard_stop, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone(timedelta(hours=-4))
        )
        seconds_left = round((stop - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        pass

    return {
        "account_id": session.get("account_id") or account.get("account_id", ""),
        "paper": account.get("paper", True),
        "equity": equity,
        "starting_equity": starting,
        "pnl": round(equity - starting, 2) if equity and starting else 0.0,
        "pnl_pct": round((equity - starting) / starting, 6) if starting else 0.0,
        "cash": account.get("cash", 0.0),
        "buying_power": account.get("buying_power", 0.0),
        "trading_halted": bool(session.get("trading_halted")),
        "hard_stop_et": hard_stop,
        "hard_stop_done": bool(session.get("hard_stop_done")),
        "hard_stop_seconds_left": seconds_left,
        "open_position_count": len(positions),
        "pending_order_count": len(session.get("pending_orders") or []),
        "exposure": readers.long_vol_exposure(positions, equity=equity or starting or 1.0),
        "market": client.clock(),
        "agent": _agent_health(),
        "daily_activity": session.get("daily_activity") or {},
    }


@app.get("/api/positions")
def positions():
    """Tracked structures, each matched to its live broker legs."""
    session = _session()
    broker = {p["symbol"]: p for p in client.positions()}

    out = []
    for pos in session.get("open_condors") or []:
        legs = []
        pl = 0.0
        cost = 0.0
        matched = 0
        for leg in pos.get("legs") or []:
            occ = leg.get("symbol", "")
            live = broker.get(occ)
            if live:
                matched += 1
                pl += live["unrealized_pl"]
                cost += abs(live["cost_basis"])
            legs.append({
                **leg,
                "current_price": live["current_price"] if live else None,
                "market_value": live["market_value"] if live else None,
                "unrealized_pl": live["unrealized_pl"] if live else None,
                "at_broker": bool(live),
            })

        qty = float(pos.get("quantity") or 0)
        credit = float(pos.get("entry_credit") or 0.0)
        out.append({
            "id": pos.get("id", ""),
            "symbol": pos.get("symbol", ""),
            "structure": pos.get("structure", ""),
            "expiry": pos.get("expiry", ""),
            "quantity": qty,
            "entry_credit": credit,
            "entry_dollars": round(abs(credit) * qty * 100, 2),
            "opened_at": pos.get("opened_at", ""),
            "peak_gain_fraction": pos.get("peak_gain_fraction", 0.0),
            "unrealized_pl": round(pl, 2),
            "unrealized_pct": round(pl / cost, 6) if cost else 0.0,
            "legs_matched": matched,
            "legs_expected": len(pos.get("legs") or []),
            "legs": legs,
        })

    return {"positions": out, "broker_leg_count": len(broker)}


@app.get("/api/decisions")
def decisions(limit: int = 120):
    """Recent per-ticker decisions plus the stage funnel they fell out at."""
    text = _text(AGENT_LOG, tail_bytes=600_000)
    rows = readers.parse_decisions(text, newest_first=True)
    return {
        "decisions": rows[:limit],
        "funnel": readers.decision_funnel(rows),
        "orders": readers.parse_orders(text, newest_first=True)[:30],
        "total": len(rows),
    }


@app.get("/api/scan")
def scan():
    """The latest 12-ticker basket scan with per-gate pass/fail."""
    return readers.parse_scan_table(_text(ACTIVITY_LOG, tail_bytes=300_000))


@app.get("/api/signals")
def signals():
    """Macro event, VIX term structure, correlation clusters, RSI/ADX, news."""
    return readers.parse_signals(_text(ACTIVITY_LOG, tail_bytes=300_000))


@app.get("/api/equity")
def equity(period: str = "1W", timeframe: str = "15Min"):
    session = _session()
    return {
        "points": client.equity_curve(period, timeframe),
        "starting_equity": session.get("starting_equity"),
    }


@app.get("/api/iv")
def iv(symbols: str = ""):
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()] or None
    return {"series": readers.iv_series(_text(IV_CSV), wanted)}


@app.get("/api/journal")
def journal():
    """Entry/exit events from the agent's own session history."""
    return {"history": list(reversed(_session().get("history") or []))}


@app.get("/api/lessons")
def lessons(limit: int = 40):
    """Post-trade lessons the risk officer wrote after each close."""
    rows = list(reversed(_json(LESSONS, {}).get("lessons") or []))
    return {"lessons": rows[:limit], "total": len(rows)}
