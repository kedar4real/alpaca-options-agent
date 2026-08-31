# Project State

_Last updated: 2026-08-31_

## Architecture

Two-layer market-data design:

```
alpaca_trader.py  (low-level Alpaca layer)
  load_credentials()          - env / .env discovery -> AlpacaCredentials
  get_spot_price(method=)     - "trade" (last trade) or "quote_mid" (NBBO mid)
  get_daily_closes(sessions=) - last N daily closes (for realized vol)
  nth_trading_day(n)          - NYSE-calendar date math (weekends + holidays)
  trading_sessions(a, b)      - NYSE sessions in a date range
  next_friday()               - next upcoming Friday expiry
  parse_occ_symbol()          - OCC symbol -> (root, expiry, right, strike)
  fetch_option_chain()        - chain fetch w/ optional expiry + strike filters
  OptionContract / build_contracts() / filter_delta_band()
  scan_spy_chain()  + CLI  (20-30 delta scan, bid/ask spread report)
        |  imported by
        v
data.py  (strategy-facing layer for the iron condor agent)
  get_underlying_price()      - wraps get_spot_price(method="quote_mid")
  get_current_option_chain()  - near-dated (+1..+3 trading days), +/-5% strikes
  get_atm_iv()                - ATM implied vol via parse_occ_symbol
  calculate_realized_vol()    - 10d annualized stdev of daily log returns
  log_daily_iv() / read_iv_history() / calculate_iv_percentile()
  evaluate_iv_regime()        -> IVRegime (percentile >=10d, else static 15%)
  get_market_snapshot()       <- strategy.py calls this
      (adds realized_vol, iv_rv_spread = atm_iv - realized_vol)
        |  imported by
        v
strategy.py  (iron condor builder — proposes, does not place orders)
  pick_expiry()               - earliest listed expiry in nth_trading_day(1..3)
  select_short_leg()          - ~0.225 delta (prefers 0.20-0.25 band)
  select_long_leg()           - ~0.10 delta, else ~$5 further OTM
  plan_iron_condor()          - IV gate -> IV-RV gate -> legs -> credit/width -> sizing
  build_iron_condor()         -> IronCondorPlan
        |  its proposal is vetted by
        v
risk_manager.py  (pre-trade gates + expiry monitor — decides, never trades)
  check_order(order, account) -> RiskDecision   (gates 1-5, collects all fails)
    1 max risk/trade <= 1.5% current equity   2 daily loss halt 2.5% starting
    3 total drawdown floor 5% starting (+ sticky halt)   4 max 3 positions
    5 defined-risk: per right, long contracts == short contracts
  flag_expiring_positions() / trading_days_until()  (gate 6: <=1 session to expiry)
        |  imported by
        v
executor.py  (broker submission — gated, never bypassable)
  from_iron_condor_plan(plan) -> ProposedOrder   (adds the 4 OCC symbols)
  submit_iron_condor(order, account, *, client=, creds=) -> ExecutionResult
    -> check_order() FIRST; if not approved, nothing is sent
    -> else build LimitOrderRequest(OrderClass.MLEG, 4 x OptionLegRequest)
       and TradingClient.submit_order(); logs describe() every time
```

| Component | File | Status |
|---|---|---|
| Low-level Alpaca data layer | `alpaca_trader.py` | **Working** (live paper API) |
| Strategy-facing market data | `data.py` | **Working** (live paper API) |
| IV history / percentile / regime gate | `data.py` + `iv_history.csv` | Live; Hackathon Mode static gate until >= 10 daily rows |
| Strategy / iron condor builder | `strategy.py` | **Working** (smoke-tested on live chain); proposes only |
| Pre-trade risk gates + expiry monitor | `risk_manager.py` | **Working** (31 offline tests); decides only |
| Broker submission (MLEG) | `executor.py` | **Working** (18 offline tests); gate-checked, never bypassable. Not yet exercised against the live API |
| Order cancel / fill polling / position close | — | Not started |
| LangGraph orchestration | `agent.py` | Not started |

## `alpaca_trader.py`

`fetch_option_chain(creds, expiry=None, *, expiry_gte=None, expiry_lte=None,
feed=INDICATIVE, spot=None, strike_window_pct=0.15)` — all filters optional;
omitting them all pulls the full chain (large). `scan_spy_chain()` uses a single
Friday expiry + spot-windowed strikes.

CLI: `python -m trading_agent.alpaca_trader [--weeks-ahead N] [--delta-min X] [--delta-max Y] [--feed indicative|opra] [--json] [-v]`

## `data.py`

`get_market_snapshot()` returns `{timestamp, underlying, current_price, atm_iv,
realized_vol, iv_rv_spread, atm_strike, iv_percentile, iv_regime, chain}`
(`iv_regime` is an `IVRegime` dataclass; `iv_rv_spread = atm_iv - realized_vol`,
`None` if either side is missing). Chain pull is filtered to
`nth_trading_day(1)`..`nth_trading_day(3)` and strikes within +/-5% of spot:
~460 contracts / ~3s (down from ~13,160 / ~90s).

`evaluate_iv_regime()`: with >= `IV_HISTORY_MIN_DAYS` (10) logged IV rows, gate on
IV percentile >= `IV_PERCENTILE_MIN` (50); otherwise Hackathon Mode — eligible
when ATM IV > `STATIC_IV_THRESHOLD` (0.15).

`calculate_realized_vol(closes, window=10)`: sample stdev of daily log returns
over the last `window` sessions x sqrt(252); `None` if < `window + 1` closes.

Tunables: `STRIKE_WINDOW_PCT=0.05`, `EXPIRY_MIN/MAX_TRADING_DAYS=1/3`,
`IV_HISTORY_PATH`, `IV_HISTORY_MIN_DAYS`, `IV_PERCENTILE_MIN`, `STATIC_IV_THRESHOLD`,
`REALIZED_VOL_WINDOW=10`, `TRADING_DAYS_PER_YEAR=252`.

## `strategy.py`

`build_iron_condor(snapshot=None)` -> `IronCondorPlan(eligible, reason, expiry,
iv_rv_spread, legs[4], net_credit, wing_width, credit_to_width,
max_loss_per_contract, suggested_contracts)`. Iron condor only, defined-risk.
Gates in order: IV regime -> IV-RV spread (>= `MIN_IV_RV_SPREAD`, skipped when the
snapshot's `iv_rv_spread` is `None`) -> expiry -> legs -> credit/width -> risk
sizing. Credit is a mid-price estimate; wing width is the wider side. Position
size = `floor($1,500 / max-loss-per-contract)` (1.5% of a nominal $100k).

Tunables: `SHORT_DELTA_TARGET/MIN/MAX = 0.225/0.20/0.25`,
`LONG_DELTA_TARGET=0.10` (`±0.05` tol), `LONG_OTM_OFFSET=5.0`,
`MIN_CREDIT_TO_WIDTH=0.25`, `MIN_IV_RV_SPREAD=0.02`,
`DTE_MIN/MAX_TRADING_DAYS=1/3`, `MAX_RISK_PER_TRADE = MAX_RISK_PER_TRADE_PCT
× $100k = $1,500` (`MAX_RISK_PER_TRADE_PCT` imported from `risk_manager` — one
source of truth).

Run: `python -m trading_agent.strategy`.

## `risk_manager.py`

`check_order(order: ProposedOrder, account: AccountState) -> RiskDecision`
(`.approved`, `.blocks`, `.checks`, plus the computed risk / loss / drawdown
numbers; `.describe()`). Runs gates 1-5 and reports **all** failures. Gate 6 is
`flag_expiring_positions(positions, today=) -> list[ExpiringPosition]` built on
`trading_days_until()` (NYSE-session count, holiday-aware).

Boundary behaviour: risk `<=` cap passes (exactly at cap is OK); daily loss and
drawdown use `>=` (exactly at threshold halts); positions use `len < 3`.

Limits: `MAX_RISK_PER_TRADE_PCT=0.015` (of current equity),
`DAILY_LOSS_HALT_PCT=0.025` / `TOTAL_DRAWDOWN_FLOOR_PCT=0.05` (of starting
equity), `MAX_CONCURRENT_POSITIONS=3`, `EXPIRY_CLOSE_TRADING_DAYS=1`.

`executor.submit_iron_condor()` is the only caller of `check_order()`.
`AccountState` (starting / day-start / current equity, open positions, sticky
`trading_halted`) still has to be assembled and persisted by whatever drives the
executor — that glue does not exist yet.

## `executor.py`

`submit_iron_condor(order: ProposedOrder, account: AccountState, *, client=None,
creds=None) -> ExecutionResult`. Calls `check_order()` first; if not approved,
returns `submitted=False` and **sends nothing**. No bypass parameter (signature
is exactly `order, account, client, creds`). Logs `RiskDecision.describe()` on
every attempt — `WARNING "ORDER BLOCKED"` or `INFO "ORDER APPROVED"` +
`"ORDER SUBMITTED id=…"`. Broker/API exceptions are caught and returned in
`ExecutionResult.error`, never raised.

Approved -> `LimitOrderRequest(order_class=OrderClass.MLEG, qty=N,
time_in_force=DAY, limit_price=round(abs(net_credit), 2),
legs=[OptionLegRequest(occ_symbol, side, ratio_qty=1) × 4])` ->
`TradingClient.submit_order` (`paper=creds.paper`).

`from_iron_condor_plan(plan)` -> `ProposedOrder` with the 4 OCC symbols +
`suggested_contracts`; raises if `not plan.eligible` (strategy rejected it) or
the plan has no legs / no sizing — a strategy-rejected plan is never convertible
to an order, no override. `risk_manager.OrderLeg` now carries an optional
`symbol`.

Not exercised against the live API yet; `client=` injection is for tests only,
not a gate bypass.

## Known limitations / follow-ups
- **`get_atm_iv()` does not pin an expiry** — it keeps the nearest-strike
  contract across the whole 1-3 day window, so ATM IV varies run-to-run
  (~0.10-0.26 observed). This now **gates trading** via `evaluate_iv_regime()`,
  so pinning the front expiry is the top follow-up.
- `strategy.py` wings can be **uneven** (put wing vs call wing) because each long
  leg is chosen independently; `plan_iron_condor` uses the wider wing for width
  and max-loss. Add an equal-wing constraint if desired.
- Credit / fills are **mid-price estimates**, not modelled slippage.
- Data feed is **indicative** — no signed OPRA agreement on the paper account.
  `--feed opra` returns `"OPRA agreement is not signed"`.
- Safety rules: per-trade risk is standardized at **1.5%** everywhere —
  `risk_manager.MAX_RISK_PER_TRADE_PCT = 0.015` is the single source of truth,
  `strategy.py` imports it for sizing, and `CLAUDE.md` rule 2 says 1.5% ($1,500).
  `executor.submit_iron_condor()` enforces every gate before submitting; the
  daily-loss halt, 5% drawdown floor, position cap, and expiry monitor are only
  as good as the `AccountState` / positions the (not-yet-built) driver feeds in.
- `executor.py` MLEG `limit_price` is the mid-based net credit rounded to $0.01;
  no marketable-limit / re-price logic if the order sits unfilled.

## Environment
- Repo: `C:\alpaca-hackathon\trading-agent` (git, `src/trading_agent/` package
  layout). Migrated here 2026-08-31 from `C:\alpaca options ai agent\alpaca-hackathon`
  (flat modules, no git).
- `.venv/` — Python 3.12 (pinned in `pyproject.toml`: `>=3.12,<3.13`; alpaca-py
  deps lacked 3.14 wheels). Setup: `uv venv --python 3.12 && uv pip install -e ".[dev]"`.
- Deps in `pyproject.toml`: `alpaca-py`, `numpy`, `pandas`,
  `pandas-market-calendars`, `python-dotenv`; `[dev]` adds `pytest`.
- Credentials: repo-root `.env` (auto-discovered by `load_credentials()` — cwd
  then repo root); `.env` is git-ignored.

## Tests
`pytest tests/` -> **103 offline tests**, no network (the market calendar ships
its data):
- `test_alpaca_trader.py` (25) — expiry math, `nth_trading_day` incl. Labor Day /
  Thanksgiving / Christmas, OCC parsing, spread metrics, delta-band filtering.
- `test_data.py` (15) — `calculate_iv_percentile`, `evaluate_iv_regime` (Hackathon
  Mode + percentile mode), `calculate_realized_vol` (window, annualization,
  guards).
- `test_strategy.py` (14) — leg selection, IV gate, IV-RV gate, credit/width gate,
  expiry-window gate, position sizing.
- `test_risk_manager.py` (31) — each of the 6 gates with boundary cases (equity
  exactly at each threshold, one contract over the risk cap, 3rd vs 4th position,
  0/1/2 DTE, Labor Day skip, all gates failing together).
- `test_executor.py` (18) — blocked / sticky-halt / oversized orders never reach
  the (fake) client; approved builds the correct MLEG (symbols, SELL/BUY/SELL/BUY,
  qty, limit); API error surfaced not raised; approved-but-unbuildable not sent;
  no-bypass signature + `check_order` always invoked; `IronCondorPlan` -> order.
