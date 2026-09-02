"""
data.py — Market data layer for the SPY adaptive iron condor agent.

Responsibilities:
- Pull the current near-the-money SPY option chain from Alpaca
- Track implied volatility history (logged locally, day by day)
- Calculate IV percentile (today's IV vs. accumulated history)
- Return clean, structured data for strategy.py to consume

This module does NOT make trading decisions — it only fetches and shapes data.

Low-level Alpaca access (credentials, clients, chain fetch, OCC symbol parsing,
spot price) lives in ``alpaca_trader.py``; this module builds on those primitives
and adds the IV-percentile pipeline plus the strategy-facing snapshot.
"""

import csv
import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime

from .alpaca_trader import (
    UNDERLYING,
    fetch_option_chain,
    get_daily_closes,
    get_spot_price,
    load_credentials,
    nth_trading_day,
    parse_occ_symbol,
)

IV_LOOKBACK_DAYS = 30
IV_HISTORY_PATH = "iv_history.csv"

# --- Realized volatility ------------------------------------------------------
REALIZED_VOL_WINDOW = 10       # trading days of daily log returns
TRADING_DAYS_PER_YEAR = 252    # annualization factor

# --- IV-regime gate -------------------------------------------------------------
# The strategy sells premium, so it should only trade when volatility is at least
# moderately elevated.
IV_HISTORY_MIN_DAYS = 10    # rows of history required before the percentile is trusted
IV_PERCENTILE_MIN = 50.0    # once trusted: trade only at/above the median IV day
STATIC_IV_THRESHOLD = 0.08  # "Hackathon Mode": trade-eligible if ATM IV > 8% (uncalibrated bootstrap floor; the IV-RV spread gate is the governing edge check)

# The strategy only trades near-the-money contracts a few days out, so keep the
# chain pull tiny: strikes within +/- STRIKE_WINDOW_PCT of spot, expiring between
# EXPIRY_MIN_TRADING_DAYS and EXPIRY_MAX_TRADING_DAYS from now.
STRIKE_WINDOW_PCT = 0.05
EXPIRY_MIN_TRADING_DAYS = 1
EXPIRY_MAX_TRADING_DAYS = 3


def get_underlying_price(creds=None, symbol=UNDERLYING):
    """Latest SPY reference price (bid/ask midpoint, falling back to last trade)."""
    creds = creds or load_credentials()
    return get_spot_price(creds, symbol, method="quote_mid")


def get_current_option_chain(creds, current_price, underlying=UNDERLYING):
    """
    Fetch the live near-the-money option chain for the underlying.

    Limited to contracts expiring in the next 1-3 trading days and strikes within
    +/-5% of ``current_price``. Returns a dict of {symbol: OptionsSnapshot} with
    pricing, Greeks, and IV, on the 'indicative' feed (the paper account has no
    signed OPRA agreement).
    """
    return fetch_option_chain(
        creds,
        underlying=underlying,
        expiry_gte=nth_trading_day(EXPIRY_MIN_TRADING_DAYS),
        expiry_lte=nth_trading_day(EXPIRY_MAX_TRADING_DAYS),
        spot=current_price,
        strike_window_pct=STRIKE_WINDOW_PCT,
    )


def get_atm_iv(chain, current_price):
    """
    From the chain, find the at-the-money (ATM) contract and return its implied
    volatility as today's reference IV.

    We use ATM IV as a simple, stable proxy for 'current' volatility level.
    """
    closest_strike = None
    closest_diff = float("inf")
    atm_iv = None

    for symbol, snapshot in chain.items():
        if snapshot.implied_volatility is None:
            continue
        # OptionsSnapshot has no strike field; decode it from the OCC symbol
        # (e.g. "SPY260904C00777000" -> 777.0).
        try:
            _root, _expiry, _right, strike = parse_occ_symbol(symbol)
        except ValueError:
            continue
        diff = abs(strike - current_price)
        if diff < closest_diff:
            closest_diff = diff
            closest_strike = strike
            atm_iv = snapshot.implied_volatility

    return atm_iv, closest_strike


def get_historical_iv_series(underlying=UNDERLYING, days=IV_LOOKBACK_DAYS):
    """
    Placeholder for historical IV tracking.

    NOTE: Alpaca's options API gives you LIVE snapshots, not a clean historical
    IV time series out of the box. For a real IV percentile calculation, you have
    two practical options:

    1. Start logging today's ATM IV to a local file/CSV every time this script runs,
       and build your own 30-day history over the course of the hackathon.
    2. Approximate using historical underlying price volatility (realized vol) as
       a proxy until you've accumulated enough live IV snapshots.

    For NOW, this returns an empty list — you'll populate this by logging
    get_atm_iv() results daily. This is intentional: don't fake historical data,
    build the real pipeline from day 1 so your IV percentile is genuine by the
    time you need it for competition trading.
    """
    return []


def calculate_realized_vol(
    closes,
    *,
    window=REALIZED_VOL_WINDOW,
    annualization=TRADING_DAYS_PER_YEAR,
):
    """
    Annualized realized volatility of the underlying.

    = sample standard deviation of daily log returns over the last ``window``
    trading days, scaled by sqrt(``annualization``).

    ``closes`` is a price series, oldest first. Needs at least ``window + 1``
    positive prices; returns None otherwise (so the strategy can treat it as
    'not enough data' rather than a real reading).
    """
    if closes is None or len(closes) < window + 1:
        return None

    prices = closes[-(window + 1):]
    if any(p <= 0 for p in prices):
        return None

    log_returns = [
        math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))
    ]
    daily_std = statistics.stdev(log_returns)  # sample stdev (ddof=1)
    return round(daily_std * math.sqrt(annualization), 4)


def calculate_iv_percentile(current_iv, historical_iv_series):
    """
    Calculate where current IV ranks against historical values.
    Returns a percentile from 0-100.

    If we have fewer than IV_HISTORY_MIN_DAYS rows, returns None — callers
    should fall back to evaluate_iv_regime()'s Hackathon Mode static threshold.
    """
    if current_iv is None or len(historical_iv_series) < IV_HISTORY_MIN_DAYS:
        return None

    below_current = sum(1 for iv in historical_iv_series if iv < current_iv)
    percentile = (below_current / len(historical_iv_series)) * 100
    return round(percentile, 1)


@dataclass
class IVRegime:
    """Result of the volatility-regime gate."""

    atm_iv: float | None
    iv_percentile: float | None
    mode: str            # "percentile" | "hackathon_static"
    trade_eligible: bool
    reason: str


def evaluate_iv_regime(
    current_iv,
    historical_iv_series,
    *,
    static_iv_threshold=STATIC_IV_THRESHOLD,
    percentile_min=IV_PERCENTILE_MIN,
):
    """
    Decide whether the current volatility regime is rich enough to sell premium.

    - With >= IV_HISTORY_MIN_DAYS of logged history: use the IV percentile,
      eligible when it is at or above ``percentile_min``.
    - Otherwise ("Hackathon Mode"): fall back to a static level — eligible when
      ATM IV is above ``static_iv_threshold`` (15% by default).
    """
    percentile = calculate_iv_percentile(current_iv, historical_iv_series)

    if percentile is not None:
        eligible = percentile >= percentile_min
        rel = ">=" if eligible else "<"
        return IVRegime(
            atm_iv=current_iv,
            iv_percentile=percentile,
            mode="percentile",
            trade_eligible=eligible,
            reason=f"IV percentile {percentile:.1f} {rel} {percentile_min:.0f}",
        )

    if current_iv is None:
        return IVRegime(None, None, "hackathon_static", False, "ATM IV unavailable")

    eligible = current_iv > static_iv_threshold
    rel = ">" if eligible else "<="
    have = len(historical_iv_series)
    return IVRegime(
        atm_iv=current_iv,
        iv_percentile=None,
        mode="hackathon_static",
        trade_eligible=eligible,
        reason=(
            f"Hackathon Mode: ATM IV {current_iv:.3f} {rel} {static_iv_threshold:.2f} "
            f"({have}/{IV_HISTORY_MIN_DAYS} IV days logged)"
        ),
    )


# iv_history.csv is one global file for the whole basket:
#   timestamp, symbol, iv, rv, spread
IV_HISTORY_FIELDS = ["timestamp", "symbol", "iv", "rv", "spread"]


def log_iv_reading(symbol, iv, rv, spread, log_path=IV_HISTORY_PATH):
    """Append one IV reading for ``symbol`` to the shared ``iv_history.csv``.

    Called once per ticker per loop cycle — every call is a new row, so the file
    is a dense unified time-series for the basket. ``read_iv_history`` collapses
    it to one point per calendar day when feeding the IV-percentile gate.
    """
    file_exists = os.path.isfile(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(IV_HISTORY_FIELDS)
        writer.writerow([datetime.now().isoformat(), symbol, iv, rv, spread])


def read_iv_history(symbol=None, log_path=IV_HISTORY_PATH):
    """IV series for ``symbol`` (all symbols if None), oldest first.

    Collapsed to the **last reading per calendar day** so the IV percentile stays
    a day-based measure even though the file is appended every cycle. Rows with a
    blank/unparseable ``iv`` are skipped.
    """
    if not os.path.isfile(log_path):
        return []

    by_day: dict[str, float] = {}  # "symbol|YYYY-MM-DD" -> latest iv that day
    with open(log_path, "r", newline="") as f:
        for row in csv.DictReader(f):
            if symbol is not None and row.get("symbol") != symbol:
                continue
            try:
                iv = float(row["iv"])
            except (ValueError, KeyError, TypeError):
                continue
            day = row.get("timestamp", "")[:10]
            by_day[f"{row.get('symbol')}|{day}"] = iv
    return [by_day[k] for k in sorted(by_day)]


def get_market_snapshot(symbol=UNDERLYING, creds=None):
    """
    Main entry point — call this from strategy.py, once per basket ticker.
    Returns a single dict with everything the strategy layer needs for ``symbol``.
    """
    creds = creds or load_credentials()

    current_price = get_underlying_price(creds, symbol)
    chain = get_current_option_chain(creds, current_price, underlying=symbol)
    current_iv, atm_strike = get_atm_iv(chain, current_price)

    # Daily closes drive both realized vol and strategy.py's efficiency ratio.
    closes = get_daily_closes(creds, symbol, sessions=REALIZED_VOL_WINDOW + 1)
    realized_vol = calculate_realized_vol(closes)
    iv_rv_spread = (
        round(current_iv - realized_vol, 4)
        if current_iv is not None and realized_vol is not None
        else None
    )

    log_iv_reading(symbol, current_iv, realized_vol, iv_rv_spread)

    # Read back this symbol's accumulated history and run the IV-regime gate
    historical_iv = read_iv_history(symbol)
    iv_regime = evaluate_iv_regime(current_iv, historical_iv)

    return {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "underlying": symbol,
        "current_price": current_price,
        "atm_iv": current_iv,
        "realized_vol": realized_vol,
        "iv_rv_spread": iv_rv_spread,
        "atm_strike": atm_strike,
        "iv_percentile": iv_regime.iv_percentile,
        "iv_regime": iv_regime,
        "daily_closes": closes,
        "chain": chain,
    }


if __name__ == "__main__":
    import sys

    # Quick sanity test — run this file directly to confirm the pipeline works.
    # Optional arg: ticker (default SPY), e.g. `python -m trading_agent.data QQQ`
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else UNDERLYING
    snapshot = get_market_snapshot(sym)
    print(f"Timestamp:        {snapshot['timestamp']}")
    print(f"{sym} price:        ${snapshot['current_price']:.2f}")
    print(f"ATM strike:       {snapshot['atm_strike']}")
    print(f"ATM IV:           {snapshot['atm_iv']}")
    print(f"Realized vol:     {snapshot['realized_vol']} (10d, annualized)")
    print(f"IV - RV spread:   {snapshot['iv_rv_spread']} (>0 = IV richer than recent movement)")
    print(f"IV percentile:    {snapshot['iv_percentile']} (None = not enough history yet)")
    regime = snapshot["iv_regime"]
    verdict = "ELIGIBLE" if regime.trade_eligible else "blocked"
    print(f"IV regime:        [{regime.mode}] {verdict} - {regime.reason}")
    print(f"Chain size:       {len(snapshot['chain'])} contracts")
