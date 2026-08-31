"""
alpaca_trader.py
================
SPY options-chain utilities for the Alpaca Options Trading Agent.

Responsibilities
----------------
1. Resolve the next upcoming Friday expiration date.
2. Pull the SPY option chain for that expiry via ``alpaca-py`` (indicative feed).
3. Filter for 20-30 delta puts and calls.
4. Compute bid/ask spread metrics (absolute and % of mid) per contract.

Run directly for a formatted console report::

    python alpaca_trader.py
    python alpaca_trader.py --weeks-ahead 1 --delta-min 0.20 --delta-max 0.30
    python alpaca_trader.py --json

Credentials are read from the environment (never hardcoded)::

    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    ALPACA_PAPER_TRADE   (optional, default: true)

A local ``.env`` file (project root, or ``./alpaca-mcp-server/.env``) is
auto-loaded if present.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from alpaca.data.enums import OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame

log = logging.getLogger("alpaca_trader")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
UNDERLYING = "SPY"
FRIDAY = 4  # date.weekday(): Monday == 0 ... Sunday == 6
MARKET_CALENDAR = "XNYS"  # NYSE; SPY options follow the NYSE holiday schedule
DEFAULT_DELTA_MIN = 0.20
DEFAULT_DELTA_MAX = 0.30
# Only request strikes within +/- this fraction of spot, to keep the payload
# small. Ignored when the spot price cannot be fetched.
STRIKE_WINDOW_PCT = 0.15

# .env lookup: the cwd, then the repo root (src/trading_agent/ -> ../../..).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_CANDIDATES = (
    Path.cwd() / ".env",
    _REPO_ROOT / ".env",
    Path(__file__).resolve().parent / ".env",
)

_OCC_RE = re.compile(
    r"^(?P<root>[A-Z]+)"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])"
    r"(?P<strike>\d{8})$"
)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    secret_key: str
    paper: bool = True


def load_credentials() -> AlpacaCredentials:
    """Load API credentials from the environment / a local ``.env`` file."""
    for path in _ENV_CANDIDATES:
        if path.is_file():
            load_dotenv(path, override=False)
            log.debug("loaded env file: %s", path)

    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Export them or add "
            "them to a .env file in the project root."
        )
    paper = os.environ.get("ALPACA_PAPER_TRADE", "true").strip().lower() != "false"
    return AlpacaCredentials(api_key=api_key, secret_key=secret_key, paper=paper)


# --------------------------------------------------------------------------- #
# Expiry helpers
# --------------------------------------------------------------------------- #
def next_friday(from_date: date | None = None, weeks_ahead: int = 0) -> date:
    """Return the next upcoming Friday on/after ``from_date``.

    If ``from_date`` is itself a Friday, that date is returned (``weeks_ahead=0``).
    Use ``weeks_ahead`` to roll forward whole weeks.
    """
    d = from_date or date.today()
    days_ahead = (FRIDAY - d.weekday()) % 7
    return d + timedelta(days=days_ahead + 7 * weeks_ahead)


@lru_cache(maxsize=1)
def _market_calendar():
    """Cached NYSE calendar (import is a little heavy, do it lazily/once)."""
    import pandas_market_calendars as mcal

    return mcal.get_calendar(MARKET_CALENDAR)


def trading_sessions(start: date, end: date) -> list[date]:
    """All NYSE trading sessions in the inclusive range ``[start, end]``."""
    schedule = _market_calendar().schedule(
        start_date=start.isoformat(), end_date=end.isoformat()
    )
    return [ts.date() for ts in schedule.index]


def nth_trading_day(n: int, from_date: date | None = None) -> date:
    """Return the date ``n`` NYSE trading sessions after ``from_date``
    (``n=1`` -> next trading day).

    Weekends **and** exchange holidays (via ``pandas-market-calendars``) are
    skipped, so e.g. the session after Fri 2026-09-04 is Tue 2026-09-08
    (Mon 09-07 is Labor Day).
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    start = (from_date or date.today()) + timedelta(days=1)
    # Widen the search window until it contains at least n sessions (holidays and
    # weekends mean a calendar span > n is always needed).
    span = n + 7
    while True:
        sessions = trading_sessions(start, start + timedelta(days=span))
        if len(sessions) >= n:
            return sessions[n - 1]
        span *= 2


def parse_occ_symbol(symbol: str) -> tuple[str, date, str, float]:
    """Parse an OCC option symbol -> ``(root, expiry, right, strike)``.

    Example: ``SPY260904C00709000`` -> ``("SPY", date(2026, 9, 4), "call", 709.0)``.
    """
    m = _OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"unrecognized OCC option symbol: {symbol!r}")
    expiry = date(2000 + int(m["yy"]), int(m["mm"]), int(m["dd"]))
    right = "call" if m["cp"] == "C" else "put"
    strike = int(m["strike"]) / 1000.0
    return m["root"], expiry, right, strike


# --------------------------------------------------------------------------- #
# Contract model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    expiry: date
    right: str  # "call" | "put"
    strike: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    mid: float
    spread: float  # ask - bid, in dollars
    spread_pct: float  # spread / mid * 100 (nan when mid <= 0)
    delta: float | None
    abs_delta: float | None
    implied_volatility: float | None

    def as_row(self) -> dict[str, object]:
        row = asdict(self)
        row["expiry"] = self.expiry.isoformat()
        for key in ("mid", "spread"):
            row[key] = round(row[key], 4)
        row["spread_pct"] = None if math.isnan(self.spread_pct) else round(self.spread_pct, 4)
        return row


def _to_contract(symbol: str, snapshot) -> OptionContract | None:
    """Convert an ``OptionsSnapshot`` into an :class:`OptionContract`.

    Returns ``None`` when the snapshot has no usable two-sided quote.
    """
    quote = getattr(snapshot, "latest_quote", None)
    if quote is None or quote.bid_price is None or quote.ask_price is None:
        return None

    root, expiry, right, strike = parse_occ_symbol(symbol)
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)
    mid = (bid + ask) / 2.0
    spread = ask - bid
    spread_pct = (spread / mid * 100.0) if mid > 0 else math.nan

    greeks = getattr(snapshot, "greeks", None)
    delta = float(greeks.delta) if greeks is not None else None

    return OptionContract(
        symbol=symbol,
        underlying=root,
        expiry=expiry,
        right=right,
        strike=strike,
        bid=bid,
        ask=ask,
        bid_size=float(quote.bid_size or 0.0),
        ask_size=float(quote.ask_size or 0.0),
        mid=mid,
        spread=spread,
        spread_pct=spread_pct,
        delta=delta,
        abs_delta=abs(delta) if delta is not None else None,
        implied_volatility=(
            float(snapshot.implied_volatility)
            if getattr(snapshot, "implied_volatility", None) is not None
            else None
        ),
    )


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def get_spot_price(
    creds: AlpacaCredentials,
    symbol: str = UNDERLYING,
    *,
    method: str = "trade",
) -> float | None:
    """Latest reference price for ``symbol``; ``None`` if it cannot be fetched.

    ``method="trade"``     -> last trade price.
    ``method="quote_mid"`` -> midpoint of the latest NBBO quote, falling back to
    the last trade when the quote is empty or one-sided (e.g. market closed).
    """
    try:
        client = StockHistoricalDataClient(creds.api_key, creds.secret_key)
        if method == "quote_mid":
            quote = client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
            bid = float(quote.bid_price or 0.0)
            ask = float(quote.ask_price or 0.0)
            if bid > 0.0 and ask > 0.0:
                return (bid + ask) / 2.0
            log.debug("%s quote empty/one-sided; falling back to last trade", symbol)
        trade = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol)
        )
        return float(trade[symbol].price)
    except Exception as exc:  # noqa: BLE001 - spot is a best-effort optimization
        log.warning("could not fetch %s spot price: %s", symbol, exc)
        return None


def get_daily_closes(
    creds: AlpacaCredentials,
    symbol: str = UNDERLYING,
    *,
    sessions: int = 11,
    calendar_lookback_days: int | None = None,
) -> list[float]:
    """Return up to the last ``sessions`` daily closing prices, oldest first.

    Pass ``sessions = window + 1`` so the caller has enough closes for ``window``
    daily returns. Returns ``[]`` if the request fails or no bars come back.
    """
    lookback = calendar_lookback_days or (sessions * 3 + 15)
    try:
        client = StockHistoricalDataClient(creds.api_key, creds.secret_key)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=(date.today() - timedelta(days=lookback)).isoformat(),
        )
        bars = client.get_stock_bars(request)
        series = bars[symbol]
    except Exception as exc:  # noqa: BLE001 - best-effort history pull
        log.warning("could not fetch %s daily bars: %s", symbol, exc)
        return []
    return [float(bar.close) for bar in series][-sessions:]


def fetch_option_chain(
    creds: AlpacaCredentials,
    expiry: date | None = None,
    *,
    expiry_gte: date | None = None,
    expiry_lte: date | None = None,
    feed: OptionsFeed = OptionsFeed.INDICATIVE,
    spot: float | None = None,
    strike_window_pct: float | None = STRIKE_WINDOW_PCT,
) -> dict:
    """Return ``{occ_symbol: OptionsSnapshot}`` for SPY.

    Pass ``expiry`` for a single expiration date, or ``expiry_gte`` /
    ``expiry_lte`` for a date range. With ``spot`` set and a non-zero
    ``strike_window_pct``, strikes are limited to ``spot * (1 +/- pct)``. Every
    filter is optional; omitting all of them pulls the entire chain (large).
    """
    client = OptionHistoricalDataClient(creds.api_key, creds.secret_key)
    kwargs: dict[str, object] = {"underlying_symbol": UNDERLYING, "feed": feed}
    if expiry is not None:
        kwargs["expiration_date"] = expiry
    if expiry_gte is not None:
        kwargs["expiration_date_gte"] = expiry_gte
    if expiry_lte is not None:
        kwargs["expiration_date_lte"] = expiry_lte
    if spot and strike_window_pct:
        kwargs["strike_price_gte"] = round(spot * (1.0 - strike_window_pct), 2)
        kwargs["strike_price_lte"] = round(spot * (1.0 + strike_window_pct), 2)

    snapshots = client.get_option_chain(OptionChainRequest(**kwargs))
    log.debug(
        "chain %s..%s %s -> %d contracts",
        expiry_gte or expiry or "*",
        expiry_lte or expiry or "*",
        feed.value,
        len(snapshots),
    )
    return snapshots


def build_contracts(snapshots: dict) -> list[OptionContract]:
    contracts: list[OptionContract] = []
    for symbol, snapshot in snapshots.items():
        try:
            contract = _to_contract(symbol, snapshot)
        except ValueError as exc:
            log.debug("skip %s: %s", symbol, exc)
            continue
        if contract is not None:
            contracts.append(contract)
    return contracts


def filter_delta_band(
    contracts: list[OptionContract],
    right: str,
    delta_min: float = DEFAULT_DELTA_MIN,
    delta_max: float = DEFAULT_DELTA_MAX,
) -> list[OptionContract]:
    """Contracts of ``right`` ("call"/"put") whose |delta| is in the band.

    Sorted by ascending |delta|.
    """
    lo, hi = sorted((abs(delta_min), abs(delta_max)))
    return sorted(
        (
            c
            for c in contracts
            if c.right == right
            and c.abs_delta is not None
            and lo <= c.abs_delta <= hi
        ),
        key=lambda c: c.abs_delta,  # type: ignore[arg-type,return-value]
    )


# --------------------------------------------------------------------------- #
# Top-level scan
# --------------------------------------------------------------------------- #
@dataclass
class ChainScan:
    expiry: date
    spot: float | None
    feed: str
    delta_min: float
    delta_max: float
    calls: list[OptionContract] = field(default_factory=list)
    puts: list[OptionContract] = field(default_factory=list)
    contract_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "expiry": self.expiry.isoformat(),
            "spot": self.spot,
            "feed": self.feed,
            "delta_band": [self.delta_min, self.delta_max],
            "contract_count": self.contract_count,
            "calls": [c.as_row() for c in self.calls],
            "puts": [c.as_row() for c in self.puts],
        }


def scan_spy_chain(
    *,
    weeks_ahead: int = 0,
    delta_min: float = DEFAULT_DELTA_MIN,
    delta_max: float = DEFAULT_DELTA_MAX,
    feed: OptionsFeed = OptionsFeed.INDICATIVE,
    creds: AlpacaCredentials | None = None,
) -> ChainScan:
    """Fetch + filter the SPY chain for the next (or later) Friday expiry."""
    creds = creds or load_credentials()
    expiry = next_friday(weeks_ahead=weeks_ahead)
    spot = get_spot_price(creds)
    snapshots = fetch_option_chain(creds, expiry, feed=feed, spot=spot)
    contracts = build_contracts(snapshots)

    if not contracts:
        log.warning(
            "no quotable contracts for %s %s - the expiry may not be listed "
            "(holiday?) or the feed returned no quotes; try --weeks-ahead 1",
            UNDERLYING,
            expiry,
        )

    return ChainScan(
        expiry=expiry,
        spot=spot,
        feed=feed.value,
        delta_min=delta_min,
        delta_max=delta_max,
        calls=filter_delta_band(contracts, "call", delta_min, delta_max),
        puts=filter_delta_band(contracts, "put", delta_min, delta_max),
        contract_count=len(contracts),
    )


# --------------------------------------------------------------------------- #
# CLI / reporting
# --------------------------------------------------------------------------- #
# (attr, header, width, precision) -- symbol is left-aligned, everything else right.
_COLUMNS = (
    ("symbol", "Symbol", 20, None),
    ("strike", "Strike", 8, 2),
    ("abs_delta", "|Delta|", 7, 3),
    ("bid", "Bid", 8, 2),
    ("ask", "Ask", 8, 2),
    ("mid", "Mid", 8, 2),
    ("spread", "Spread", 7, 2),
    ("spread_pct", "Spread%", 8, 2),
    ("implied_volatility", "IV", 7, 3),
    ("bid_size", "BidSz", 7, 0),
    ("ask_size", "AskSz", 7, 0),
)


def _render_table(contracts: list[OptionContract]) -> str:
    def cell(text: str, width: int, left: bool) -> str:
        return f"{text:<{width}}" if left else f"{text:>{width}}"

    lines = [
        "  ".join(cell(title, width, attr == "symbol") for attr, title, width, _ in _COLUMNS)
    ]
    lines.append("-" * len(lines[0]))
    for c in contracts:
        cells = []
        for attr, _title, width, precision in _COLUMNS:
            value = getattr(c, attr)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                text = "n/a"
            elif precision is None:
                text = str(value)
            else:
                text = f"{value:.{precision}f}"
            cells.append(cell(text, width, attr == "symbol"))
        lines.append("  ".join(cells))
    return "\n".join(lines)


def _print_report(scan: ChainScan) -> None:
    spot = f"{scan.spot:.2f}" if scan.spot is not None else "n/a"
    print(
        f"\n{UNDERLYING} options chain  |  expiry {scan.expiry.isoformat()} "
        f"({scan.expiry:%a})  |  spot {spot}  |  feed {scan.feed}  |  "
        f"delta band {scan.delta_min:.2f}-{scan.delta_max:.2f}  |  "
        f"{scan.contract_count} quotable contracts"
    )
    for label, rows in (("PUTS", scan.puts), ("CALLS", scan.calls)):
        print(f"\n{label}  ({len(rows)} in band)")
        print(_render_table(rows) if rows else "  (none)")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--weeks-ahead",
        type=int,
        default=0,
        help="roll the target expiry forward N whole weeks (default: 0)",
    )
    parser.add_argument("--delta-min", type=float, default=DEFAULT_DELTA_MIN)
    parser.add_argument("--delta-max", type=float, default=DEFAULT_DELTA_MAX)
    parser.add_argument(
        "--feed",
        choices=[f.value for f in OptionsFeed],
        default=OptionsFeed.INDICATIVE.value,
        help="option data feed (default: indicative; opra needs a signed agreement)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        scan = scan_spy_chain(
            weeks_ahead=args.weeks_ahead,
            delta_min=args.delta_min,
            delta_max=args.delta_max,
            feed=OptionsFeed(args.feed),
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean CLI error
        log.error("%s", exc)
        return 1

    if args.json:
        print(json.dumps(scan.to_dict(), indent=2))
    else:
        _print_report(scan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
