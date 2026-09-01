# Work Summary — Multi-Ticker Options Trading Agent

_Generated: 2026-08-31 · last updated: 2026-09-01_

Rolling summary of everything built so far for the Alpaca x LabLab.ai hackathon
agent. Companion to `PROJECT_STATE.md` (current architecture) and `DEVLOG.md`
(dated change entries).

---

## 1. Executive summary

A **multi-ticker market-data layer**, a **regime-aware structure builder**, a
**pre-trade risk manager**, a **gated order executor**, an **LLM second-opinion
gate**, and the **autonomous loop** that drives them across a `SPY/QQQ/IWM/TLT`
basket are built and tested — **306 offline tests** (the data path, the LLM
providers, the context layer, agent startup, and one live `run_cycle` that opened
a QQQ condor are also verified against the Alpaca paper account):

- **`alpaca_trader.py`** — low-level Alpaca access: credentials, spot price,
  option-chain fetch with `underlying=` + expiry/strike filters, OCC symbol
  parsing, a 20–30 delta scanner, and a CLI.
- **`data.py`** — strategy-facing layer, **per basket ticker**
  (`get_market_snapshot(symbol)`): near-the-money chain pull, ATM IV, 10-day
  realized vol + IV−RV spread, `daily_closes`, the IV-regime gate, and one shared
  `iv_history.csv` (`timestamp,symbol,iv,rv,spread`, appended per ticker/cycle;
  `read_iv_history(symbol)` collapses to one point/day for the percentile).
- **`strategy.py`** — **dynamic market-regime switch**. `select_regime()` reads
  the IV-RV spread and a Kaufman **efficiency ratio** (`is_range_bound`,
  window 10, threshold 0.3): Regime A (IV rich + elevated) → **iron condor**,
  B (IV ≪ RV + range-bound) → **long strangle**, C (IV ≪ RV + trending) →
  **bull put / bear call**. `build_strategy_plan()` logs the choice explicitly,
  dispatches, and sizes every structure within the **$1,500** (1.5%) cap.
  Proposes only.
- **`risk_manager.py`** — six hard gates: 1.5%/trade risk cap, 2.5% daily-loss
  halt, 5% total-drawdown floor (+ sticky halt), **max 3 concurrent positions
  globally across the basket**, defined-risk invariant, and a ≤1-session-to-expiry
  force-close flag. **Limit values unchanged**; Gate 5 additionally recognises an
  all-long position (long strangle) as defined-risk, and `ProposedOrder.max_loss`
  lets a debit structure feed gate 1 its true worst case. Decides only.
- **`risk_officer.py`** — LLM second opinion. After `check_order()` approves,
  asks an LLM to APPROVE/VETO with a 2-3 sentence thesis on the IV regime, IV-RV
  spread, and exposure. **Featherless AI** (hosted, primary) with **local Ollama**
  as an automatic fallback on any Featherless failure; same prompt + parser for
  both. Parses `VERDICT:` + `THESIS:` into `(approved, thesis, provider)`.
  **Fails safe to VETO only if both providers fail**; logs the prompt, provider,
  raw response, and failures as an evidence trail.
- **`executor.py`** — `submit_iron_condor()` runs `check_order()` first and
  **sends nothing** unless approved (no bypass parameter). Approved orders go out
  as one `OrderClass.MLEG` limit order with the four OCC legs. Every attempt logs
  the full `RiskDecision.describe()`.
- **`main.py`** — the autonomous loop. Every 15 min in market hours: one global
  `AccountState` (persisted `starting_equity`), **manage open positions first**
  across the basket (credit +50%/-2×, debit strangle +50%/-50%, expiry), latch
  the sticky halt, then **for each ticker** run **strategy (regime switch) →
  risk_manager → risk_officer → executor in that exact order** while under the
  global 3-position cap. One `DecisionSummary` per ticker; per-ticker + per-cycle
  try/except; daily copy-paste recap (per ticker/structure/regime) at 4 pm ET.
- **`offhours.py`** — **Off-Hours Intelligence**, around the loop, never in it
  (no risk/strategy change, stdlib only). Hourly **Heartbeat** (open or closed:
  status / connectivity / IV-readings-stored), a 09:00–09:30 ET **Morning Brief**
  (pre-market gap per ticker vs prior close → **PRE-MARKET ALERT** + Trending-vs-
  Range read when `|gap| > 0.5%`), and a 16:00 ET **Nightly Post-Mortem** (the
  day's pipeline funnel — scans → proposed → approved, vetoes per gate — plus
  open unrealized P&L and the dominant regime). All three also stream to
  `logs/agent_activity.log`.
- **`context_gatherer.py`** — **Contextual Intelligence & Macro-Filter**
  (numpy + `alpaca-py` only). `gather_context()` pulls a macro-event guard
  (bundled 2026 FOMC / CPI / NFP calendar — "upcoming 48h" + "today"), a VIX
  proxy (VIXY level + 5-day change), Alpaca News headlines (top 4/ticker), and a
  Wilder-14 RSI/ticker, and folds them into one `synthesis()` string. Every pull
  fails safe; a total wipe-out → `"No Context Available"`. `prioritize()` orders
  eligible tickers by IV-RV spread (news score breaks ties). Feeds the
  risk_officer's `### MACRO CONTEXT` block; a High-Impact day sets
  `AccountState.risk_multiplier = 0.5` so gate 1's cap halves for that cycle
  (the 1.5% constant is untouched).
- **`intelligence_hub.py`** — **Quantamental context** (adds `yfinance`).
  `gather()` prefers yfinance for the ^VIX/^VIX3M **term structure** (ratio > 1
  = backwardation → `PANIC_REGIME`), `.news`, and closes for RSI, falling back
  pipe-by-pipe to the `context_gatherer` fetchers, then "No Context Available".
  `MACRO_DANGER` = a High-Impact event within 48h. `strategy.select_regime`
  turns either flag into a forced long strangle (vetoes short-vol); `strategy`
  also has **dynamic delta scaling** (0.10 delta at low IV, 0.30 at high) and
  `rank_basket()` (the relative-value optimiser). The risk_officer runs a
  **Bull/Bear/Judge debate** on the #1-ranked candidate and a
  **self-correction loop** (`post_trade_analysis` → `lessons_learned.json`,
  re-injected into every future judge prompt).

Per-trade risk is **1.5% everywhere**: `risk_manager.MAX_RISK_PER_TRADE_PCT` is
the single source of truth, `strategy.py` imports it, and `CLAUDE.md` rule 2 says
1.5% ($1,500). `main.py` drives the full pipeline; a first live `run_cycle()`
opened + tracked a QQQ iron condor (later hand-unwound) and the agent is running
against the paper account.

---

## 2. Account status (Alpaca paper)

| Field | Value |
|---|---|
| Account number | `PA3ARUWVYYGH` |
| Portfolio value / equity / cash | $100,000.00 |
| Buying power | $400,000.00 (4× margin) |
| Options buying power | $100,000.00 |
| Options trading level | 3 |
| Open positions | none |
| Status | ACTIVE, no trading/transfer blocks |

Checked three times over the session — untouched, well above the $95,000
drawdown-halt threshold.

---

## 3. What was built

### 3.1 `CLAUDE.md`
System-instructions file saved to the project root (project context, hard safety
rules, dev workflow commands, state-sync requirement).

### 3.2 `alpaca_trader.py` — low-level Alpaca data layer

| Function | Purpose |
|---|---|
| `load_credentials()` | Reads `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from env or a `.env` file (project root, then `alpaca-mcp-server/.env`). Never hardcoded. |
| `get_spot_price(creds, symbol, method=)` | `"trade"` = last trade price; `"quote_mid"` = NBBO midpoint, falling back to last trade when the quote is empty/one-sided. |
| `get_daily_closes(creds, symbol, *, sessions=11)` | Last N daily closing prices (oldest first) via `StockBarsRequest` / `TimeFrame.Day`. Best-effort → `[]` on failure. |
| `next_friday(from_date=None, weeks_ahead=0)` | Next upcoming Friday expiry (today if today is Friday). |
| `nth_trading_day(n, from_date=None)` | Date `n` trading sessions out, on the **NYSE (XNYS)** calendar — skips weekends *and* exchange holidays (e.g. Fri 09-04 → Tue 09-08, skipping Labor Day). `trading_sessions(a, b)` lists sessions in a range. |
| `parse_occ_symbol(symbol)` | `SPY260904P00763000` → `("SPY", 2026-09-04, "put", 763.0)`. |
| `fetch_option_chain(creds, expiry=None, *, expiry_gte=None, expiry_lte=None, feed=INDICATIVE, spot=None, strike_window_pct=0.15)` | Chain fetch; all filters optional. With `spot` + `strike_window_pct`, strikes are limited to `spot × (1 ± pct)`. |
| `OptionContract` (dataclass) | `bid`, `ask`, `mid`, `spread` (= ask − bid), `spread_pct` (= spread / mid × 100), `delta`, `abs_delta`, `implied_volatility`. |
| `build_contracts(snapshots)` | Snapshots → `list[OptionContract]`; drops rows without a two-sided quote. |
| `filter_delta_band(contracts, right, delta_min=0.20, delta_max=0.30)` | In-band puts/calls, sorted by `|delta|`. |
| `scan_spy_chain(...)` | Ties it together → `ChainScan(calls, puts, spot, expiry, …)`. |

**CLI:** `python -m trading_agent.alpaca_trader [--weeks-ahead N] [--delta-min X] [--delta-max Y] [--feed indicative|opra] [--json] [-v]`

Sample run (2026-08-31, spot ≈ $767, expiry 2026-09-04): 380 quotable contracts,
4 puts + 3 calls in the 20–30 delta band, spreads $0.01–0.06 (0.8–4.3% of mid).

### 3.3 `data.py` — strategy-facing market-data layer

Imports its primitives from `alpaca_trader.py` (no duplicate client/credential/
chain code).

| Function | Purpose |
|---|---|
| `get_underlying_price(creds=None)` | Wraps `get_spot_price(method="quote_mid")`. |
| `get_current_option_chain(creds, current_price)` | Chain for contracts expiring in the next **1–3 trading days**, strikes within **±5%** of spot. |
| `get_atm_iv(chain, current_price)` | Nearest-strike contract's implied volatility as today's reference IV. |
| `calculate_realized_vol(closes, *, window=10, annualization=252)` | Sample stdev (ddof=1) of daily log returns over the last `window` sessions × √252. `None` if < `window + 1` positive closes. |
| `log_daily_iv(...)` / `read_iv_history(...)` | Append/read `iv_history.csv` (`timestamp, underlying_price, atm_iv`). `log_daily_iv` writes **at most one row per calendar day** — a no-op if today's date is already present (first reading of the day wins), so `main.py` can call it every cycle. |
| `calculate_iv_percentile(current_iv, series)` | Percentile rank vs. history; `None` until ≥ 10 rows or if IV is missing. |
| `evaluate_iv_regime(current_iv, series)` | The gate. Returns `IVRegime(atm_iv, iv_percentile, mode, trade_eligible, reason)`. `mode="percentile"` (eligible ≥ 50) once ≥ 10 IV days logged; else `mode="hackathon_static"` — **Hackathon Mode**, eligible when ATM IV > 15%. |
| `get_historical_iv_series(...)` | Deliberate placeholder returning `[]` — real history is accumulated from the daily log, not faked. |
| `get_market_snapshot()` | Entry point for `strategy.py`. Returns `{timestamp, underlying, current_price, atm_iv, realized_vol, iv_rv_spread, atm_strike, iv_percentile, iv_regime, chain}` — `iv_rv_spread = atm_iv − realized_vol` (`None` if either is missing). |

Tunables (module constants): `STRIKE_WINDOW_PCT = 0.05`,
`EXPIRY_MIN_TRADING_DAYS = 1`, `EXPIRY_MAX_TRADING_DAYS = 3`, `IV_HISTORY_PATH`,
`IV_HISTORY_MIN_DAYS = 10`, `IV_PERCENTILE_MIN = 50.0`, `STATIC_IV_THRESHOLD = 0.15`,
`REALIZED_VOL_WINDOW = 10`, `TRADING_DAYS_PER_YEAR = 252`.

**Run:** `python -m trading_agent.data` → prints price / ATM strike / ATM IV / realized vol /
IV−RV spread / IV percentile / IV-regime verdict / chain size.

### 3.4 `strategy.py` — dynamic market-regime switch

Consumes `get_market_snapshot(symbol)`. **Proposes** a defined-risk structure
chosen by the volatility regime; places no orders.

**Range-bound filter (efficiency ratio):**

| Function | Purpose |
|---|---|
| `efficiency_ratio(prices, window=10)` | `|net change over window| / Σ|daily abs changes|`. 1.0 = a straight line; ~0 = chop. `None` if < `window+1` prices; `0.0` if flat. |
| `is_range_bound(prices, window=10, threshold=0.3)` | `True` if ER < `threshold` (range-bound), `False` if ≥ (trending), `None` without enough history. |
| `trend_direction(prices, window=10)` | `"up"` / `"down"` / `"flat"` (first vs last close). |

**Regime switch** — `select_regime(snapshot) -> RegimeDecision(regime, label, reason, efficiency_ratio, direction)`:

| Regime | Condition | Structure | Builder |
|---|---|---|---|
| **A** High vol | `iv_rv_spread ≥ +0.02` **and** IV elevated (`iv_regime.trade_eligible`) | Iron Condor | `plan_iron_condor` |
| **B** Low vol / range-bound | `iv_rv_spread ≤ −0.02` **and** ER < 0.3 | **Long Strangle** (buy ~0.25Δ P + C; net **debit**; max loss = debit) | `plan_long_strangle` |
| **C** Low vol / trending | `iv_rv_spread ≤ −0.02` **and** ER ≥ 0.3 | **Bull Put** (trend up) / **Bear Call** (down) credit spread | `plan_bull_put` / `plan_bear_call` |
| — | neutral spread, or B/C without price history | no trade | — |

`build_strategy_plan(snapshot, *, today=None)` is the entry point `main.py` uses:
it runs `select_regime`, **logs the choice explicitly** (`REGIME [SPY]: Regime B:
Low IV / Range-Bound -> Long Strangle | <quantitative detail>` and
`STRATEGY [SPY]: long_strangle selected — …`), dispatches, and tags the plan with
`structure` / `regime` / `regime_reason` / `symbol` / `direction` — which
`main.evaluate_new_trade` forwards into the `risk_officer` prompt and the daily
summary. `IronCondorPlan` (alias `StrategyPlan`) is the shared result type;
`net_credit < 0` marks a debit; `max_loss_per_contract` is always the true worst
case; every structure sizes to `floor($1,500 / max_loss_per_contract)`. Verticals
still enforce the **25%** credit/width gate.

New tunables: `LOW_IV_RV_SPREAD = -0.02`, `EFFICIENCY_RATIO_WINDOW = 10`,
`RANGE_BOUND_ER = 0.30`, `STRANGLE_DELTA_TARGET = 0.25`. Existing delta / credit /
risk constants unchanged.

**Run:** `python -m trading_agent.strategy [TICKER]`.

Live check (2026-09-01): SPY/QQQ/IWM neutral (no trade); **TLT → Regime B Long
Strangle** — `IV 0.061 ≪ RV (spread −0.062)`, `ER 0.230 < 0.3` → real 82P/83C
strangle proposed at $0.20 debit.

### 3.5 `risk_manager.py` — pre-trade gates + expiry monitor

Pure/deterministic (no network, no clock — pass `today`). **Decides only**;
never places or cancels orders.

| Function / model | Purpose |
|---|---|
| `check_order(order, account)` → `RiskDecision` | Runs gates 1–5, collects **every** failure (not short-circuit). `RiskDecision` = `approved`, `blocks[]`, `checks{gate: passed}`, plus computed `order_risk` / `max_risk_allowed` / `daily_loss` / `total_drawdown` / … and `.describe()`. |
| `is_defined_risk(legs)` | Gate 5 — **all-long** (every leg `buy`, qty > 0 → long strangle) OR per right bought == sold. Any `sell` leg still goes through the matched-legs rule; naked / qty-mismatch / empty / bad-action still rejected. |
| `flag_expiring_positions(positions, today=)` → `list[ExpiringPosition]` | Gate 6 — positions ≤ `EXPIRY_CLOSE_TRADING_DAYS` (1) NYSE sessions from expiry. |
| `trading_days_until(target, today=)` | NYSE-session count to a date (0 if on/past); holiday-aware via `trading_sessions`. |
| `OrderLeg`, `ProposedOrder` (`.risk_dollars`, optional `max_loss`), `OpenPosition`, `AccountState` | Inputs. `ProposedOrder.max_loss` (per-contract $, set by debit structures) overrides the `(wing − credit)` formula in `.risk_dollars`; gate 1 still caps at 1.5%. `AccountState.open_positions` is **global across the basket**. |

**Gates & boundaries**

| # | Gate | Threshold | At exactly the threshold |
|---|---|---|---|
| 1 | Max risk / trade | `risk_dollars ≤ 1.5% × current_equity` — `risk_dollars` = `max_loss × qty` when set, else `(wing − credit) × 100 × qty` (`MAX_RISK_PER_TRADE_PCT`, shared with `strategy.py`) | **allowed** (`≤`) |
| 2 | Daily loss halt | `day_start − current ≥ 2.5% × starting_equity` | **halts** (`≥`) |
| 3 | Total drawdown floor | `starting − current ≥ 5% × starting_equity`, or `trading_halted` | **halts** (`≥`) |
| 4 | Max concurrent positions | `len(open_positions) < 3` — **counted globally over the whole basket** | 3 open → **blocks** the 4th |
| 5 | Defined-risk invariant | all-long (strangle) OR long == short per right | — |
| 6 | Expiration auto-close | `trading_days_until(expiry) ≤ 1` | flags 0- and 1-DTE |

**Run:** `python -m trading_agent.risk_manager` → demo order through `.describe()` + an expiry flag.

`check_order()` is called only by `executor.submit_iron_condor()`; the caller
must still assemble and persist `AccountState`.

### 3.6 `executor.py` — gated broker submission

Turns a risk-approved order into one Alpaca **`OrderClass.MLEG`** limit order.
Talks to the broker; decides nothing.

| Function / model | Purpose |
|---|---|
| `submit_iron_condor(order, account, *, client=None, creds=None)` → `ExecutionResult` | **Runs `check_order()` first.** Not approved → `submitted=False`, nothing sent. Approved → build MLEG request → `TradingClient.submit_order`. Logs `RiskDecision.describe()` every time (`WARNING "ORDER BLOCKED"` / `INFO "ORDER APPROVED"` + `"ORDER SUBMITTED id=…"`). Broker exceptions are caught into `ExecutionResult.error`, never raised. |
| `from_plan(plan)` (alias `from_iron_condor_plan`) → `ProposedOrder` | Maps any `strategy.IronCondorPlan` (condor / strangle / vertical) → order with the OCC symbols, `suggested_contracts`, and `max_loss=plan.max_loss_per_contract`. Raises if `not plan.eligible` (no override) or no legs / no sizing. |
| `_build_mleg_request(order)` → `LimitOrderRequest` | `order_class=MLEG`, `qty=N`, `time_in_force=DAY`, `limit_price=round(abs(net_credit), 2)` (= debit for a strangle), `legs=[OptionLegRequest(occ_symbol, side, ratio_qty=1) × (2 or 4)]`. Rejects anything other than 2 or 4 legs, `qty<1`, or a leg with no symbol. |
| `ExecutionResult` | `submitted`, `decision`, `order`, `submitted_request`, `error`, `.order_id`. |

**No bypass:** the signature is exactly `order, account, client, creds` — no
`force` / `skip_checks` / `override`. A test asserts that, and that `check_order`
is invoked on every call. `client=` is a test seam, not a gate bypass.

**Run:** `python -m trading_agent.executor` → no-network demo: prints a blocked oversized order
and the MLEG request a sane order would send. Never submits.

Not done: not run against the live API; no fill polling, re-price, or cancel; the
MLEG `limit_price` is the mid-based net credit with no working/marketable logic.

### 3.7 `risk_officer.py` — LLM second-opinion gate

Runs **after** `risk_manager.check_order()` approves — an additional judgment
layer, not a replacement. Two LLM providers, tried in order; decides nothing on
its own beyond parsing the reply.

**Providers:**
1. **Featherless AI** (primary) — hosted, OpenAI-compatible, via the `openai`
   package: `OpenAI(base_url, api_key, max_retries=0, timeout=45)` →
   `chat.completions.create(model, messages=[{role:"user", content:prompt}])`,
   raw text from `choices[0].message.content`. Skipped if no `FEATHERLESS_API_KEY`.
2. **Ollama** (fallback) — local `POST /api/generate`. Runs automatically on
   **any** Featherless failure: connection error, timeout, auth/API error, no
   choices, empty content, or an unparseable (`VERDICT`-less) reply.

| Function / model | Purpose |
|---|---|
| `review_trade(order, snapshot, account, *, featherless_api_key=None, featherless_model=None, featherless_base_url=None, featherless_client=None, host=None, model=None, timeout=None, session=None)` → `OfficerReview` | Builds one prompt, tries Featherless then Ollama, parses whichever answers. **Fail-safe VETO only if both fail** (`approved=False, ok=False, provider="none"`). `featherless_client=` and `session=` are test seams. |
| `build_prompt(order, snapshot, account)` | Pure. Lays out the trade, the volatility backdrop (ATM IV, realized vol, **IV-RV spread**, IV regime + reason), and current exposure (max-loss % of equity, open positions, day P&L, drawdown). Asks for `VERDICT: APPROVE\|VETO` + `THESIS: <2-3 sentences>`. Tolerates missing snapshot keys. **One prompt, both providers.** |
| `parse_review(text, model)` → `OfficerReview` | Pure. Regex `VERDICT\s*[:\-]\s*(APPROVE\|VETO)` (case-insensitive), thesis from a `THESIS:` label or the text after the verdict; whitespace-collapsed, capped at 800 chars. No match → fail-safe VETO. **Shared by both providers.** |
| `OfficerReview` | `approved`, `thesis`, `model`, `ok` (False ⇒ fail-safe), `raw_response`, `error`, `provider` (`"featherless"`/`"ollama"`/`"none"`); `.describe()` shows the provider and marks fail-safe vetoes. |
| `warm_up(*, host=None, model=None, timeout=None, session=None)` → `bool` | Fires a throwaway one-token `/api/generate` (`options={"num_predict": 1}`) to force the **Ollama** model resident before the loop (Featherless is hosted, no cold-load). Never raises — returns `False` if Ollama is down at startup. `main.py` calls it once. |

**Logging (evidence trail):** the prompt + provider at INFO; on success the
verbatim raw response + parsed thesis; `"featherless FAILED … -> falling back to
ollama"` at WARNING; unparseable replies at WARNING with the raw text;
`"NO PROVIDER SUCCEEDED -> VETO"` at ERROR with both providers' failure detail;
warm-up outcome at INFO / WARNING.

Config via env (auto-loaded from `.env`): `FEATHERLESS_API_KEY`,
`FEATHERLESS_MODEL` (`Qwen/Qwen2.5-7B-Instruct` — mid-size, non-gated, 32k ctx,
free tier), `FEATHERLESS_BASE_URL` (`https://api.featherless.ai/v1`),
`FEATHERLESS_TIMEOUT` (`45`); `OLLAMA_HOST` (`http://localhost:11434`),
`OLLAMA_MODEL` (`llama3.2`), `OLLAMA_TIMEOUT` (`120`), `OLLAMA_WARM_UP_TIMEOUT`
(`180`). The Featherless key lives only in the git-ignored `.env`.

**Run:** `python -m trading_agent.risk_officer` → `warm_up()` then a real call.
Verified end to end: real `VERDICT` via `featherless:Qwen/Qwen2.5-7B-Instruct`;
with a bogus key → `featherless FAILED (401) -> falling back to ollama` → real
verdict via `ollama:llama3.2`.

`main.py` calls `warm_up()` at startup and `review_trade(..., timeout=45)` in the
pipeline.

---

### 3.8 `main.py` — the autonomous loop

`run_forever(Config.from_env())` — or the `trading-agent` console script — runs
one `run_cycle()` every `AGENT_LOOP_INTERVAL_SECONDS` (default 900), **only while
`TradingClient.get_clock().is_open`**.

| Function / model | Purpose |
|---|---|
| `startup(config)` | First run: fetch REAL equity from Alpaca, persist as `starting_equity` in `session.json`. Restart: load it, **never re-derive** (would corrupt the 5% floor). Then `risk_officer.warm_up()` + startup log (ts, account id, equities). |
| `run_cycle(conn, session, config)` | One global `AccountState` → manage positions across the basket → sticky-halt latch → **loop `config.tickers`**: `get_market_snapshot(sym)` → `evaluate_cycle_decision()`, rebuilding `account` after each open → one `DecisionSummary` per ticker → fold the cycle's decisions into `session.daily_activity` (the Post-Mortem funnel) → persist `session.json`. Returns `CycleReport(decisions[], closed[], opened[])`. |
| `decide_exit(valuation, *, is_expiring, ...)` | Pure, **structure-aware**. Credit (condor / vertical): **profit-target** (≥ 50% of credit captured) → **stop-loss** (loss ≥ 2× credit). Debit (long strangle): **profit** (≥ +50% of premium) → **stop** (≤ −`AGENT_DEBIT_STOP_FRACTION`, default 50%, of premium). Then **expiry**. First match wins. |
| `value_condor(legs, mid_by_symbol)` | Pure. Net mid $ to close (pay for legs sold, receive for legs bought — sign convention works for all-long debit structures too). `None` if a leg has no quote. |
| `manage_open_positions(...)` | Closes every triggered position via `close_fn`, records a history event with `symbol`/`structure`/realized P&L, drops it. A `close_fn` exception keeps the position. |
| `halt_status(account)` / `update_sticky_halt(session, account)` | Same thresholds as `risk_manager`, vs **persisted** `starting_equity`. The 5% breach latches `session.trading_halted = True` permanently. |
| `evaluate_new_trade(snapshot, account, *, config, plan_fn=, to_order_fn=, check_fn=, review_fn=, submit_fn=, call_log=)` | Runs **strategy (`build_strategy_plan`) → risk_manager → risk_officer (45 s) → executor in that exact order**; a rejection at any stage returns immediately. Forwards the plan's regime into the `risk_officer` prompt. All stages injectable. |
| `evaluate_cycle_decision(...)` | Capacity (< 3, **global**) + halt prechecks, then `evaluate_new_trade`. Always returns a `DecisionSummary` (`Skipped`/`Halted`/`Blocked`/`Vetoed`/`Executed`/`Error`). |
| `daily_summary_text(...)` | Copy-paste-ready recap: equity, day P&L, trades opened/closed today **with ticker + structure + regime**, open book per ticker, hashtags. At/after 16:00 ET, once per ET day. |
| `_maybe_heartbeat / _maybe_morning_brief / _maybe_post_mortem` | Off-hours-intelligence hooks called every loop, each gated internally by a session marker (`last_heartbeat_at` / `last_morning_brief_date` / `last_post_mortem_date`). Cheap no-ops until due; never block a cycle. Pure rendering lives in `offhours.py`. |
| `AlpacaConnection.premarket_gaps(tickers)` | Per-ticker reference price vs prior daily close → `offhours.TickerGap[]` for the Morning Brief. One bad ticker is skipped. |

**Resilience:** every cycle is wrapped in try/except — one bad cycle logs and the
loop continues, never crashes. A `get_clock()` failure still emits an Error
heartbeat before the loop sleeps. Logs to console **and** `logs/agent.log` (both
UTF-8; stdout reconfigured so Windows cp1252 doesn't mangle output); off-hours
events additionally go to `logs/agent_activity.log`.

**Config — env vars, nothing hardcoded:** `AGENT_TICKERS` (`SPY,QQQ,IWM,TLT`),
`AGENT_LOOP_INTERVAL_SECONDS`, `AGENT_LOG_LEVEL`, `AGENT_ENV_FILE` (loaded first,
wins), `AGENT_SESSION_FILE`, `AGENT_LOG_FILE`, `AGENT_REVIEW_TIMEOUT_SECONDS`
(45), `AGENT_PROFIT_TARGET_FRACTION` (0.50), `AGENT_STOP_LOSS_MULTIPLE` (2.0,
credit stop), `AGENT_DEBIT_STOP_FRACTION` (0.50, long-strangle stop),
`AGENT_ACTIVITY_LOG` (`logs/agent_activity.log`), `AGENT_HEARTBEAT_MINUTES` (60),
`AGENT_GAP_ALERT_PCT` (0.5). `session.json` / `logs/` are git-ignored.

**Run:** `python -m trading_agent.main` (or `trading-agent`). Verified live:
`startup()` created `session.json` (starting_equity $100,000, account
`PA3ARUWVYYGH`), `warm_up()` OK, `get_clock()` → `is_open=True`, and one full
`strategy` sweep of the basket detected TLT → Regime B. No live `run_cycle()`
executed (it would place a real paper MLEG order).

---

### 3.9 `offhours.py` — Off-Hours Intelligence (observability, not trading)

Runs *around* the 15-min loop. **No change to `risk_manager.py` or the
`strategy.py` trade logic; no new dependency.** Pure functions here; `main.py`
does the time-gating (session markers, same pattern as the daily summary) and IO.

| Function / model | Purpose |
|---|---|
| `build_heartbeat(now, *, market_open, connectivity_ok, iv_readings)` → `Heartbeat` | `.render()` → `"[YYYY-MM-DD HH:MM] HEARTBEAT: Status: Idle/Active \| Connectivity: OK/Error \| Memory: N IV readings stored."` — the required format verbatim. |
| `count_iv_readings(path)` | Raw data-row count of `iv_history.csv` (missing file → 0). |
| `interval_elapsed(last_iso, now, *, min_gap_seconds=3600)` | Hourly gate for the heartbeat; true on empty / unparseable stamp. |
| `in_morning_brief_window(now_et)` | `09:00 ≤ t < 09:30` ET. |
| `TickerGap(symbol, prev_close, premarket)` | `.gap_pct`, `.is_significant(0.5)`, `.regime_hint()` → TRENDING vs RANGE-BOUND read. |
| `morning_brief_text(gaps, *, et_date, threshold=0.5)` | Lists every ticker's gap; a `> threshold` move emits a **PRE-MARKET ALERT** block; all-flat → "RANGE-BOUND bias intact". |
| `DailyActivity` + `accumulate_activity(activity, decisions)` | Per-ET-day funnel folded from each cycle's `DecisionSummary[]`: `ticker_scans`, `proposed` (reached `risk_manager`+), `approved` (executed), `rm_vetoes`, `ro_vetoes`, `regimes{}`. Cumulative across cycles; tolerates plan-less decisions. Persisted in `session.daily_activity` (last 10 days). |
| `dominant_regime(regimes)` | Most-scanned label → "Overall Range-Bound / Trending / High-Volatility / Neutral". |
| `post_mortem_text(activity, *, et_date, open_positions, unrealized_pnl)` | End-of-day digest: the funnel, open unrealized P&L, dominant regime, regime breakdown, hashtags. |

`main.py` wiring: `setup_logging` adds a third handler so the `agent.offhours`
logger writes to `AGENT_ACTIVITY_LOG` **and** propagates to the main log +
console. `run_forever` restructured to emit an Error heartbeat even when
`get_clock()` throws.

---

### 3.10 `context_gatherer.py` — Contextual Intelligence & Macro-Filter

Reads only — never trades, never loosens a limit. numpy + `alpaca-py` (News API,
VIXY quote); macro dates are a bundled static schedule. Every pull fails safe.

| Function / model | Purpose |
|---|---|
| `wilder_rsi(closes, 14)` / `classify_rsi(r)` | Classic Wilder RSI from a close series (numpy). 100 pure rally / 0 pure selloff / 50 flat / None if < 15 closes. Bands: ≥ 70 overbought, ≤ 30 oversold. |
| `HIGH_IMPACT_CALENDAR` (2026) + `upcoming_high_impact(now, 48h)` / `high_impact_today(now)` | Bundled FOMC decisions + CPI releases + monthly NFP (first-Friday). `calendar=` is injectable for a live feed. |
| `fetch_vix_proxy(creds)` | VIXY latest trade + ~5-session % change → `(level, change%, note)`; `±10%/5d` ⇒ "possibly spiking" / "falling". |
| `fetch_headlines(creds, symbols, per=4)` | Top recent headlines/ticker via the Alpaca News API, de-duped, wire-dumps truncated to 180 chars. |
| `score_headlines(headlines)` | Net keyword sentiment (bullish − bearish); tiebreak only. |
| `MarketContext.synthesis()` | `Macro: … \| VIX: … \| News SYM: … \| RSI SYM: … \| …` on one line. `MarketContext.unavailable()` → `"No Context Available"`, `macro_today_high_impact = False`. |
| `gather_context(creds, symbols, *, now=, calendar=, headlines_fn=, vix_fn=, closes_fn=)` | Orchestrator. Each sub-pull degrades independently; a total wipe-out returns `unavailable`. Never raises. |
| `prioritize(symbols, snapshots, context)` | Eligible tickers ordered by IV-RV spread desc, news score as the tiebreak. |

**risk_manager (surgical):** `AccountState.risk_multiplier: float = 1.0`; gate 1's
effective cap is `MAX_RISK_PER_TRADE_PCT × equity × min(risk_multiplier, 1.0)` — a
multiplier can only tighten, and `MAX_RISK_PER_TRADE_PCT` is byte-unchanged.
`is_macro_safe(macro_high_impact=)` / `macro_risk_multiplier(macro_high_impact=)`
(→ `MACRO_RISK_REDUCTION = 0.5` on a High-Impact day).

**risk_officer:** `build_prompt` gained a `### MACRO CONTEXT` block carrying the
synthesis string (or `No Context Available`) plus the standing instruction —
scrutinise / VETO short-vol trades on a VIX spike or imminent Red-Folder event,
check RSI overbought/oversold before a directional credit spread.

**main.py:** `run_cycle` calls `_gather_market_context` first, logs a
`MARKET CONTEXT` block to `agent_activity.log`, sets `risk_multiplier` for the
whole cycle from the macro flag (`MACRO GUARD ACTIVE` warning when < 1.0),
pre-fetches all snapshots, evaluates in `prioritize()` order, and stamps the
synthesis on every `DecisionSummary` (also passed into the risk_officer's
`review_snapshot`). `evaluate_cycle_decision` / `evaluate_new_trade` /
`reconcile_account_state` gained `market_context` / `risk_multiplier` params.

---

## 4. Consolidation (two files → two clean layers)

`data.py` (pasted in) originally re-implemented chain fetching, client creation,
spot price, and `.env` loading that `alpaca_trader.py` already had. Chosen fix:
**two-layer split** — `alpaca_trader.py` owns the primitives, `data.py` imports
them and keeps only strategy-facing logic.

Removed from `data.py`: `get_clients()`, inline `.env` loading, raw
`alpaca-py` client/request imports.

At the same time, `data.py`'s chain pull was narrowed from the full chain to
±5% strikes / next 1–3 trading days:

| Metric | Before | After |
|---|---|---|
| Contracts fetched | ~13,160 | ~460 |
| Wall time | ~90 s | ~3 s |
| ATM IV reported | 0.084 (stale far-dated contract) | 0.256 (near-dated ATM) |

---

## 5. Environment & tooling

- **Repo**: `C:\alpaca-hackathon\trading-agent` — git, `src/trading_agent/`
  package (editable install), docs + `tests/` at the repo root. Migrated here
  2026-08-31 from `C:\alpaca options ai agent\alpaca-hackathon` (flat, no git).
- **`.venv/`** — Python **3.12** (pinned in `pyproject.toml` as `>=3.12,<3.13`;
  alpaca-py's deps lacked 3.14 wheels). `uv venv --python 3.12 && uv pip install -e ".[dev]"`.
- **Dependencies** (`pyproject.toml`): `alpaca-py` 0.44.0, `numpy`, `openai`
  (3.6.0; Featherless client), `pandas`, `pandas-market-calendars` 5.4.0 (NYSE
  holiday calendar), `python-dotenv`, `requests`, `tzdata` (ET clock on Windows);
  `[dev]` adds `pytest`. `uv.lock` regenerated after each dep change.
- **`risk_officer.py` LLM providers**: **Featherless AI** (primary, hosted,
  `https://api.featherless.ai/v1`, `Qwen/Qwen2.5-7B-Instruct`) and **local
  Ollama** (`http://localhost:11434`, v0.33.2, `llama3.2:latest` 3.2B ~2 GB) as
  the automatic fallback — both verified.
- **Credentials & keys**: repo-root `.env` (git-ignored), auto-discovered (cwd,
  then repo root; or `AGENT_ENV_FILE` for an explicit path) — `ALPACA_*` plus
  `FEATHERLESS_API_KEY` / `FEATHERLESS_MODEL`. No key is hardcoded in source.
  Paper trading.
- **`main.py` runtime state** (git-ignored): `session.json` (persisted
  `starting_equity`, sticky halt, tracked positions incl. `symbol`/`structure`,
  event history) and `logs/agent.log`. Basket + tunables via `AGENT_*` env vars.
- **Data feed**: **indicative** only. The paper account has no signed OPRA
  agreement, so `--feed opra` / the default OPRA feed returns
  `"OPRA agreement is not signed"`. Indicative still returns quotes, Greeks, IV.
- **`C:\alpaca-hackathon\alpaca-mcp-server/`** is a pre-existing, separate
  OpenAPI/`fastmcp` server — not `alpaca-py`, not part of this agent's runtime.

---

## 6. Tests

`pytest tests/` → **306 tests, all passing, fully offline** (no network / no API
keys — the market calendar ships its data; the LLM providers, the news/VIX pulls
and Alpaca are all mocked):

- **`test_alpaca_trader.py` (25)** — `next_friday`; `nth_trading_day` (10 cases
  incl. Labor Day / Thanksgiving / Christmas + a non-positive-`n` guard);
  `parse_occ_symbol`; `_to_contract` spread math (mid / spread / spread_pct,
  zero-mid → NaN, missing quote → `None`, missing Greeks → delta `None`);
  `filter_delta_band`.
- **`test_data.py` (19)** — `calculate_iv_percentile`; `evaluate_iv_regime`
  (Hackathon Mode + percentile mode); `calculate_realized_vol`;
  **`log_iv_reading`** — new `timestamp,symbol,iv,rv,spread` schema, appended on
  every call for every ticker; **`read_iv_history(symbol)`** filters by symbol,
  collapses to the last reading per calendar day, tolerates blank IV.
- **`test_strategy.py` (32)** — leg selection; iron-condor gates; **efficiency
  ratio / `is_range_bound` / `trend_direction`** (straight line → 1.0, chop → 0,
  threshold rule, insufficient data → None); **`select_regime`** A/B/C/none
  (incl. IV-rich-but-not-elevated → none, IV-cheap-but-no-history → none);
  **`plan_long_strangle`** (2 long legs, net debit, sized ≤ 1.5%, blocked when
  debit > cap); **`plan_bull_put` / `plan_bear_call`** (credit spreads, 25% gate
  still applies); **`build_strategy_plan`** dispatch + explicit `REGIME [SYM]` /
  `STRATEGY [SYM]` logging + no-regime → ineligible.
- **`test_risk_manager.py` (43)** — the gates with boundary cases;
  **`is_defined_risk`** table now includes all-long strangle → True, single long
  → True, all-long-with-zero-qty → False, mixed short leg → still matched-rule
  (uncovered → False); **`ProposedOrder.max_loss`** overrides the credit formula
  in `risk_dollars`, and gate 1 both approves a $630 strangle and blocks an
  $1,800 one at the unchanged 1.5% cap. **Macro guard**: `is_macro_safe` /
  `macro_risk_multiplier` (0.5 on a High-Impact day); a halved
  `AccountState.risk_multiplier` blocks a $900 trade the full $1,500 cap passes;
  `MAX_RISK_PER_TRADE_PCT` byte-unchanged; a multiplier > 1.0 is clamped to 1.0.
- **`test_context_gatherer.py` (19)** — Wilder RSI (100 rally / 0 selloff / 50
  flat / None short / worked-series band) + `classify_rsi` bands; `score_headlines`
  ±/0; `upcoming_high_impact` 48h window + `high_impact_today`; `synthesis()` four
  sections on one line; `MarketContext.unavailable()` → "No Context Available" +
  macro flag False; `gather_context` happy path (injected fetchers), news-only
  failure degrades just news, total failure → unavailable, never raises on bad
  creds; `prioritize` orders by IV-RV spread then news score.
- **`test_executor.py` (23)** — blocked / sticky-halt / oversized never reach the
  fake client; approved **4-leg** MLEG (SELL/BUY/SELL/BUY, qty, limit); **2-leg
  strangle** MLEG (BUY/BUY, limit = abs(debit)); **3-leg still rejected**; API
  failure returned not raised; unbuildable not sent; no bypass param;
  `from_plan` is the alias and carries `max_loss`; strangle round-trips; plan → order
  round-trips through `submit`.
- **`test_risk_officer.py` (38)** — mocked `FakeFeatherless` (OpenAI-compatible)
  + mocked Ollama `session`, kept offline by an autouse fixture. `parse_review`
  verdict forms incl. Featherless trailing-space style; **Featherless primary**
  (APPROVE / VETO used, Ollama untouched, client built from key); **Featherless →
  Ollama fallback** on `ConnectionError` / `Timeout` / auth error / malformed /
  unparseable / empty content / no choices; **no key → Ollama is primary**;
  **both providers fail → fail-safe VETO** (`provider="none"`, `error` names
  both, last raw body kept); the prompt is identical across both providers and
  survives missing snapshot keys; prompt / provider / fallback / both-failed all
  logged; `warm_up()` one-token request, `True`/`False` without raising, logs
  the outcome; **`### MACRO CONTEXT`** section carries the supplied synthesis
  string and fails safe to "No Context Available".
- **`test_main.py` (57)** — **position management**: `value_condor`;
  `decide_exit` credit (exactly 50% / exactly 2× credit, ordering, configurable)
  **and debit-aware** (strangle +50% / −50% of premium; expiry still forces a
  close; credit logic proven unchanged); `manage_open_positions` selective close
  + history + `close_fn`-raises keeps the position.
  **Gate sequencing** (spies): full approval → `strategy → to_order →
  risk_manager → risk_officer → executor` in that exact order; rejection at each
  stage stops the rest; executor non-submission → `error`; 45 s timeout;
  halt / capacity prechecks skip the pipeline.
  **Multi-ticker `run_cycle`**: evaluates every basket ticker, the **global
  3-position cap** stops opening at 3 (`SPY,QQQ,IWM` open, `TLT` skipped), one
  bad ticker's snapshot logs an error and the rest continue.
  Plus session persistence, sticky-halt latch, `reconcile_account_state`,
  `TrackedCondor` symbol/structure round-trip, basket-aware `daily_summary_text`,
  `DecisionSummary.render`.
  **Off-hours wiring**: `run_cycle` folds decisions into the persisted
  `daily_activity` funnel; session round-trips the off-hours markers and a legacy
  `session.json` (no off-hours keys) still loads; `_maybe_heartbeat` fires once
  per interval + marks the session + reports the Error path; `_maybe_morning_brief`
  only inside 09:00–09:30 ET and once/day; `_maybe_post_mortem` only at/after
  16:00 ET and once/day.
  **Context wiring**: `run_cycle` gathers the market context first, logs the
  `MARKET CONTEXT` block, stamps the synthesis on every `DecisionSummary`,
  evaluates tickers in `prioritize()` order (QQQ before SPY when told to), and
  threads `AccountState.risk_multiplier = 0.5` + a `MACRO GUARD ACTIVE` log on a
  macro day; a `gather_context` blow-up degrades to "No Context Available" and the
  cycle still runs.
- **`test_offhours.py` (23)** — `count_iv_readings` (rows only, missing → 0);
  **Heartbeat** exact format + Active/Idle + OK/Error; `interval_elapsed` hourly
  gate (no-stamp / unparseable / custom gap); **Morning Brief** window,
  `TickerGap` gap % + significance, `> 0.5%` → PRE-MARKET ALERT + "TRENDING",
  all-flat → "RANGE-BOUND", no-quotes path; **Post-Mortem** `accumulate_activity`
  funnel (cumulative, plan-less tolerant), `dominant_regime` bucketing,
  `post_mortem_text` digest + no-open-positions, `DailyActivity` dict round-trip.

---

## 7. Known limitations / follow-ups

1. **`get_atm_iv()` does not pin an expiry** — it keeps the nearest-strike
   contract across the whole 1–3-day window, so ATM IV varies run-to-run
   (observed ~0.10–0.26). This value now **gates trading** via
   `evaluate_iv_regime()`, so pinning the front expiry is the **top follow-up**.
2. **IV percentile is `None` until ≥ 10 daily rows *per symbol*.** Until then the
   gate runs in Hackathon Mode (static > 15%). `iv_history.csv` is appended every
   cycle; `read_iv_history(symbol)` collapses to one point/day, so the agent has
   to run on ≥ 10 distinct days before percentile mode engages for a ticker.
3. **Regime thresholds are first-pass.** "IV ≪ RV" = `iv_rv_spread ≤ −0.02`
   (mirror of the +0.02 rich side); `RANGE_BOUND_ER = 0.30` is Kaufman's common
   default; trend direction is a first-vs-last-close sign over 10 sessions. In a
   genuinely low-IV tape the **Regime C** credit spread often can't clear the 25%
   credit/width gate → C frequently yields no trade (expected). The **long
   strangle** is a raw 2-leg debit sized only by the 1.5% cap — no delta hedge.
4. **Wings can be uneven** (condor) / **Credit & fills are mid-price estimates**,
   no modelled slippage.
5. **Realized vol is close-to-close over 10 sessions** — overnight gaps included,
   no intraday-range estimator.
6. **`data.py`'s snapshot `chain` is ±5% / near-dated only**; `value_condor`
   pulls the full near-dated expiry per ticker (`strike_window_pct=None`) — only
   when positions are open.
7. **`main.py` has not run a live cycle.** `startup()` + `get_clock()` + a live
   multi-ticker `strategy` sweep are verified; no `run_cycle()` has executed live
   (it would place a real paper MLEG order). Position P&L is a mid-price re-quote
   via `value_condor()`; no use of Alpaca's own `unrealized_pl`. Position↔order
   reconciliation is by our own tracking id — a partial fill or a manual close in
   the Alpaca UI is not reconciled back.
8. **`risk_manager` gate 5 checks quantity match only**, not that strikes
   actually bracket (a "long" far ITM would still pass). Fine for condors built
   by `strategy.py`; tighten if orders can come from elsewhere.
9. **`executor.py` has no order lifecycle.** MLEG `limit_price` is the mid-based
   net credit rounded to $0.01; no fill polling, re-price, working-order, or
   cancel logic if it sits unfilled.
10. **`risk_officer` model output is untrusted free text** — only the
    `VERDICT`/`THESIS` shape is parsed, and a VETO can never be overridden by a
    malformed reply. Featherless (`Qwen/Qwen2.5-7B-Instruct`) is primary, Ollama
    (`llama3.2`) the fallback; both verified live plus the 401→fallback and
    both-down→VETO paths. The Ollama fallback's first call after the model idles
    out cold-loads ~2 GB; `main.py` calls `warm_up()` at startup to absorb that.

---

## 8. Not started

- **First live `run_cycle()`** — `main.py` startup is verified against the paper
  account; running a real cycle would place a real paper MLEG order.
- **Order lifecycle** — fill polling, re-price / working orders, partial-fill and
  manual-close reconciliation back into `session.json`.
- **Scheduler** — `main.py` runs its own `time.sleep` loop; no OS-level
  cron/service wrapper or restart-on-crash supervision.
- **Pin `get_atm_iv()` to the front expiry** (see limitation 1).
- **Rolling** — `main.py` closes on the triggers; it does not roll a tested
  position out to a new expiry.

---

## 9. File inventory

Repo root: `C:\alpaca-hackathon\trading-agent`.

| Path | Role |
|---|---|
| `CLAUDE.md` | System instructions + safety rules |
| `pyproject.toml` | Package + deps (incl. `openai`, `tzdata`; `[dev]` = pytest); `requires-python >=3.12,<3.13` |
| `.env` | git-ignored — `ALPACA_*`, `FEATHERLESS_API_KEY`, `FEATHERLESS_MODEL` |
| `src/trading_agent/alpaca_trader.py` | Low-level Alpaca data layer + delta/spread CLI |
| `src/trading_agent/data.py` | Per-ticker market-data layer + IV-regime gate (`get_market_snapshot(symbol)`) |
| `src/trading_agent/strategy.py` | **Regime switch** — efficiency ratio + `select_regime` → condor / long strangle / vertical (`build_strategy_plan()` → `IronCondorPlan`) |
| `src/trading_agent/risk_manager.py` | Pre-trade gates (`check_order()` → `RiskDecision`) + expiry monitor. Gate 5 = matched OR all-long; `ProposedOrder.max_loss`; `AccountState.risk_multiplier` + `macro_risk_multiplier()` (macro guard halves gate 1, clamped ≤ 1.0) |
| `src/trading_agent/risk_officer.py` | LLM second opinion — Featherless primary + Ollama fallback (`review_trade()` → `OfficerReview`, `warm_up()`); regime + `### MACRO CONTEXT` prompt; fail-safe VETO only if both fail |
| `src/trading_agent/context_gatherer.py` | **Contextual Intelligence & Macro-Filter** — macro-event guard + VIX proxy + Alpaca News + Wilder RSI → `gather_context()` / `MarketContext.synthesis()` / `prioritize()`; fail-safe, reads only |
| `src/trading_agent/executor.py` | Gated 2/4-leg MLEG submission (`submit_iron_condor()`, `from_plan()`) |
| `src/trading_agent/main.py` | **Autonomous loop** — context pull + macro guard, prioritised multi-ticker basket, global 3-cap, regime-aware position mgmt, strict gate sequencing, per-cycle resilience, daily summary, off-hours-intelligence wiring (`run_forever()` / `trading-agent`) |
| `src/trading_agent/offhours.py` | **Off-Hours Intelligence** — Heartbeat / Morning Brief / Nightly Post-Mortem renderers + `DailyActivity` funnel (pure; observability only) |
| `iv_history.csv` | git-tracked — one shared basket IV log (`timestamp,symbol,iv,rv,spread`) |
| `tests/test_alpaca_trader.py` | 25 offline unit tests |
| `tests/test_data.py` | 19 offline tests — IV percentile, regime gate, realized vol, per-ticker IV log |
| `tests/test_strategy.py` | 32 offline tests — leg selection, efficiency ratio, regime switch A/B/C, long strangle + verticals |
| `tests/test_risk_manager.py` | 43 offline tests — the six risk gates + edge cases, all-long defined-risk, `max_loss`, macro guard (halved cap, constant untouched, clamp) |
| `tests/test_context_gatherer.py` | 19 offline tests — Wilder RSI, 48h macro calendar, sentiment score, synthesis format, partial/total failure degradation, `prioritize` order |
| `tests/test_executor.py` | 23 offline tests — gate-before-submit, 2/4-leg MLEG, `from_plan` + `max_loss` |
| `tests/test_risk_officer.py` | 38 offline tests — mocked Featherless + Ollama, both providers, fallback path, both-fail VETO, warm-up, `### MACRO CONTEXT` section |
| `tests/test_main.py` | 57 offline tests — position triggers (credit + debit), gate sequencing, multi-ticker global cap, session persistence, daily summary, off-hours + context wiring |
| `tests/test_offhours.py` | 23 offline tests — heartbeat format, hourly gate, morning-brief window + gap alert, post-mortem funnel + digest |
| `session.json` / `logs/agent.log` / `logs/agent_activity.log` | git-ignored — `main.py` runtime state + logs (main + off-hours audit trail) |
| `iv_history.csv` | Generated — daily ATM IV log |
| `PROJECT_STATE.md` | Current architecture status |
| `DEVLOG.md` | Dated change log |
| `WORK_SUMMARY.md` | This document |
| `.venv/` | Python 3.12 virtual environment (git-ignored) |
