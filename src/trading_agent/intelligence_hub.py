"""
intelligence_hub.py — Quantamental context layer (yfinance primary).

Sits in front of ``context_gatherer``: it pulls the same signals plus a VIX term
structure, preferring **yfinance** and falling back — pipe by pipe — to the
Alpaca-based fetchers, then to "No Context Available". The main loop never sees an
exception from here.

Signals added on top of ``context_gatherer.MarketContext``:

  * ``vix`` / ``vxv`` / ``vix_vxv_ratio`` — ^VIX (1-month) vs ^VIX3M (3-month).
  * ``panic_regime`` — term structure in **backwardation** (VIX > VXV): the
    market is pricing near-term stress; strategy.py flips short-vol → long-vol.
  * ``macro_danger`` — a High-Impact ("Red Folder") event inside the next 48h.

``context_gatherer`` keeps ownership of the pure maths (Wilder RSI, sentiment,
the macro calendar, ``prioritize``); this module only swaps the data source and
computes the two regime flags.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import context_gatherer as cg

log = logging.getLogger("agent.intel")

PANIC_RATIO_THRESHOLD = 1.0      # VIX / VXV above this => backwardation => panic
VIX_SYMBOL = "^VIX"
VXV_SYMBOLS = ("^VIX3M", "^VXV")  # ^VXV was delisted; ^VIX3M is the live 3-month
RSI_SESSIONS = 30


# --------------------------------------------------------------------------- #
# yfinance response parsing (pure)
# --------------------------------------------------------------------------- #
def _headlines_from_yf_news(items, *, per: int) -> list[str]:
    """yfinance's ``.news`` changed shape: newer items nest the title under
    ``content``, older ones expose ``title`` directly. Handle both."""
    out: list[str] = []
    for it in items or []:
        title = (it.get("title")
                 or (it.get("content") or {}).get("title")
                 or "").strip()
        if title and title not in out:
            out.append(title)
        if len(out) >= per:
            break
    return out


# --------------------------------------------------------------------------- #
# yfinance fetchers (import lazily; each raises on failure so gather() can
# fall back)
# --------------------------------------------------------------------------- #
def _yf_last_close(ticker) -> float | None:
    hist = ticker.history(period="7d")
    closes = [float(x) for x in hist["Close"].dropna().tolist()] if hist is not None else []
    return closes[-1] if closes else None


def yf_vix_term_structure() -> tuple[float | None, float | None, float | None]:
    import yfinance as yf

    vix = _yf_last_close(yf.Ticker(VIX_SYMBOL))
    vxv = None
    for sym in VXV_SYMBOLS:
        vxv = _yf_last_close(yf.Ticker(sym))
        if vxv:
            break
    ratio = round(vix / vxv, 3) if (vix and vxv) else None
    return vix, vxv, ratio


def yf_headlines(symbols, *, per: int = cg.HEADLINES_PER_TICKER) -> dict[str, list[str]]:
    import yfinance as yf

    out: dict[str, list[str]] = {}
    for s in symbols:
        try:
            out[s] = _headlines_from_yf_news(yf.Ticker(s).news, per=per)
        except Exception as exc:  # noqa: BLE001
            log.debug("yfinance news for %s: %s", s, exc)
            out[s] = []
    return out


def yf_closes(symbols, *, sessions: int = RSI_SESSIONS) -> dict[str, list[float]]:
    import yfinance as yf

    out: dict[str, list[float]] = {}
    for s in symbols:
        try:
            hist = yf.Ticker(s).history(period=f"{sessions * 2}d")
            closes = [float(x) for x in hist["Close"].dropna().tolist()]
            out[s] = closes[-sessions:]
        except Exception as exc:  # noqa: BLE001
            log.debug("yfinance history for %s: %s", s, exc)
            out[s] = []
    return out


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def _try(primary, fallback, errors: list[str], label: str):
    """Run ``primary``; on any exception log it and run ``fallback``; if that
    also raises, return None and record both."""
    try:
        return primary()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label}: yf {type(exc).__name__}: {exc}")
        if fallback is None:
            return None
        try:
            return fallback()
        except Exception as exc2:  # noqa: BLE001
            errors.append(f"{label}: fallback {type(exc2).__name__}: {exc2}")
            return None


def gather(
    creds,
    symbols,
    *,
    now: datetime | None = None,
    calendar=cg.HIGH_IMPACT_CALENDAR,
    # yfinance pipes (injected in tests)
    vix_fn=None,
    news_fn=None,
    closes_fn=None,
    # Alpaca fallbacks (context_gatherer); injected in tests
    alpaca_vix_fn=None,
    alpaca_news_fn=None,
    alpaca_closes_fn=None,
) -> cg.MarketContext:
    """yfinance-first context pull with Alpaca fallback. Never raises."""
    now = now or datetime.now(timezone.utc)
    symbols = list(symbols)
    errors: list[str] = []

    vix_fn = vix_fn or yf_vix_term_structure
    news_fn = news_fn or (lambda syms: yf_headlines(syms))
    closes_fn = closes_fn or (lambda syms: yf_closes(syms))
    alpaca_vix_fn = alpaca_vix_fn or (lambda: cg.fetch_vix_proxy(creds))
    alpaca_news_fn = alpaca_news_fn or (lambda syms: cg.fetch_headlines(creds, syms))
    alpaca_closes_fn = alpaca_closes_fn or (lambda syms: cg.fetch_closes_map(creds, syms))

    # -- macro (pure; from context_gatherer's calendar) -------------------- #
    try:
        events = tuple(cg.upcoming_high_impact(now, calendar=calendar))
        macro_today = cg.high_impact_today(now, calendar=calendar)
    except Exception as exc:  # noqa: BLE001
        events, macro_today = (), False
        errors.append(f"macro: {exc}")
    macro_danger = bool(events)

    # -- VIX term structure (yf) -> proxy (Alpaca) ------------------------ #
    vix = vxv = ratio = None
    vix_proxy = vix_chg = None
    vix_note = "unavailable"
    term = _try(vix_fn, None, errors, "vix")
    if term:
        vix, vxv, ratio = term
    if vix is None:
        proxy = _try(alpaca_vix_fn, None, errors, "vix")
        if proxy:
            vix_proxy, vix_chg, vix_note = proxy
    panic_regime = ratio is not None and ratio > PANIC_RATIO_THRESHOLD

    # -- news ------------------------------------------------------------- #
    head_map = _try(lambda: news_fn(symbols), lambda: alpaca_news_fn(symbols),
                    errors, "news") or {}

    # -- closes -> RSI -------------------------------------------------- #
    closes_map = _try(lambda: closes_fn(symbols), lambda: alpaca_closes_fn(symbols),
                      errors, "internals") or {}

    tickers = []
    for s in symbols:
        heads = tuple(head_map.get(s) or ())
        rsi = cg.wilder_rsi(closes_map.get(s) or [])
        tickers.append(cg.TickerContext(s, rsi, cg.classify_rsi(rsi), heads,
                                        cg.score_headlines(heads)))

    got_something = (
        bool(events) or vix is not None or vix_proxy is not None
        or any(t.headlines for t in tickers) or any(t.rsi is not None for t in tickers)
    )
    if not got_something:
        return cg.MarketContext.unavailable("; ".join(errors) or "no context signals")

    return cg.MarketContext(
        as_of=now.isoformat(),
        macro_events=events,
        macro_today_high_impact=macro_today,
        vix_proxy=vix_proxy,
        vix_change_5d_pct=vix_chg,
        vix_note=vix_note,
        tickers=tuple(tickers),
        ok=True,
        errors=tuple(errors),
        vix=vix,
        vxv=vxv,
        vix_vxv_ratio=ratio,
        panic_regime=panic_regime,
        macro_danger=macro_danger,
    )


# re-export so callers can do intelligence_hub.prioritize(...)
prioritize = cg.prioritize
MarketContext = cg.MarketContext
