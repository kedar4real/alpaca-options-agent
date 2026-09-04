"""
live.py - read-only broker access for the dashboard.

This module deliberately exposes *only* Alpaca's ``get_*`` calls. It never
constructs an order, never cancels one, and never closes a position. The
dashboard is an observer; the trading agent is the only thing permitted to act
on the account.

Every call is fail-safe: if the broker is unreachable the dashboard still
renders from the agent's on-disk files rather than erroring out.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("dashboard.live")

# Cache broker reads briefly so a dashboard left open on a 5s poll does not
# hammer the same API the trading agent depends on.
CACHE_TTL_S = 10.0


def load_creds(env_path: Path) -> tuple[str, str, bool]:
    """Read Alpaca credentials from the agent's .env.

    Secrets stay in this process - they are never placed in an API response.
    """
    key = secret = ""
    paper = True
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if name == "ALPACA_API_KEY" and not key:
                key = value
            elif name == "ALPACA_SECRET_KEY" and not secret:
                secret = value
            elif name == "ALPACA_PAPER_TRADE":
                paper = value.lower() != "false"
    return (
        os.environ.get("ALPACA_API_KEY", key),
        os.environ.get("ALPACA_SECRET_KEY", secret),
        paper,
    )


class LiveClient:
    """Thin read-only facade over ``alpaca-py``'s TradingClient."""

    def __init__(self, env_path: Path):
        self._key, self._secret, self._paper = load_creds(env_path)
        self._client = None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._key and self._secret)

    def _get_client(self):
        if self._client is None and self.configured:
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(self._key, self._secret, paper=self._paper)
        return self._client

    def _cached(self, name: str, fn, default):
        """Run ``fn`` at most once per CACHE_TTL_S; fall back to ``default``."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(name)
            if hit and now - hit[0] < CACHE_TTL_S:
                return hit[1]
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - the dashboard must never crash
            log.warning("broker read %s failed: %s", name, exc)
            stale = self._cache.get(name)
            return stale[1] if stale else default
        with self._lock:
            self._cache[name] = (now, value)
        return value

    # -- reads ------------------------------------------------------------- #
    def account(self) -> dict:
        def _read():
            c = self._get_client()
            if c is None:
                return {}
            a = c.get_account()
            return {
                "account_id": str(getattr(a, "id", "")),
                "account_number": getattr(a, "account_number", ""),
                "status": str(getattr(a, "status", "")),
                "equity": float(getattr(a, "equity", 0) or 0),
                "last_equity": float(getattr(a, "last_equity", 0) or 0),
                "cash": float(getattr(a, "cash", 0) or 0),
                "buying_power": float(getattr(a, "buying_power", 0) or 0),
                "paper": self._paper,
            }

        return self._cached("account", _read, {})

    def positions(self) -> list[dict]:
        """Live legs as the broker sees them - the source of truth for P&L."""

        def _read():
            c = self._get_client()
            if c is None:
                return []
            out = []
            for p in c.get_all_positions():
                out.append({
                    "symbol": getattr(p, "symbol", ""),
                    "qty": float(getattr(p, "qty", 0) or 0),
                    "side": str(getattr(p, "side", "")),
                    "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                    "current_price": float(getattr(p, "current_price", 0) or 0),
                    "market_value": float(getattr(p, "market_value", 0) or 0),
                    "cost_basis": float(getattr(p, "cost_basis", 0) or 0),
                    "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                    "unrealized_plpc": float(getattr(p, "unrealized_plpc", 0) or 0),
                    "asset_class": str(getattr(p, "asset_class", "")),
                })
            return out

        return self._cached("positions", _read, [])

    def clock(self) -> dict:
        def _read():
            c = self._get_client()
            if c is None:
                return {}
            k = c.get_clock()
            return {
                "is_open": bool(getattr(k, "is_open", False)),
                "timestamp": str(getattr(k, "timestamp", "")),
                "next_open": str(getattr(k, "next_open", "")),
                "next_close": str(getattr(k, "next_close", "")),
            }

        return self._cached("clock", _read, {})

    def equity_curve(self, period: str = "1W", timeframe: str = "15Min") -> list[dict]:
        """Account equity over time, for the headline chart."""

        def _read():
            c = self._get_client()
            if c is None:
                return []
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            h = c.get_portfolio_history(
                GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
            )
            stamps = list(getattr(h, "timestamp", []) or [])
            equity = list(getattr(h, "equity", []) or [])
            pl = list(getattr(h, "profit_loss", []) or [])
            out = []
            for i, ts in enumerate(stamps):
                eq = equity[i] if i < len(equity) else None
                if eq is None:
                    continue
                out.append({
                    "t": int(ts),
                    "equity": float(eq),
                    "pl": float(pl[i]) if i < len(pl) and pl[i] is not None else 0.0,
                })
            return out

        return self._cached(f"equity:{period}:{timeframe}", _read, [])
