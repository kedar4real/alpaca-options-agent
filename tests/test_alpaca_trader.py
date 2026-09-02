"""Offline unit tests for alpaca_trader pure helpers (no network)."""

from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

import pytest

from trading_agent import alpaca_trader as at


# --------------------------------------------------------------------------- #
# next_friday
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("today", "weeks_ahead", "expected"),
    [
        (date(2026, 8, 30), 0, date(2026, 9, 4)),   # Sunday   -> that Friday
        (date(2026, 8, 31), 0, date(2026, 9, 4)),   # Monday   -> that Friday
        (date(2026, 9, 4), 0, date(2026, 9, 4)),    # Friday   -> same day
        (date(2026, 9, 5), 0, date(2026, 9, 11)),   # Saturday -> next Friday
        (date(2026, 8, 30), 1, date(2026, 9, 11)),  # roll one week forward
    ],
)
def test_next_friday(today: date, weeks_ahead: int, expected: date) -> None:
    assert at.next_friday(from_date=today, weeks_ahead=weeks_ahead) == expected


# --------------------------------------------------------------------------- #
# nth_trading_day  (NYSE calendar: skips weekends AND exchange holidays)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        (date(2026, 8, 31), 1, date(2026, 9, 1)),    # Mon -> Tue
        (date(2026, 8, 31), 3, date(2026, 9, 3)),    # Mon -> Thu
        (date(2026, 8, 28), 1, date(2026, 8, 31)),   # Fri -> Mon (skip weekend)
        (date(2026, 8, 29), 1, date(2026, 8, 31)),   # Sat -> Mon
        (date(2026, 8, 28), 3, date(2026, 9, 2)),    # Fri -> Wed (skip weekend)
        # Labor Day 2026-09-07 (Mon) is skipped:
        (date(2026, 9, 4), 1, date(2026, 9, 8)),     # Fri -> Tue (skip Mon holiday)
        (date(2026, 9, 3), 2, date(2026, 9, 8)),     # Thu -> +1 Fri, +2 Tue
        (date(2026, 9, 4), 3, date(2026, 9, 10)),    # Fri -> Tue, Wed, Thu
        # Thanksgiving 2026-11-26 (Thu) is skipped:
        (date(2026, 11, 25), 1, date(2026, 11, 27)),  # Wed -> Fri
        # Christmas 2026-12-25 (Fri) is skipped:
        (date(2026, 12, 24), 1, date(2026, 12, 28)),  # Thu -> Mon
    ],
)
def test_nth_trading_day(start: date, n: int, expected: date) -> None:
    assert at.nth_trading_day(n, from_date=start) == expected


def test_nth_trading_day_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        at.nth_trading_day(0, from_date=date(2026, 9, 4))


# --------------------------------------------------------------------------- #
# parse_occ_symbol
# --------------------------------------------------------------------------- #
def test_parse_occ_symbol_put() -> None:
    root, expiry, right, strike = at.parse_occ_symbol("SPY260904P00763000")
    assert (root, expiry, right, strike) == ("SPY", date(2026, 9, 4), "put", 763.0)


def test_parse_occ_symbol_call_fractional_strike() -> None:
    root, expiry, right, strike = at.parse_occ_symbol("SPY260904C00777500")
    assert (root, expiry, right, strike) == ("SPY", date(2026, 9, 4), "call", 777.5)


def test_parse_occ_symbol_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        at.parse_occ_symbol("not-an-option")


# --------------------------------------------------------------------------- #
# _to_contract / spread math
# --------------------------------------------------------------------------- #
def _snap(bid, ask, *, bid_size=10, ask_size=12, delta=-0.25, iv=0.13):
    quote = SimpleNamespace(
        bid_price=bid, ask_price=ask, bid_size=bid_size, ask_size=ask_size
    )
    greeks = None if delta is None else SimpleNamespace(delta=delta)
    return SimpleNamespace(latest_quote=quote, greeks=greeks, implied_volatility=iv)


def test_to_contract_spread_metrics() -> None:
    c = at._to_contract("SPY260904P00763000", _snap(1.87, 1.90))
    assert c is not None
    assert c.right == "put"
    assert c.strike == 763.0
    assert c.mid == pytest.approx(1.885)
    assert c.spread == pytest.approx(0.03)
    assert c.spread_pct == pytest.approx(0.03 / 1.885 * 100)
    assert c.abs_delta == pytest.approx(0.25)


def test_to_contract_zero_mid_is_nan_pct() -> None:
    c = at._to_contract("SPY260904P00500000", _snap(0.0, 0.0))
    assert c is not None
    assert c.spread == 0.0
    assert math.isnan(c.spread_pct)


def test_to_contract_without_quote_returns_none() -> None:
    snap = SimpleNamespace(latest_quote=None, greeks=None, implied_volatility=None)
    assert at._to_contract("SPY260904C00777000", snap) is None


def test_to_contract_missing_greeks_keeps_delta_none() -> None:
    c = at._to_contract("SPY260904C00777000", _snap(1.21, 1.27, delta=None))
    assert c is not None and c.delta is None and c.abs_delta is None


# --------------------------------------------------------------------------- #
# filter_delta_band
# --------------------------------------------------------------------------- #
def _contract(right: str, abs_delta: float | None) -> at.OptionContract:
    return at.OptionContract(
        symbol="X",
        underlying="SPY",
        expiry=date(2026, 9, 4),
        right=right,
        strike=700.0,
        bid=1.0,
        ask=1.1,
        bid_size=1,
        ask_size=1,
        mid=1.05,
        spread=0.1,
        spread_pct=9.5,
        delta=None if abs_delta is None else -abs_delta,
        abs_delta=abs_delta,
        implied_volatility=0.1,
    )


def test_filter_delta_band_selects_and_sorts() -> None:
    contracts = [
        _contract("put", 0.10),
        _contract("put", 0.30),
        _contract("put", 0.22),
        _contract("put", 0.35),
        _contract("put", None),
        _contract("call", 0.25),  # wrong right
    ]
    out = at.filter_delta_band(contracts, "put", 0.20, 0.30)
    assert [c.abs_delta for c in out] == [0.22, 0.30]


def test_filter_delta_band_is_inclusive_and_order_agnostic() -> None:
    contracts = [_contract("call", 0.20), _contract("call", 0.30)]
    out = at.filter_delta_band(contracts, "call", 0.30, 0.20)
    assert [c.abs_delta for c in out] == [0.20, 0.30]


# =========================================================================== #
# Step 5 — intraday inputs: recent news + intraday realized vol
# =========================================================================== #
CREDS = at.AlpacaCredentials(api_key="k", secret_key="s", paper=True)


def test_annualize_intraday_vol_needs_a_minimum_bar_count() -> None:
    assert at.annualize_intraday_vol([100.0] * 11, min_bars=12) is None
    assert at.annualize_intraday_vol(None) is None
    assert at.annualize_intraday_vol([]) is None


def test_annualize_intraday_vol_is_zero_for_a_flat_tape() -> None:
    assert at.annualize_intraday_vol([100.0] * 20) == 0.0


def test_annualize_intraday_vol_grows_with_movement() -> None:
    calm = [100.0 + (i % 2) * 0.01 for i in range(40)]
    wild = [100.0 + (i % 2) * 1.00 for i in range(40)]
    assert at.annualize_intraday_vol(calm) < at.annualize_intraday_vol(wild)
    assert at.annualize_intraday_vol(wild) > 0.0


class _FakeNewsClient:
    def __init__(self, headlines, raises=None):
        self._h = headlines
        self._raises = raises
        self.seen = None

    def get_news(self, request):
        if self._raises:
            raise self._raises
        self.seen = request
        items = [SimpleNamespace(headline=h) for h in self._h]
        return SimpleNamespace(data={"news": items})


def test_fetch_recent_news_returns_trimmed_headlines() -> None:
    client = _FakeNewsClient(["  Gold rips higher  ", "Fed speaks", ""])
    out = at.fetch_recent_news(CREDS, "GLD", limit=5, client=client)
    assert out == ["Gold rips higher", "Fed speaks"]      # blanks dropped, trimmed


def test_fetch_recent_news_respects_the_limit() -> None:
    client = _FakeNewsClient([f"h{i}" for i in range(10)])
    assert len(at.fetch_recent_news(CREDS, "SPY", limit=3, client=client)) == 3


def test_fetch_recent_news_is_fail_safe() -> None:
    client = _FakeNewsClient([], raises=RuntimeError("news api 500"))
    assert at.fetch_recent_news(CREDS, "SPY", client=client) == []


class _FakeBarsClient:
    def __init__(self, closes, raises=None):
        self._closes = closes
        self._raises = raises

    def get_stock_bars(self, request):
        if self._raises:
            raise self._raises
        bars = [SimpleNamespace(close=c) for c in self._closes]
        return {request.symbol_or_symbols: bars}


def test_intraday_realized_vol_from_five_minute_bars() -> None:
    client = _FakeBarsClient([100.0 + (i % 2) * 0.5 for i in range(30)])
    rv = at.intraday_realized_vol(CREDS, "SPY", client=client)
    assert rv is not None and rv > 0.0


def test_intraday_realized_vol_none_when_too_few_bars() -> None:
    client = _FakeBarsClient([100.0] * 5)
    assert at.intraday_realized_vol(CREDS, "SPY", client=client) is None


def test_intraday_realized_vol_is_fail_safe() -> None:
    client = _FakeBarsClient([], raises=RuntimeError("bars 500"))
    assert at.intraday_realized_vol(CREDS, "SPY", client=client) is None
