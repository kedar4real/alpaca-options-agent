"""
alerts.py — optional Discord notifications for the trading agent.

Set ``AGENT_DISCORD_WEBHOOK`` to a Discord webhook URL and the agent posts a
one-line message on each of five events:

  trade_opened   a submitted order was confirmed filled
  trade_closed   a position was closed (with realized P&L and the trigger)
  halt           the sticky competition drawdown halt latched
  hard_stop      the competition hard stop flattened the book
  cycle_error    a cycle raised (the loop continues; this just surfaces it)

Design rule: alerts are strictly observational. With no webhook configured this
module is a no-op, and a failing POST is swallowed and logged — a Discord outage
must never affect a trade. ``notify`` therefore always returns a bool and never
raises.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("alerts")

WEBHOOK_ENV = "AGENT_DISCORD_WEBHOOK"
MAX_CONTENT = 1900          # Discord caps `content` at 2000 chars; leave headroom
POST_TIMEOUT_S = 5.0


def _money(x) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return "?"


def _signed_money(x) -> str:
    try:
        return f"{float(x):+,.0f}".replace("+", "+$", 1).replace("-", "-$", 1)
    except (TypeError, ValueError):
        return "$?"


def format_message(kind: str, **f) -> str:
    """One line per event kind. Unknown kinds return ``""`` (caller stays quiet)."""
    if kind == "trade_opened":
        return (f"🟢 OPENED {f.get('symbol', '?')} [{f.get('structure', '?')}] "
                f"— {f.get('detail', '')}".strip())[:MAX_CONTENT]
    if kind == "trade_closed":
        return (f"🔴 CLOSED {f.get('symbol', '?')} [{f.get('structure', '?')}] "
                f"— {f.get('reason', '?')} — P&L {_signed_money(f.get('pnl'))}")[:MAX_CONTENT]
    if kind == "halt":
        return (f"⛔ TRADING HALTED — {f.get('reason', 'risk limit breached')}")[:MAX_CONTENT]
    if kind == "hard_stop":
        return (f"🏁 COMPETITION HARD STOP — flattened {f.get('legs_closed', '?')} leg(s), "
                f"{f.get('remaining', '?')} remaining — ending equity "
                f"${_money(f.get('equity'))}")[:MAX_CONTENT]
    if kind == "cycle_error":
        return (f"⚠️ CYCLE ERROR — {f.get('error', 'unknown')}")[:MAX_CONTENT]
    return ""


def _default_poster(url: str, content: str) -> bool:
    import json
    import urllib.request

    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        # Discord rejects the stock "Python-urllib/x.y" User-Agent with a 403.
        headers={
            "Content-Type": "application/json",
            "User-Agent": "trading-agent/1.0 (+https://github.com/alpaca-hackathon)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as resp:
        return 200 <= resp.status < 300


def notify(kind: str, *, poster=None, webhook: str | None = None, **fields) -> bool:
    """Post one alert. Returns True only if a message was actually delivered.

    Silent (returns False) when: no webhook is configured, the kind is unknown,
    or the POST raises. Never propagates an exception.
    """
    content = format_message(kind, **fields)
    if not content:
        return False
    url = webhook or os.environ.get(WEBHOOK_ENV, "")
    if not url.strip():
        return False
    try:
        return bool((poster or _default_poster)(url, content))
    except Exception as exc:  # noqa: BLE001 - an alert must never break a cycle
        log.warning("discord alert (%s) failed: %s", kind, exc)
        return False
