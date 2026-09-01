"""
context_gatherer.py — Contextual Intelligence & Macro-Filter layer.

The quantitative pipeline (data -> strategy -> risk_manager -> risk_officer ->
executor) only sees option chains and price history. This module bolts on the
context an institutional desk would glance at before putting on a short-premium
trade:

  * Macro Event Guard  — is a High-Impact event (FOMC / CPI / NFP) inside the
                         next 48h, or landing *today*?  Uses a bundled static
                         calendar (the Fed/BLS publish these a year ahead); swap
                         ``calendar=`` / ``calendar_fn`` for a live feed later.
  * Volatility Surface — a VIX proxy: the VIXY short-term VIX-futures ETF level
                         and its 5-session change (the true ^VIX index is not on
                         Alpaca's feed).
  * Ticker News        — the top few recent headlines per basket ticker, via the
                         Alpaca News API (already a project dependency).
  * Market Internals   — a Wilder 14-day RSI per ticker, computed with numpy from
                         the same daily closes ``data.py`` already pulls.
  * Synthesis          — one unified context string for the risk_officer prompt
                         and ``agent_activity.log``.

Every pull fails safe: any part that errors degrades to its own "unavailable"
marker; a total wipe-out yields :meth:`MarketContext.unavailable`, whose
``synthesis()`` is the literal string ``"No Context Available"``. Nothing here
raises into the trade loop, and nothing here can *loosen* a risk limit — the
macro guard only ever tightens gate 1 (see ``risk_manager.macro_risk_multiplier``).

No dependency beyond numpy + alpaca-py (both already required).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np

log = logging.getLogger("agent.context")

# --- tunables --------------------------------------------------------------- #
MACRO_HORIZON_HOURS = 48
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
VIX_PROXY_SYMBOL = "VIXY"          # short-term VIX-futures ETF (proxy for ^VIX)
VIX_SPIKE_5D_PCT = 10.0           # +this much over ~5 sessions => "possibly spiking"
NEWS_LOOKBACK_DAYS = 5
HEADLINES_PER_TICKER = 4
HEADLINE_MAX_CHARS = 180          # some wires concatenate a whole bulletin into one headline
NO_CONTEXT = "No Context Available"

# keyword sentiment — crude but dependency-free
_BULLISH = {
    "surge", "surges", "surged", "rally", "rallies", "rallied", "jump", "jumps",
    "jumped", "gain", "gains", "gained", "beat", "beats", "upgrade", "upgraded",
    "record", "soar", "soars", "soared", "tops", "boost", "boosted", "optimism",
    "strong", "strength", "bullish", "outperform", "rebound", "climb", "climbs",
}
_BEARISH = {
    "miss", "misses", "missed", "plunge", "plunges", "plunged", "fall", "falls",
    "fell", "drop", "drops", "dropped", "selloff", "sell-off", "downgrade",
    "downgraded", "warn", "warns", "warned", "warning", "slump", "rout", "fear",
    "fears", "weak", "weakness", "cut", "cuts", "crash", "tumble", "tumbles",
    "sink", "sinks", "bearish", "underperform", "recession",
}


# --------------------------------------------------------------------------- #
# Macro calendar (static; see module docstring)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MacroEvent:
    date: date
    name: str
    impact: str = "High"


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _build_calendar() -> tuple[MacroEvent, ...]:
    """The bundled 2026 High-Impact schedule — FOMC decisions + CPI releases +
    the monthly Employment Situation (NFP). Verify against the Fed / BLS each
    year and extend; ``gather_context`` simply reports nothing upcoming once the
    clock runs past the last entry."""
    fomc = [date(2026, m, d) for m, d in
            [(1, 28), (3, 18), (4, 29), (6, 17), (7, 29), (9, 16), (10, 28), (12, 16)]]
    cpi = [date(2026, m, d) for m, d in
           [(1, 13), (2, 11), (3, 11), (4, 10), (5, 12), (6, 10),
            (7, 15), (8, 12), (9, 11), (10, 13), (11, 12), (12, 10)]]
    nfp = [_first_friday(2026, m) for m in range(1, 13)]
    events = (
        [MacroEvent(d, "FOMC rate decision") for d in fomc]
        + [MacroEvent(d, "CPI release") for d in cpi]
        + [MacroEvent(d, "Employment Situation (NFP)") for d in nfp]
    )
    return tuple(sorted(events, key=lambda e: e.date))


HIGH_IMPACT_CALENDAR: tuple[MacroEvent, ...] = _build_calendar()


def _as_utc_date(now: datetime) -> date:
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(timezone.utc).date()


def upcoming_high_impact(
    now: datetime, *, horizon_hours: int = MACRO_HORIZON_HOURS, calendar=HIGH_IMPACT_CALENDAR
) -> list[MacroEvent]:
    """High-impact events whose date falls between ``now`` and ``now + horizon``.
    Events are dated (no wall-clock time), so an event date is "in" the window if
    it is >= today and its 00:00 is within ``horizon_hours`` of ``now``."""
    start = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    end = start + timedelta(hours=horizon_hours)
    out = []
    for e in calendar:
        ev_dt = datetime(e.date.year, e.date.month, e.date.day, tzinfo=timezone.utc)
        if start <= ev_dt <= end or (e.date == _as_utc_date(start) and ev_dt <= end):
            out.append(e)
    return out


def high_impact_today(now: datetime, *, calendar=HIGH_IMPACT_CALENDAR) -> bool:
    today = _as_utc_date(now)
    return any(e.date == today for e in calendar)


# --------------------------------------------------------------------------- #
# Market internals — Wilder RSI
# --------------------------------------------------------------------------- #
def wilder_rsi(closes, period: int = RSI_PERIOD) -> float | None:
    """Classic Wilder 14-period RSI from a close series (oldest first).

    Returns None if there are fewer than ``period + 1`` closes. A series with no
    downward moves -> 100.0; none upward -> 0.0; perfectly flat -> 50.0."""
    c = np.asarray([x for x in (closes or []) if x is not None], dtype=float)
    if c.size < period + 1:
        return None
    delta = np.diff(c)
    gain = np.clip(delta, 0.0, None)
    loss = np.clip(-delta, 0.0, None)

    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    for i in range(period, delta.size):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period

    if avg_loss == 0.0 and avg_gain == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def classify_rsi(rsi: float | None) -> str:
    if rsi is None:
        return "n/a"
    if rsi >= RSI_OVERBOUGHT:
        return "overbought"
    if rsi <= RSI_OVERSOLD:
        return "oversold"
    return "neutral"


# --------------------------------------------------------------------------- #
# Headline sentiment
# --------------------------------------------------------------------------- #
def _words(text: str):
    return {w.strip(".,:;!?\"'()[]").lower() for w in (text or "").split()}


def score_headlines(headlines) -> int:
    """Net keyword sentiment across the headlines: +1 per bullish word hit,
    -1 per bearish. Coarse, dependency-free, only used as a tiebreak."""
    score = 0
    for h in headlines or []:
        w = _words(h)
        score += len(w & _BULLISH) - len(w & _BEARISH)
    return score


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TickerContext:
    symbol: str
    rsi: float | None
    rsi_state: str
    headlines: tuple[str, ...]
    news_score: int


@dataclass(frozen=True)
class MarketContext:
    as_of: str
    macro_events: tuple[MacroEvent, ...]
    macro_today_high_impact: bool
    vix_proxy: float | None
    vix_change_5d_pct: float | None
    vix_note: str
    tickers: tuple[TickerContext, ...]
    ok: bool
    errors: tuple[str, ...] = ()
    # -- quantamental regime signals (populated by intelligence_hub) -------- #
    vix: float | None = None              # ^VIX, 1-month
    vxv: float | None = None              # ^VIX3M / ^VXV, 3-month
    vix_vxv_ratio: float | None = None    # > 1.0 => backwardation
    panic_regime: bool = False            # VIX term structure inverted
    macro_danger: bool = False            # High-Impact ("Red Folder") event within 48h

    # -- lookups ------------------------------------------------------------- #
    def ticker(self, symbol: str) -> TickerContext | None:
        for t in self.tickers:
            if t.symbol == symbol:
                return t
        return None

    def regime_flags(self) -> list[str]:
        flags = []
        if self.macro_danger:
            flags.append("MACRO_DANGER")
        if self.panic_regime:
            flags.append("PANIC_REGIME")
        return flags

    @classmethod
    def unavailable(cls, reason: str = "context fetch failed") -> "MarketContext":
        return cls(
            as_of=datetime.now(timezone.utc).isoformat(),
            macro_events=(),
            macro_today_high_impact=False,     # fail-safe: never trips the macro guard
            vix_proxy=None,
            vix_change_5d_pct=None,
            vix_note="unavailable",
            tickers=(),
            ok=False,
            errors=(reason,),
            vix=None, vxv=None, vix_vxv_ratio=None,
            panic_regime=False, macro_danger=False,   # fail-safe: quant-only logic
        )

    # -- synthesis --------------------------------------------------------- #
    def _macro_str(self) -> str:
        if not self.macro_events:
            base = "none in next 48h"
        else:
            base = "; ".join(f"{e.name} ({e.date:%Y-%m-%d})" for e in self.macro_events)
        if self.macro_today_high_impact:
            base = f"HIGH-IMPACT EVENT TODAY -> {base}"
        return base

    def _vix_str(self) -> str:
        if self.vix is not None and self.vxv is not None:
            struct = "BACKWARDATION" if (self.vix_vxv_ratio or 0) > 1.0 else "contango"
            return (f"VIX {self.vix:.2f} / VXV {self.vxv:.2f} "
                    f"(ratio {self.vix_vxv_ratio:.2f}, {struct})")
        if self.vix_proxy is None:
            return "unavailable"
        chg = "" if self.vix_change_5d_pct is None else f", {self.vix_change_5d_pct:+.1f}% 5d"
        return f"{self.vix_proxy:.2f} (VIXY proxy{chg}; {self.vix_note})"

    def synthesis(self) -> str:
        """One-line: ``Macro: ... | VIX: ... | News SYM: ... | RSI SYM: ... | ...``."""
        if not self.ok:
            return NO_CONTEXT
        parts = [f"Macro: {self._macro_str()}", f"VIX: {self._vix_str()}"]
        if self.regime_flags():
            parts.append("REGIME SIGNALS: " + ", ".join(self.regime_flags()))
        for t in self.tickers:
            heads = "; ".join(t.headlines) if t.headlines else "(unavailable)"
            rsi = "n/a" if t.rsi is None else f"{t.rsi:.1f}"
            parts.append(f"News {t.symbol}: {heads}")
            parts.append(f"RSI {t.symbol}: {rsi} ({t.rsi_state})")
        return " | ".join(parts)


# --------------------------------------------------------------------------- #
# Fetchers (each swallows its own errors; injectable for tests)
# --------------------------------------------------------------------------- #
def fetch_vix_proxy(creds, *, client=None) -> tuple[float | None, float | None, str]:
    """VIXY latest trade + ~5-session % change. Returns (level, change_pct, note)."""
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
    from alpaca.data.timeframe import TimeFrame

    c = client or StockHistoricalDataClient(creds.api_key, creds.secret_key)
    sym = VIX_PROXY_SYMBOL
    level = float(c.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym))[sym].price)

    change = None
    try:
        start = datetime.now(timezone.utc) - timedelta(days=12)
        bars = c.get_stock_bars(
            StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=start)
        ).data.get(sym, [])
        if len(bars) >= 6 and bars[-6].close:
            change = round((level - bars[-6].close) / bars[-6].close * 100.0, 2)
    except Exception as exc:  # noqa: BLE001
        log.debug("vix proxy 5d change unavailable: %s", exc)

    if change is None:
        note = "level only"
    elif change >= VIX_SPIKE_5D_PCT:
        note = "elevated / possibly spiking"
    elif change <= -VIX_SPIKE_5D_PCT:
        note = "falling"
    else:
        note = "calm"
    return level, change, note


def fetch_headlines(creds, symbols, *, per: int = HEADLINES_PER_TICKER, client=None) -> dict[str, list[str]]:
    """Top ``per`` recent headlines per symbol via the Alpaca News API."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    c = client or NewsClient(creds.api_key, creds.secret_key)
    start = datetime.now(timezone.utc) - timedelta(days=NEWS_LOOKBACK_DAYS)
    out: dict[str, list[str]] = {}
    for s in symbols:
        try:
            res = c.get_news(NewsRequest(symbols=s, start=start, limit=max(per, 4) * 3))
            items = (getattr(res, "data", {}) or {}).get("news", []) or []
            seen, heads = set(), []
            for it in items:
                h = (getattr(it, "headline", "") or "").strip()
                if len(h) > HEADLINE_MAX_CHARS:
                    h = h[:HEADLINE_MAX_CHARS - 1].rstrip() + "…"
                if h and h not in seen:
                    seen.add(h)
                    heads.append(h)
                if len(heads) >= per:
                    break
            out[s] = heads
        except Exception as exc:  # noqa: BLE001
            log.debug("news for %s unavailable: %s", s, exc)
            out[s] = []
    return out


def fetch_closes_map(creds, symbols, *, sessions: int = 30, closes_fn=None) -> dict[str, list[float]]:
    """{symbol: [daily closes oldest-first]} for the RSI, via alpaca_trader."""
    if closes_fn is None:
        from .alpaca_trader import get_daily_closes

        def closes_fn(sym):  # noqa: E306
            return get_daily_closes(creds, sym, sessions=sessions)

    out: dict[str, list[float]] = {}
    for s in symbols:
        try:
            out[s] = list(closes_fn(s))
        except Exception as exc:  # noqa: BLE001
            log.debug("closes for %s unavailable: %s", s, exc)
            out[s] = []
    return out


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def gather_context(
    creds,
    symbols,
    *,
    now: datetime | None = None,
    calendar=HIGH_IMPACT_CALENDAR,
    headlines_fn=None,
    vix_fn=None,
    closes_fn=None,
) -> MarketContext:
    """Pull every context signal, degrading each independently. Always returns a
    :class:`MarketContext` — never raises."""
    now = now or datetime.now(timezone.utc)
    symbols = list(symbols)
    errors: list[str] = []

    # -- macro --------------------------------------------------------------- #
    try:
        events = tuple(upcoming_high_impact(now, calendar=calendar))
        macro_today = high_impact_today(now, calendar=calendar)
    except Exception as exc:  # noqa: BLE001
        events, macro_today = (), False
        errors.append(f"macro: {exc}")

    # -- vix --------------------------------------------------------------- #
    _vix = vix_fn or (lambda: fetch_vix_proxy(creds))
    try:
        vix, vix_chg, vix_note = _vix()
    except Exception as exc:  # noqa: BLE001
        vix, vix_chg, vix_note = None, None, "unavailable"
        errors.append(f"vix: {exc}")

    # -- news --------------------------------------------------------------- #
    _news = headlines_fn or (lambda: fetch_headlines(creds, symbols))
    try:
        head_map = _news() or {}
    except Exception as exc:  # noqa: BLE001
        head_map = {}
        errors.append(f"news: {exc}")

    # -- internals (RSI) ------------------------------------------------- #
    _closes = closes_fn or (lambda: fetch_closes_map(creds, symbols))
    try:
        closes_map = _closes() or {}
    except Exception as exc:  # noqa: BLE001
        closes_map = {}
        errors.append(f"internals: {exc}")

    tickers = []
    for s in symbols:
        heads = tuple(head_map.get(s) or ())
        rsi = wilder_rsi(closes_map.get(s) or [])
        tickers.append(TickerContext(s, rsi, classify_rsi(rsi), heads, score_headlines(heads)))

    got_something = bool(events) or vix is not None or \
        any(t.headlines for t in tickers) or any(t.rsi is not None for t in tickers)
    if not got_something:
        mc = MarketContext.unavailable("; ".join(errors) or "no context signals available")
        return mc

    return MarketContext(
        as_of=now.isoformat(),
        macro_events=events,
        macro_today_high_impact=macro_today,
        vix_proxy=vix,
        vix_change_5d_pct=vix_chg,
        vix_note=vix_note,
        tickers=tuple(tickers),
        ok=True,
        errors=tuple(errors),
    )


# --------------------------------------------------------------------------- #
# Prioritised selection
# --------------------------------------------------------------------------- #
def prioritize(symbols, snapshots: dict, context: MarketContext) -> list[str]:
    """Order eligible tickers best-first: richest IV-RV spread wins, and the
    news-sentiment score breaks ties (more favourable first)."""
    def key(s: str):
        spread = (snapshots.get(s) or {}).get("iv_rv_spread")
        spread = spread if spread is not None else float("-inf")
        tc = context.ticker(s) if context else None
        news = tc.news_score if tc else 0
        return (spread, news)

    return sorted(symbols, key=key, reverse=True)
