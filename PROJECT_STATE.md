# Project State

_Last updated: 2026-09-01_

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
data.py  (strategy-facing layer — PER BASKET TICKER)
  get_market_snapshot(symbol, creds=)  <- strategy.py calls this, once per ticker
  get_underlying_price(creds, symbol)  / get_current_option_chain(.., underlying=)
  get_atm_iv()                - ATM implied vol via parse_occ_symbol
  calculate_realized_vol()    - 10d annualized stdev of daily log returns
  log_iv_reading(symbol,iv,rv,spread)  - one shared iv_history.csv, appended/cycle
  read_iv_history(symbol)     - filters + collapses to 1 point/day  -> percentile
  evaluate_iv_regime()        -> IVRegime (percentile >=10d, else static 15%)
  snapshot adds: symbol, realized_vol, iv_rv_spread, daily_closes
        |  imported by
        v
strategy.py  (regime-aware structure builder — proposes, does not place orders)
  efficiency_ratio() / is_range_bound(window=10, threshold=0.3) / trend_direction()
  select_regime(snapshot) -> RegimeDecision:
     A  IV-RV >= +0.02 & IV elevated        -> plan_iron_condor()
     B  IV-RV <= -0.02 & ER < 0.3           -> plan_long_strangle()   (net debit)
     C  IV-RV <= -0.02 & ER >= 0.3, trend   -> plan_bull_put / plan_bear_call
  pick_expiry / select_short_leg / select_long_leg / select_leg_near_delta
  build_strategy_plan(snapshot) -> IronCondorPlan   (logs "REGIME [SYM]: ...")
        |  its proposal is vetted by
        v
context_gatherer.py  (Contextual Intelligence & Macro-Filter — reads, never trades)
  gather_context(creds, tickers) -> MarketContext  (fail-safe; never raises)
    * macro guard   HIGH_IMPACT_CALENDAR (2026 FOMC/CPI/NFP): upcoming 48h + today
    * vol surface   fetch_vix_proxy()  VIXY level + 5d % change (^VIX not on feed)
    * news          fetch_headlines()  top 4/ticker via Alpaca News API
    * internals     wilder_rsi(closes, 14) per ticker  (pure numpy)
  MarketContext.synthesis() -> "Macro: … | VIX: … | News SYM: … | RSI SYM: … | …"
    .unavailable() -> "No Context Available", macro_today_high_impact = False
  prioritize(tickers, snapshots, ctx) -> best IV-RV spread first, news score ties
        |  its synthesis string + macro flag feed
        v
risk_manager.py  (pre-trade gates + expiry monitor — decides, never trades)
  check_order(order, account) -> RiskDecision   (gates 1-5, collects all fails)
    1 max risk/trade <= 1.5% current equity  (uses ProposedOrder.max_loss when set;
      x AccountState.risk_multiplier, clamped <=1.0 — macro guard halves it)
    2 daily loss halt 2.5% starting   3 total drawdown floor 5% starting (+ sticky)
    4 max 3 positions (GLOBAL across the basket)
    5 defined-risk: matched long/short per right  OR  all-long (long strangle)
  is_macro_safe() / macro_risk_multiplier()  (0.5 on a High-Impact macro day)
  flag_expiring_positions() / trading_days_until()  (gate 6: <=1 session to expiry)
        |  imported by
        v
risk_officer.py  (LLM second opinion — runs AFTER check_order passes)
  warm_up(*, session=) -> bool   (one-token Ollama call; main.py runs it once at startup)
  review_trade(order, snapshot, account, *, featherless_client=, session=) -> OfficerReview
    -> build_prompt() on IV regime + IV-RV + exposure  (one prompt, both providers)
    1. Featherless AI (primary): OpenAI-compatible /v1/chat/completions
    2. Ollama (fallback): local /api/generate  -- on ANY Featherless failure
    -> parse "VERDICT: APPROVE|VETO" + "THESIS:" -> (approved, thesis, provider)
    -> BOTH providers fail / unparseable  =>  fail-safe VETO (ok=False)
    -> logs prompt + provider + raw response + failures (evidence trail)
        |  imported by
        v
executor.py  (broker submission — gated, never bypassable)
  from_plan(plan) -> ProposedOrder  (2 or 4 OCC legs; carries max_loss)
  submit_iron_condor(order, account, *, client=, creds=) -> ExecutionResult
    -> check_order() FIRST; if not approved, nothing is sent
    -> else build LimitOrderRequest(OrderClass.MLEG, 2 or 4 x OptionLegRequest)
       and TradingClient.submit_order(); logs describe() every time
        ^  all five modules are driven by
        |
main.py  (autonomous loop — every AGENT_LOOP_INTERVAL_SECONDS, market hours only)
  startup(): session.json (persist REAL starting_equity, never re-derived) +
             risk_officer.warm_up() + log account id / equity / basket
  run_cycle():
    0. context_gatherer.gather_context() FIRST -> synthesis logged to
       agent_activity.log; macro_risk_multiplier() -> AccountState.risk_multiplier
       (0.5 on a High-Impact macro day; logs MACRO GUARD ACTIVE)
    1. live AccountState (persisted starting_equity, macro multiplier) — GLOBAL
    2. manage_open_positions() FIRST across all tickers — decide_exit():
       credit: +50% / -2x credit ; debit (strangle): +50% / -50% of premium ;
       expiry (flag_expiring_positions); close via Alpaca
    3. update_sticky_halt() + halt_status() precheck (daily / total drawdown)
    4. pre-fetch every AGENT_TICKERS (SPY,QQQ,IWM,TLT) snapshot, then
       context_gatherer.prioritize() -> evaluate best IV-RV spread first:
         evaluate_cycle_decision(market_context=synthesis):
           capacity(<3 global) + halt prechecks, then
           strategy(regime switch) -> risk_manager -> risk_officer(45s) -> executor
         rebuild account after each open so the 3-cap stays global
    5. log one DecisionSummary per ticker (Skipped/Halted/Blocked/Vetoed/Executed);
       each carries the market_context synthesis string
  try/except per ticker + per cycle; console + logs/agent.log
  daily_summary_text() at >= 4pm ET — copy-paste-ready recap (per ticker/structure)
        |  observability wrapped around it (never blocks a cycle)
        v
offhours.py  (Off-Hours Intelligence — pure renderers; main.py time-gates them)
  _maybe_heartbeat()      every AGENT_HEARTBEAT_MINUTES, open OR closed:
      "[ts] HEARTBEAT: Status Idle/Active | Connectivity OK/Error | Memory N IV readings"
  _maybe_morning_brief()  09:00-09:30 ET, 1x/day: premarket_gaps() vs prior close;
      |gap| > AGENT_GAP_ALERT_PCT (0.5%)  ->  PRE-MARKET ALERT (Trending vs Range)
  _maybe_post_mortem()    >= 16:00 ET, 1x/day: DailyActivity funnel folded each
      run_cycle (scans -> proposed -> approved, rm/ro vetoes, regime tally) +
      open unrealized P&L + dominant_regime()  ->  shareable digest
  all three also stream to logs/agent_activity.log
```

| Component | File | Status |
|---|---|---|
| Low-level Alpaca data layer | `alpaca_trader.py` | **Working** (live paper API); `fetch_option_chain(underlying=)` per ticker |
| Strategy-facing market data | `data.py` | **Working** (live); `get_market_snapshot(symbol)` per basket ticker |
| IV history / percentile / regime gate | `data.py` + `iv_history.csv` | Live; one shared file `timestamp,symbol,iv,rv,spread`, appended per ticker/cycle; Hackathon Mode static gate until >= 10 daily rows per symbol |
| Regime-aware strategy | `strategy.py` | **Working** (44 offline tests); regime switch A→condor / B→long strangle / C→bull-put·bear-call, verified live (TLT→strangle). **ADX ≥ 25 disables condors** (→ directional spread / stand aside; 20-25 → Kaufman ER). **IV-relative delta**: high IV → 0.15 (further OTM), low IV → 0.25. Proposes only |
| Pre-trade risk gates + expiry monitor | `risk_manager.py` | **Working** (47 offline tests); decides only. Gate 5 recognises all-long (strangle) as defined-risk; gate 1 uses `ProposedOrder.max_loss` and `AccountState.risk_multiplier` (macro guard halves the cap, clamped ≤ 1.0); **gate 4b correlation guard** — a >0.8 (10d) correlated cluster shares one slot toward the 3-cap — **limit values unchanged** |
| Contextual Intelligence / Macro-Filter | `context_gatherer.py` | **Working** (27 offline tests); verified live. Macro-event guard (static 2026 FOMC/CPI/NFP calendar), VIX proxy (VIXY), Alpaca News headlines, Wilder-14 RSI, **Wilder-14 ADX**, **basket correlation clusters** → one synthesis string. Fail-safe to "No Context Available"; reads only, never trades |
| IntelligenceHub (Quantamental) | `intelligence_hub.py` | **Working** (14 offline tests); verified live. **yfinance-primary** — ^VIX/^VIX3M **term structure** → `PANIC_REGIME` on backwardation, `.news`, closes for RSI, **OHLC for ADX**, correlation clusters; pipe-by-pipe fallback to `context_gatherer` then "No Context Available". `MACRO_DANGER` = High-Impact event ≤48h. `main._gather_market_context` calls it |
| LLM second-opinion gate | `risk_officer.py` | **Working** (50 offline tests); fail-safe VETO. Single-pass `review_trade` + **Bull/Bear/Judge `debate_review`** (top pick) + `post_trade_analysis` -> `lessons_learned.json`. **Featherless AI** (`Qwen/Qwen2.5-72B-Instruct`, `max_tokens` capped) primary, **Ollama** (`llama3.2`) auto-fallback. Prompt carries the regime + structure + `### MACRO CONTEXT` + `### QUANT CLARIFICATION` (contango / neutral RSI are not veto triggers). Live: APPROVES clean condors |
| Broker submission (MLEG) | `executor.py` | **Working** (23 offline tests); 2- or 4-leg MLEG; gate-checked, never bypassable. One live paper MLEG filled (QQQ condor, later hand-unwound) |
| Autonomous loop / driver | `main.py` | **Working** (60 offline tests); running live against the paper account. Context pull + macro guard + prioritised basket loop, global 3-position cap, per-ticker resilience, regime-aware position mgmt + daily summary. First live `run_cycle()` opened + tracked a QQQ condor |
| Off-hours intelligence | `offhours.py` | **Working** (23 offline tests); observability only — never touches the trade path or a limit. Hourly **Heartbeat** (open or closed), 09:00–09:30 ET **Morning Brief** (pre-market gap → PRE-MARKET ALERT / regime read), 16:00 ET **Nightly Post-Mortem** (pipeline funnel + open P&L + dominant regime). Streams to `logs/agent_activity.log`. |
| Order cancel / fill polling | — | Not started (`main.py` closes via `TradingClient.close_position` per leg; no re-price / partial-fill handling) |

## `alpaca_trader.py`

`fetch_option_chain(creds, expiry=None, *, expiry_gte=None, expiry_lte=None,
feed=INDICATIVE, spot=None, strike_window_pct=0.15)` — all filters optional;
omitting them all pulls the full chain (large). `scan_spy_chain()` uses a single
Friday expiry + spot-windowed strikes.

CLI: `python -m trading_agent.alpaca_trader [--weeks-ahead N] [--delta-min X] [--delta-max Y] [--feed indicative|opra] [--json] [-v]`

## `data.py`

`get_market_snapshot(symbol=UNDERLYING, creds=None)` — call once per basket
ticker — returns `{timestamp, symbol, underlying, current_price, atm_iv,
realized_vol, iv_rv_spread, atm_strike, iv_percentile, iv_regime, daily_closes,
chain}` (`iv_regime` is an `IVRegime` dataclass; `iv_rv_spread = atm_iv -
realized_vol`, `None` if either side is missing; `daily_closes` is the 11-session
close series, reused by `strategy.efficiency_ratio`). Chain pull is
`nth_trading_day(1)`..`nth_trading_day(3)`, strikes within +/-5% of spot,
`fetch_option_chain(underlying=symbol)`.

IV history is one shared `iv_history.csv` — `timestamp,symbol,iv,rv,spread`.
`log_iv_reading(symbol, iv, rv, spread)` appends a row **every call** (per ticker
per cycle); `read_iv_history(symbol)` filters to that symbol and collapses to the
**last reading per calendar day** before the percentile calc, so
`IV_HISTORY_MIN_DAYS` stays day-based.

`evaluate_iv_regime()`: with >= `IV_HISTORY_MIN_DAYS` (10) logged IV rows, gate on
IV percentile >= `IV_PERCENTILE_MIN` (50); otherwise Hackathon Mode — eligible
when ATM IV > `STATIC_IV_THRESHOLD` (0.15).

`calculate_realized_vol(closes, window=10)`: sample stdev of daily log returns
over the last `window` sessions x sqrt(252); `None` if < `window + 1` closes.

Tunables: `STRIKE_WINDOW_PCT=0.05`, `EXPIRY_MIN/MAX_TRADING_DAYS=1/3`,
`IV_HISTORY_PATH`, `IV_HISTORY_MIN_DAYS`, `IV_PERCENTILE_MIN`, `STATIC_IV_THRESHOLD`,
`REALIZED_VOL_WINDOW=10`, `TRADING_DAYS_PER_YEAR=252`.

## `strategy.py`

`build_strategy_plan(snapshot=None, *, today=None) -> IronCondorPlan` — the
regime-aware entry point (`main.py` uses this; `build_iron_condor` stays for a
forced condor). It calls `select_regime()`, **logs the choice**
(`REGIME [SYM]: <label> | <detail>` and `STRATEGY [SYM]: <structure> selected`),
dispatches to the matching plan builder, and tags the plan with
`structure` / `regime` / `regime_reason` / `symbol` / `direction`.
`IronCondorPlan` (alias `StrategyPlan`) is the shared result type for every
structure; `net_credit` is **negative for a net debit** (long strangle);
`max_loss_per_contract` is always the true per-contract worst case.

**Range-bound filter** — `efficiency_ratio(prices, window=10)` =
`|net change| / Σ|daily abs changes|`; `is_range_bound(prices, window=10,
threshold=0.3)` -> `True` if ER < 0.3 (range-bound), `False` if >= 0.3
(trending), `None` if < `window+1` prices; `trend_direction()` -> up/down/flat.

**Regime switch** — `select_regime(snapshot)` -> `RegimeDecision(regime, label,
reason, efficiency_ratio, direction)`:

| Regime | Condition | Structure |
|---|---|---|
| A | `iv_rv_spread >= +0.02` **and** `iv_regime.trade_eligible` | `plan_iron_condor` |
| B | `iv_rv_spread <= -0.02` **and** ER < 0.3 | `plan_long_strangle` (buy ~0.25Δ P+C; net debit; max loss = debit) |
| C | `iv_rv_spread <= -0.02` **and** ER >= 0.3 | `plan_bull_put` (trend up) / `plan_bear_call` (trend down) |
| — | neutral spread, or B/C without enough price history | no trade |

The long strangle and both verticals size to `floor($1,500 / max-loss-per-contract)`.
Verticals apply the same `MIN_CREDIT_TO_WIDTH=0.25` gate as the condor.

Tunables (new): `LOW_IV_RV_SPREAD=-0.02`, `EFFICIENCY_RATIO_WINDOW=10`,
`RANGE_BOUND_ER=0.30`, `STRANGLE_DELTA_TARGET=0.25`. Plus the existing
`SHORT_DELTA_*`, `LONG_DELTA_*`, `MIN_CREDIT_TO_WIDTH`, `MIN_IV_RV_SPREAD`,
`MAX_RISK_PER_TRADE` (= `MAX_RISK_PER_TRADE_PCT` × $100k = $1,500, imported from
`risk_manager`).

Run: `python -m trading_agent.strategy [TICKER]`.

## `risk_manager.py`

`check_order(order: ProposedOrder, account: AccountState) -> RiskDecision`
(`.approved`, `.blocks`, `.checks`, plus the computed risk / loss / drawdown
numbers; `.describe()`). Runs gates 1-5 and reports **all** failures. Gate 6 is
`flag_expiring_positions(positions, today=) -> list[ExpiringPosition]` built on
`trading_days_until()` (NYSE-session count, holiday-aware).

Boundary behaviour: risk `<=` cap passes (exactly at cap is OK); daily loss and
drawdown use `>=` (exactly at threshold halts); positions use `len < 3`.

Limits (**unchanged this task**): `MAX_RISK_PER_TRADE_PCT=0.015` (of current
equity), `DAILY_LOSS_HALT_PCT=0.025` / `TOTAL_DRAWDOWN_FLOOR_PCT=0.05` (of
starting equity), `MAX_CONCURRENT_POSITIONS=3`, `EXPIRY_CLOSE_TRADING_DAYS=1`.

Two additive changes for the regime switch, neither loosening a limit:
- **Gate 5** `is_defined_risk()` also returns `True` for an **all-long** position
  (every leg `buy`, qty > 0) — a long strangle can't lose more than the premium
  and is never naked short. Any position with a `sell` leg still goes through the
  unchanged matched-legs rule.
- `ProposedOrder.max_loss` (optional, per-contract $). When set (by debit
  structures), `risk_dollars = max_loss × quantity`; otherwise the classic
  `(wing_width − net_credit) × 100 × quantity`. Gate 1 still caps at 1.5%.

`main.py` assembles one **global** `AccountState` (all basket tickers share the
open-position list) and feeds it to every ticker's `check_order()`.

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
legs=[OptionLegRequest(occ_symbol, side, ratio_qty=1) × (2 or 4)])` ->
`TradingClient.submit_order` (`paper=creds.paper`). `_build_mleg_request` now
accepts **2 or 4** legs (vertical / strangle / condor); 3 is still rejected. For
a long strangle `net_credit < 0` so `limit_price` is the debit.

`from_plan` (alias of `from_iron_condor_plan`) -> `ProposedOrder` with the OCC
symbols, `suggested_contracts`, and `max_loss=plan.max_loss_per_contract`; raises
if `not plan.eligible` or the plan has no legs / no sizing — a strategy-rejected
plan is never convertible to an order, no override.

Not exercised against the live API yet; `client=` injection is for tests only,
not a gate bypass.

## `risk_officer.py`

`review_trade(order: ProposedOrder, snapshot: dict, account: AccountState, *,
featherless_api_key=None, featherless_model=None, featherless_base_url=None,
featherless_client=None, host=None, model=None, timeout=None, session=None)
-> OfficerReview`.

A **second-opinion layer** on top of `risk_manager` — the caller runs it only
after `check_order().approved`. `build_prompt()` renders one prompt from the IV
regime, ATM IV, realized vol, IV-RV spread, and current exposure (order
max-loss %, open positions, day P&L, drawdown); `parse_review()` turns any LLM
reply into `OfficerReview(approved, thesis, model, ok, raw_response, error,
provider)`. Both are pure and unit-tested, and **both providers use them**.

**Two providers, tried in order:**
1. **Featherless AI** (primary) — hosted, OpenAI-compatible, via the `openai`
   package: `OpenAI(base_url, api_key, max_retries=0, timeout=45)` →
   `chat.completions.create(model, messages=[{role:"user", content:prompt}])`,
   reading `choices[0].message.content`. Skipped entirely if no
   `FEATHERLESS_API_KEY`.
2. **Ollama** (fallback) — local `/api/generate`. Used automatically on **any**
   Featherless failure: connection error, timeout, auth/API error, no choices,
   empty content, or an unparseable (`VERDICT`-less) reply.

**Fail-safe:** only if **both** providers fail (or both are unparseable) →
`approved=False, ok=False, provider="none"` (a broken reasoning step never
green-lights a trade). `error` carries both providers' failure detail;
`raw_response` keeps the last body received, for the evidence trail.

`build_prompt` now opens with a **STRATEGY REGIME** block (ticker, structure,
regime label + detail — passed in via the snapshot by `main.evaluate_new_trade`)
and labels `net_credit < 0` as a net debit.

Config via env (auto-loaded from `.env`): `FEATHERLESS_API_KEY`,
`FEATHERLESS_MODEL` (default `Qwen/Qwen2.5-7B-Instruct`), `FEATHERLESS_BASE_URL`
(default `https://api.featherless.ai/v1`), `FEATHERLESS_TIMEOUT` (45s);
`OLLAMA_HOST` (default `http://localhost:11434`), `OLLAMA_MODEL` (default
`llama3.2`), `OLLAMA_TIMEOUT` (120s), `OLLAMA_WARM_UP_TIMEOUT` (180s). Every
call logs the prompt, the provider, the raw response, and every failure.

`warm_up(*, host=None, model=None, timeout=None, session=None) -> bool` fires a
throwaway one-token generation to force the **Ollama** model resident (Featherless
is hosted, no cold-load). `main.py` calls it once before the loop so the first
fallback doesn't pay the ~2 GB cold-load. Never raises — returns `False` if
Ollama is down at startup.

Driven by `main.py` (after `risk_manager.check_order()` passes).

## `main.py`

The autonomous loop. `run_forever(Config.from_env())` — or the `trading-agent`
console script — runs one `run_cycle()` every `AGENT_LOOP_INTERVAL_SECONDS`
(default 900), **only when `TradingClient.get_clock().is_open`**.

**Startup** (`startup()`): if `session.json` is absent, fetch the REAL equity
from Alpaca and persist it as `starting_equity`; if present, load it and **never
re-derive** it (that would corrupt the 5% drawdown floor across restarts). Then
`risk_officer.warm_up()` and a startup log line (timestamp, account id, equities).

**Each cycle** (`run_cycle()`), all wrapped so one failure logs and continues:
1. Live **global** `AccountState` (persisted `starting_equity`, `current_equity`
   from `account.equity`, `day_start_equity` from `last_equity`) — the open-
   position list spans all basket tickers.
2. **Manage open positions first**, across the whole basket. `decide_exit()`
   (pure), in the spec's order: **profit-target** → **stop-loss** → **expiry**
   (`flag_expiring_positions`, ≤ 1 trading day). Credit structures: +50% / -2×
   credit. Debit (long strangle): +50% / -50% of the premium
   (`AGENT_DEBIT_STOP_FRACTION`). Closes via `TradingClient.close_position` per
   leg; records a history event with `symbol` + `structure`.
3. `update_sticky_halt()` latches the comp-level halt; `halt_status()`
   short-circuits new trading (same thresholds, persisted `starting_equity`).
4. **For each ticker in `AGENT_TICKERS`**: `get_market_snapshot(sym)` →
   `evaluate_cycle_decision()` — capacity (< 3, **global**) + halt prechecks,
   then `evaluate_new_trade()` runs **strategy (`build_strategy_plan`, regime
   switch) → risk_manager → risk_officer (45s) → executor in that exact order**;
   a rejection at any stage returns immediately. `account` is rebuilt after each
   open so the 3-cap stays global. A bad ticker's snapshot logs and the loop
   continues to the next.
5. One `DecisionSummary` logged per ticker
   (`Skipped` / `Halted` / `Blocked` / `Vetoed` / `Executed` / `Error`);
   `CycleReport.decision` surfaces the most consequential one.

**Daily summary**: at/after 16:00 ET (`_maybe_daily_summary`, once per ET day)
`daily_summary_text()` emits a copy-paste-ready recap — equity, day P&L, trades
opened/closed today **with ticker + structure + regime**, open positions per
ticker, hashtags.

**Config — all env vars, nothing hardcoded**: `AGENT_TICKERS`
(`SPY,QQQ,IWM,TLT`), `AGENT_LOOP_INTERVAL_SECONDS`, `AGENT_LOG_LEVEL`,
`AGENT_ENV_FILE` (loaded first, wins), `AGENT_SESSION_FILE`, `AGENT_LOG_FILE`,
`AGENT_REVIEW_TIMEOUT_SECONDS` (45), `AGENT_PROFIT_TARGET_FRACTION` (0.50),
`AGENT_STOP_LOSS_MULTIPLE` (2.0, credit stop), `AGENT_DEBIT_STOP_FRACTION` (0.50,
long-strangle stop). Logs to console **and** `logs/agent.log` (both UTF-8).
`session.json` / `logs/` are git-ignored.

Pure, offline-tested seams: `decide_exit`, `value_condor`, `manage_open_positions`,
`halt_status`, `update_sticky_halt`, `reconcile_account_state`,
`evaluate_new_trade` / `evaluate_cycle_decision` (all pipeline stages injectable),
`load_or_init_session`, `daily_summary_text`.

## Known limitations / follow-ups
- **`main.py` has not run a live cycle** — `startup()`, `get_clock()`, and
  multi-ticker regime detection are verified against the paper account, but no
  `run_cycle()` has been executed live (it would place a real paper MLEG order).
- **Regime thresholds are first-pass assumptions.** "IV << RV" is
  `iv_rv_spread <= -0.02` (mirrors the +0.02 "rich" side); `RANGE_BOUND_ER=0.30`
  is Kaufman's common default. In a genuinely low-IV tape the Regime C credit
  spread often can't clear the 25% credit/width gate, so C frequently produces no
  trade — expected, but worth watching. Trend direction is a simple
  first-vs-last close sign over 10 sessions.
- **Long strangle is a raw 2-leg debit** (buy ~0.25Δ P + C), sized purely by the
  1.5% cap. No profit/loss modelling beyond `decide_exit`'s ±50%-of-premium
  rule; no delta hedge.
- **Chain valuation for open positions** (`value_condor`) pulls the whole
  near-dated expiry chain per ticker (`strike_window_pct=None`); larger payload
  than the ±5% snapshot pull, but only runs when positions are open.
- **Position P&L is a mid-price re-quote.** `value_condor()` re-prices the four
  legs from a fresh `fetch_option_chain(expiry=...)` mid; if any leg has no
  quote that condor is skipped for the cycle (logged). No use of Alpaca's own
  `unrealized_pl` per position, and no slippage model on close.
- **Condor↔order reconciliation is by our own tracking id**, stored in
  `session.json`. A fill that partially executes, or a manual close in the
  Alpaca UI, is not reconciled back into `session.open_condors`.
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
- Deps in `pyproject.toml`: `alpaca-py`, `numpy`, `openai`, `pandas`,
  `pandas-market-calendars`, `python-dotenv`, `requests`, `tzdata` (ET clock on
  Windows); `[dev]` adds `pytest`. `uv.lock` regenerated after each dep change.
- `risk_officer.py` LLM providers: **Featherless AI** (primary, hosted;
  `FEATHERLESS_API_KEY` in `.env`) and **local Ollama** (`http://localhost:11434`,
  v0.33.2, `llama3.2:latest` 3.2B ~2 GB) as the automatic fallback.
- Credentials + keys: repo-root `.env` (`ALPACA_*`, `FEATHERLESS_API_KEY`,
  `FEATHERLESS_MODEL`), auto-discovered (cwd then repo root; or point
  `AGENT_ENV_FILE` at a specific file); `.env` is git-ignored — no key is ever
  hardcoded in source.
- `main.py` runtime state: `session.json` (persisted `starting_equity`, sticky
  halt, tracked positions incl. `symbol`/`structure`, history, off-hours markers
  `last_heartbeat_at` / `last_morning_brief_date` / `last_post_mortem_date` and
  the `daily_activity` funnel map — last 10 days), `logs/agent.log` and
  `logs/agent_activity.log` (off-hours-intelligence audit trail) — all
  git-ignored. Basket via `AGENT_TICKERS` (default `SPY,QQQ,IWM,TLT`). Off-hours
  knobs: `AGENT_ACTIVITY_LOG`, `AGENT_HEARTBEAT_MINUTES` (default 60),
  `AGENT_GAP_ALERT_PCT` (default 0.5).

## Tests
`pytest tests/` -> **331 offline tests**, no network (the market calendar ships
its data; both LLM providers, the news/VIX pulls and Alpaca are mocked):
- `test_alpaca_trader.py` (25) — expiry math, `nth_trading_day` incl. Labor Day /
  Thanksgiving / Christmas, OCC parsing, spread metrics, delta-band filtering.
- `test_data.py` (19) — `calculate_iv_percentile`, `evaluate_iv_regime`,
  `calculate_realized_vol`; `log_iv_reading` (new `timestamp,symbol,iv,rv,spread`
  schema, appended every call for every ticker); `read_iv_history(symbol)`
  filters by symbol + collapses to one point per calendar day + tolerates blank IV.
- `test_strategy.py` (32) — leg selection; iron-condor gates; **efficiency ratio /
  `is_range_bound` / `trend_direction`**; `select_regime` A/B/C/none (incl. "IV
  rich but not elevated" and "IV cheap but no price history" -> none);
  `plan_long_strangle` (2 long legs, net debit, sized ≤ 1.5%, blocked when debit >
  cap); `plan_bull_put` / `plan_bear_call` (credit spreads, 25% gate still
  applies); `build_strategy_plan` dispatch + explicit regime logging.
- `test_risk_manager.py` (43) — the 6 gates with boundary cases; `is_defined_risk`
  all-long (long strangle / single long) passes, all-long-with-zero-qty and any
  mixed short leg still fail; `ProposedOrder.max_loss` overrides the credit
  formula for debit structures and gate 1 caps a strangle at 1.5%; **macro guard**:
  `is_macro_safe` / `macro_risk_multiplier` (0.5 on a High-Impact day), a halved
  `AccountState.risk_multiplier` blocks a trade the full cap would pass,
  `MAX_RISK_PER_TRADE_PCT` byte-unchanged, a multiplier > 1.0 is clamped to 1.0.
- `test_context_gatherer.py` (19) — Wilder RSI (100 rally / 0 selloff / 50 flat /
  None short / worked-series band) + `classify_rsi` bands; `upcoming_high_impact`
  48h window + `high_impact_today`; `score_headlines` +/-/0; `synthesis()` four
  sections on one line; `MarketContext.unavailable` -> "No Context Available" +
  macro flag False; `gather_context` happy path, news-only failure degrades just
  news, total failure -> unavailable, never raises on bad creds; `prioritize`
  orders by IV-RV spread then news score.
- `test_executor.py` (23) — blocked / sticky-halt / oversized never reach the
  (fake) client; 4-leg and **2-leg** MLEG build (strangle: BUY/BUY, limit =
  abs(debit)); 3-leg still rejected; API error surfaced not raised;
  no-bypass signature; `from_plan` alias carries `max_loss`; strangle round-trips
  through `submit`.
- `test_risk_officer.py` (38) — `parse_review` verdict forms incl. Featherless
  trailing-space style; **Featherless primary** (APPROVE/VETO used, Ollama not
  called, client built from key); **Featherless → Ollama fallback** on
  connection / timeout / auth / malformed / unparseable / empty-content /
  no-choices; **no key → Ollama is primary**; **both fail → fail-safe VETO**
  (`provider="none"`, error names both, last raw kept); one prompt is identical
  across both providers; logging of prompt / provider / fallback / both-failed;
  `warm_up()` one-token request, True/False without raising, logs outcome;
  **`### MACRO CONTEXT`** section carries the supplied synthesis string and
  fails safe to "No Context Available".
- `test_main.py` (57) — **position management**: `value_condor` net-mid /
  missing-leg; `decide_exit` profit-target (fires at exactly 50%), stop-loss
  (fires at exactly 2× credit lost), expiry-when-flagged, none-when-healthy,
  profit/stop both beat expiry, configurable thresholds; `manage_open_positions`
  closes only triggered condors, records history + P&L, keeps the position if
  `close_fn` raises. **Gate sequencing**: full approval hits
  `strategy → to_order → risk_manager → risk_officer → executor` in that exact
  recorded order; rejection at strategy / risk_manager / risk_officer each stops
  before every later stage; executor non-submission → `error`; `review_fn` gets
  the 45s timeout; halt / capacity prechecks skip the pipeline entirely.
  Plus session persistence, sticky-halt latch, account reconciliation, daily
  summary; **`decide_exit` debit-aware** (strangle closes at +50% / -50% of the
  premium; credit logic unchanged); **multi-ticker `run_cycle`** evaluates every
  basket ticker, caps positions at 3 globally, and isolates one bad ticker;
  `TrackedCondor` symbol/structure round-trip. **Off-hours wiring**: `run_cycle`
  folds each cycle's decisions into a persisted daily-activity funnel;
  `_maybe_heartbeat` fires once per interval (and marks the session),
  `_maybe_morning_brief` only inside 09:00–09:30 ET and once/day,
  `_maybe_post_mortem` only at/after 16:00 ET and once/day; legacy `session.json`
  (no off-hours keys) still loads. **Context wiring**: `run_cycle` gathers the
  market context first, logs the `MARKET CONTEXT` block, stamps the synthesis on
  every `DecisionSummary`, evaluates tickers in `prioritize()` order, and threads
  `AccountState.risk_multiplier = 0.5` on a macro day; a `gather_context` blow-up
  degrades to "No Context Available" without stopping the cycle.
- `test_offhours.py` (23) — `count_iv_readings` (rows only, missing file → 0);
  **Heartbeat** render matches the exact `[ts] HEARTBEAT: Status … | Connectivity
  … | Memory N IV readings stored.` format, Active/Idle + OK/Error; `interval_elapsed`
  hourly gate incl. no-stamp / unparseable / custom gap; **Morning Brief** window
  09:00–09:30, `TickerGap.gap_pct` + significance, >0.5% → PRE-MARKET ALERT +
  "TRENDING", all-flat → "RANGE-BOUND", no-quotes path; **Post-Mortem**
  `accumulate_activity` funnel (scans / proposed / approved / rm+ro vetoes /
  regime tally, cumulative, plan-less tolerant), `dominant_regime` bucketing,
  `post_mortem_text` digest + no-open-positions path, `DailyActivity` dict
  round-trip.
