# Work Summary — SPY Options Trading Agent

_Generated: 2026-08-31_

Rolling summary of everything built so far for the Alpaca x LabLab.ai hackathon
agent. Companion to `PROJECT_STATE.md` (current architecture) and `DEVLOG.md`
(dated change entries).

---

## 1. Executive summary

A **market-data layer**, an **iron-condor builder**, a **pre-trade risk
manager**, a **gated order executor**, an **LLM second-opinion gate**, and the
**autonomous loop** that drives them for a SPY adaptive options agent are built
and tested — **175 offline tests** (the data path, the LLM providers, and the
agent startup are also verified live against the Alpaca paper account):

- **`alpaca_trader.py`** — low-level Alpaca access: credentials, spot price,
  option-chain fetch with expiry/strike filters, OCC symbol parsing, a 20–30
  delta scanner with bid/ask spread metrics, and a CLI.
- **`data.py`** — strategy-facing layer: near-the-money chain pull, ATM implied
  volatility, 10-day realized vol + IV−RV spread, a daily IV-history log, an
  IV-percentile calc, the IV-regime gate (`evaluate_iv_regime`, with a Hackathon
  Mode static fallback), and a single `get_market_snapshot()` entry point.
- **`strategy.py`** — proposes a defined-risk **iron condor** from a snapshot:
  IV-regime gate → IV−RV spread gate → nearest 1–3 DTE expiry → short legs ~0.225
  delta → long legs ~0.10 delta (or ~$5 wide) → credit ≥ 25% of width → size
  within the **$1,500** (1.5%) risk cap. Proposes only; does not place orders.
- **`risk_manager.py`** — six hard gates: 1.5%/trade risk cap, 2.5% daily-loss
  halt, 5% total-drawdown floor (+ sticky halt), max 3 concurrent positions,
  defined-risk leg-match invariant, and a ≤1-session-to-expiry force-close flag.
  Pure/deterministic; decides only, never trades.
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
- **`main.py`** — the autonomous loop. Every 15 min in market hours: refresh
  snapshot + `AccountState` (persisted `starting_equity`), **manage open
  positions first** (50% profit target / 2× stop / expiry close), latch the
  sticky halt, then run **strategy → risk_manager → risk_officer → executor in
  that exact order** if there's room and no halt. Logs a `DecisionSummary` every
  cycle; each cycle is isolated by try/except; daily copy-paste recap at 4 pm ET.

Per-trade risk is **1.5% everywhere**: `risk_manager.MAX_RISK_PER_TRADE_PCT` is
the single source of truth, `strategy.py` imports it, and `CLAUDE.md` rule 2 says
1.5% ($1,500). `main.py` drives the full pipeline; `startup()` + `get_clock()`
are verified against the paper account, but no live `run_cycle()` has run yet
(it would place a real paper order).

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
| `log_daily_iv(...)` / `read_iv_history(...)` | Append/read `iv_history.csv` (`timestamp, underlying_price, atm_iv`). |
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

### 3.4 `strategy.py` — iron condor builder

Consumes `get_market_snapshot()`. **Proposes** a single defined-risk iron
condor; places no orders.

| Function | Purpose |
|---|---|
| `pick_expiry(contracts, today=None)` | Earliest listed expiry within `nth_trading_day(1)`…`nth_trading_day(3)`. |
| `select_short_leg(legs)` | Contract nearest **0.225** `|delta|`, preferring the 0.20–0.25 band. |
| `select_long_leg(legs, short_leg, right)` | ~**0.10** `|delta|` (±0.05 tol); else the strike nearest **short ± $5**. Returns `(contract, rule)` — `rule ∈ {delta, otm-offset, none-further-otm}`. |
| `plan_iron_condor(contracts, *, underlying_price, iv_regime, iv_rv_spread=None, today=None)` | Core: IV gate → **IV-RV gate** → expiry → 4 legs → credit/width → risk sizing. Testable without network. |
| `build_iron_condor(snapshot=None, *, today=None)` | Fetches a snapshot if not given, then calls `plan_iron_condor` (passing `snapshot["iv_rv_spread"]`). |
| `IronCondorPlan` (dataclass) | `eligible`, `reason`, `expiry`, `iv_rv_spread`, `legs[4]` (`CondorLeg(action, right, contract)`), `net_credit` (mid estimate), `wing_width` (wider side), `credit_to_width`, `max_loss_per_contract`, `suggested_contracts`. `.describe()` pretty-prints. |

Rules enforced: iron condor only · IV must be ≥ **0.02** annualized vol points
above 10-day realized vol (`MIN_IV_RV_SPREAD`; skipped when `iv_rv_spread` is
`None`) · net credit ≥ **25%** of wing width · max loss per contract × sizing ≤
**$1,500** (1.5% of a nominal $100k) → `suggested_contracts = floor(1500 /
max_loss_per_contract)`.

Tunables: `SHORT_DELTA_TARGET/MIN/MAX = 0.225/0.20/0.25`, `LONG_DELTA_TARGET =
0.10` (`LONG_DELTA_TOLERANCE = 0.05`), `LONG_OTM_OFFSET = 5.0`,
`MIN_CREDIT_TO_WIDTH = 0.25`, `MIN_IV_RV_SPREAD = 0.02`,
`DTE_MIN/MAX_TRADING_DAYS = 1/3`, `MAX_RISK_PER_TRADE = MAX_RISK_PER_TRADE_PCT ×
$100k = $1,500` (fraction imported from `risk_manager`).

**Run:** `python -m trading_agent.strategy` → prints the proposed condor (or the block reason).

Illustrative build (gate forced open, 2026-08-31): realized vol ≈ 0.077, IV−RV
spread ≈ +0.045 (> 0.02 ✓); expiry 2026-09-01, ~0.22Δ shorts / ~0.10Δ longs, a
4-wide wing. Exact strikes, credit, and contract count move with the market each
run; sizing is `floor($1,500 / max-loss-per-contract)`.

### 3.5 `risk_manager.py` — pre-trade gates + expiry monitor

Pure/deterministic (no network, no clock — pass `today`). **Decides only**;
never places or cancels orders.

| Function / model | Purpose |
|---|---|
| `check_order(order, account)` → `RiskDecision` | Runs gates 1–5, collects **every** failure (not short-circuit). `RiskDecision` = `approved`, `blocks[]`, `checks{gate: passed}`, plus computed `order_risk` / `max_risk_allowed` / `daily_loss` / `total_drawdown` / … and `.describe()`. |
| `is_defined_risk(legs)` | Gate 5 — per option right, bought contracts == sold contracts (rejects naked, qty-mismatch, empty, bad-action). |
| `flag_expiring_positions(positions, today=)` → `list[ExpiringPosition]` | Gate 6 — positions ≤ `EXPIRY_CLOSE_TRADING_DAYS` (1) NYSE sessions from expiry. |
| `trading_days_until(target, today=)` | NYSE-session count to a date (0 if on/past); holiday-aware via `trading_sessions`. |
| `OrderLeg`, `ProposedOrder` (`.risk_dollars`), `OpenPosition`, `AccountState` | Inputs. `AccountState` = starting / day-start / current equity, `open_positions`, sticky `trading_halted`. |

**Gates & boundaries**

| # | Gate | Threshold | At exactly the threshold |
|---|---|---|---|
| 1 | Max risk / trade | `(wing − credit) × 100 × qty ≤ 1.5% × current_equity` (`MAX_RISK_PER_TRADE_PCT`, shared with `strategy.py`) | **allowed** (`≤`) |
| 2 | Daily loss halt | `day_start − current ≥ 2.5% × starting_equity` | **halts** (`≥`) |
| 3 | Total drawdown floor | `starting − current ≥ 5% × starting_equity`, or `trading_halted` | **halts** (`≥`) |
| 4 | Max concurrent positions | `len(open_positions) < 3` | 3 open → **blocks** the 4th |
| 5 | Defined-risk invariant | long contracts == short contracts, per right | — |
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
| `from_iron_condor_plan(plan)` → `ProposedOrder` | Maps `strategy.IronCondorPlan` → order with the 4 OCC symbols + `suggested_contracts`. Raises if `not plan.eligible` (strategy rejected it — no override) or the plan has no legs / no sizing. |
| `_build_mleg_request(order)` → `LimitOrderRequest` | `order_class=MLEG`, `qty=N`, `time_in_force=DAY`, `limit_price=round(abs(net_credit), 2)`, `legs=[OptionLegRequest(occ_symbol, side, ratio_qty=1) × 4]`. Rejects non-4-leg, `qty<1`, or a leg with no symbol. |
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
| `run_cycle(conn, session, config)` | Snapshot + `AccountState` → manage positions → sticky-halt latch → `evaluate_cycle_decision()` → log `DecisionSummary` → persist `session.json`. |
| `decide_exit(valuation, *, is_expiring, ...)` | Pure. Close reason in the spec's order: **profit-target** (≥ 50% of entry credit captured) → **stop-loss** (loss ≥ 2× entry credit) → **expiry**. First match wins. Thresholds are config. |
| `value_condor(legs, mid_by_symbol)` | Pure. Net mid $ to buy the structure back (pay for legs sold, receive for legs bought). `None` if a leg has no quote. |
| `manage_open_positions(session, valuations, expiring_ids, *, close_fn, ...)` | Closes every triggered condor via `close_fn`, records a history event with realized P&L, drops it from `session.open_condors`. A `close_fn` exception keeps the position. |
| `halt_status(account)` / `update_sticky_halt(session, account)` | Same thresholds as `risk_manager`, vs **persisted** `starting_equity`. The 5% breach latches `session.trading_halted = True` permanently. |
| `evaluate_new_trade(snapshot, account, *, config, plan_fn=, to_order_fn=, check_fn=, review_fn=, submit_fn=, call_log=)` | Runs **strategy → risk_manager → risk_officer (45 s) → executor in that exact order**; a rejection at any stage returns immediately and later stages never run. `executor.submit_iron_condor()` re-runs `check_order()` internally. All stages injectable for tests. |
| `evaluate_cycle_decision(...)` | Capacity (< 3) + halt prechecks, then `evaluate_new_trade`. Always returns a `DecisionSummary` (`Skipped`/`Halted`/`Blocked`/`Vetoed`/`Executed`/`Error`). |
| `daily_summary_text(session, *, current_equity, day_start_equity, et_date)` | Copy-paste-ready recap: equity, day P&L, trades opened/closed today, open book, hashtags. Emitted at/after 16:00 ET, once per ET day. |

**Resilience:** every cycle is wrapped in try/except — one bad cycle logs and the
loop continues, never crashes. Logs to console **and** `logs/agent.log` (both
UTF-8; stdout reconfigured so Windows cp1252 doesn't mangle output).

**Config — env vars, nothing hardcoded:** `AGENT_LOOP_INTERVAL_SECONDS`,
`AGENT_LOG_LEVEL`, `AGENT_ENV_FILE` (loaded first, wins), `AGENT_SESSION_FILE`,
`AGENT_LOG_FILE`, `AGENT_REVIEW_TIMEOUT_SECONDS` (45), `AGENT_PROFIT_TARGET_FRACTION`
(0.50), `AGENT_STOP_LOSS_MULTIPLE` (2.0). `session.json` / `logs/` are git-ignored.

**Run:** `python -m trading_agent.main` (or `trading-agent`). Verified live:
`startup()` created `session.json` (starting_equity $100,000, account
`PA3ARUWVYYGH`), `warm_up()` OK, `get_clock()` → `is_open=True`. No live
`run_cycle()` executed (it would place a real paper MLEG order).

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
  `starting_equity`, sticky halt, tracked condors, event history) and
  `logs/agent.log`. Tunables via `AGENT_*` env vars (loop interval, log level,
  env file, session/log paths, review timeout, profit-target / stop-loss).
- **Data feed**: **indicative** only. The paper account has no signed OPRA
  agreement, so `--feed opra` / the default OPRA feed returns
  `"OPRA agreement is not signed"`. Indicative still returns quotes, Greeks, IV.
- **`C:\alpaca-hackathon\alpaca-mcp-server/`** is a pre-existing, separate
  OpenAPI/`fastmcp` server — not `alpaca-py`, not part of this agent's runtime.

---

## 6. Tests

`pytest tests/` → **175 tests, all passing, fully offline** (no network / no API
keys — the market calendar ships its data; both LLM providers and Alpaca are mocked):

- **`test_alpaca_trader.py` (25)** — `next_friday`; `nth_trading_day` (10 cases
  incl. Labor Day / Thanksgiving / Christmas + a non-positive-`n` guard);
  `parse_occ_symbol`; `_to_contract` spread math (mid / spread / spread_pct,
  zero-mid → NaN, missing quote → `None`, missing Greeks → delta `None`);
  `filter_delta_band`.
- **`test_data.py` (15)** — `calculate_iv_percentile` (thresholds, missing IV);
  `evaluate_iv_regime` Hackathon Mode (strict > 15%, IV unavailable) and
  percentile mode (elevated / below-median / inclusive 50);
  `calculate_realized_vol` (insufficient/non-positive guards, flat → 0,
  independent recompute + √252, last-window-only, bigger-swings-higher).
- **`test_strategy.py` (14)** — short/long leg selection (delta rule, $5 offset
  fallback, none-further-OTM); full condor build; IV-gate block; **IV−RV gate**
  (blocks thin spread, passes healthy spread, skips when `None`); thin-credit
  reject; no-expiry-in-window; position sizing vs the risk cap.
- **`test_risk_manager.py` (31)** — one block per gate with boundary cases:
  risk cap exactly met vs one contract over, daily loss / drawdown exactly at
  threshold, sticky halt, 3rd position OK vs 4th blocked, `is_defined_risk`
  table (condor / vertical / naked / qty-mismatch / empty / bad-action),
  `trading_days_until` 0/1/2 DTE with the Labor Day skip, all six gates failing
  at once.
- **`test_executor.py` (18)** — blocked / sticky-halt / oversized orders never
  reach the fake client; approved order builds the right MLEG (4 OCC symbols,
  SELL/BUY/SELL/BUY, `qty`, `limit_price`); API failure returned in `error` not
  raised; approved-but-unbuildable (missing symbol) not sent; signature has no
  bypass param and `check_order` is always called; `IronCondorPlan` → order
  round-trips through `submit`.
- **`test_risk_officer.py` (36)** — mocked `FakeFeatherless` (OpenAI-compatible)
  + mocked Ollama `session`, kept offline by an autouse fixture. `parse_review`
  verdict forms incl. Featherless trailing-space style; **Featherless primary**
  (APPROVE / VETO used, Ollama untouched, client built from key); **Featherless →
  Ollama fallback** on `ConnectionError` / `Timeout` / auth error / malformed /
  unparseable / empty content / no choices; **no key → Ollama is primary**;
  **both providers fail → fail-safe VETO** (`provider="none"`, `error` names
  both, last raw body kept); the prompt is identical across both providers and
  survives missing snapshot keys; prompt / provider / fallback / both-failed all
  logged; `warm_up()` one-token request, `True`/`False` without raising, logs
  the outcome.
- **`test_main.py` (36)** — **position management**: `value_condor` net-mid /
  missing-leg → `None`; `decide_exit` profit-target (fires at *exactly* 50%),
  stop-loss (fires at *exactly* 2× credit lost), expiry-when-flagged,
  none-when-healthy, profit & stop each beat expiry when both true, configurable
  thresholds; `manage_open_positions` closes only triggered condors, records
  history + realized P&L, and **keeps the position if `close_fn` raises**.
  **Gate sequencing** (spies record every call): full approval →
  `strategy → to_order → risk_manager → risk_officer → executor` in that exact
  order; rejection at strategy / risk_manager / risk_officer each stops before
  every later stage; executor non-submission → `error`; `review_fn` receives the
  45 s timeout; halt / capacity prechecks skip the pipeline entirely (no spy
  calls). Plus session persistence (`starting_equity` seeded from live equity
  once, kept verbatim on restart even after a drawdown), sticky-halt latch,
  `reconcile_account_state`, `daily_summary_text`, `DecisionSummary.render`.

---

## 7. Known limitations / follow-ups

1. **`get_atm_iv()` does not pin an expiry** — it keeps the nearest-strike
   contract across the whole 1–3-day window, so ATM IV varies run-to-run
   (observed ~0.10–0.26). This value now **gates trading** via
   `evaluate_iv_regime()`, so pinning the front expiry is the **top follow-up**.
2. **IV percentile is `None` until ≥ 10 daily rows.** Until then the gate runs in
   Hackathon Mode (static > 15%). A scheduled daily `log_daily_iv()` is needed
   for the history to accumulate.
3. **`strategy.py` wings can be uneven.** Each long leg is chosen independently
   (0.10 delta or $5), so the put and call wings may differ; `plan_iron_condor`
   uses the wider wing for width and max-loss. Add an equal-wing constraint if
   wanted.
4. **Credit / fills are mid-price estimates**, no modelled slippage.
5. **Realized vol is close-to-close over just 10 sessions** — overnight gaps
   included, no intraday-range (Parkinson/Garman-Klass) estimator, and a short,
   noisy window. `MIN_IV_RV_SPREAD = 0.02` is a starting assumption to tune.
6. **`data.py`'s returned `chain` is ±5% / near-dated only.** If `strategy.py`
   needs wider strikes or further-dated expiries, widen the constants.
7. **`main.py` has not run a live cycle.** `startup()` + `get_clock()` are
   verified against the paper account; no `run_cycle()` has executed live (it
   would place a real paper MLEG order). Position P&L is a mid-price re-quote via
   `value_condor()` (a leg with no quote → that condor skipped for the cycle,
   logged); no use of Alpaca's own `unrealized_pl`, no slippage model on close.
   Condor↔order reconciliation is by our own tracking id in `session.json` — a
   partial fill or a manual close in the Alpaca UI is not reconciled back.
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
- **Scheduled daily `log_daily_iv()`** so IV history accumulates and the gate can
  graduate from Hackathon Mode to percentile mode.
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
| `src/trading_agent/data.py` | Strategy-facing market-data layer + IV-regime gate (`get_market_snapshot()`) |
| `src/trading_agent/strategy.py` | Iron condor builder (`build_iron_condor()` → `IronCondorPlan`) |
| `src/trading_agent/risk_manager.py` | Pre-trade gates (`check_order()` → `RiskDecision`) + expiry monitor |
| `src/trading_agent/risk_officer.py` | LLM second opinion — Featherless primary + Ollama fallback (`review_trade()` → `OfficerReview`, `warm_up()`); fail-safe VETO only if both fail |
| `src/trading_agent/executor.py` | Gated MLEG submission (`submit_iron_condor()` → `ExecutionResult`) |
| `src/trading_agent/main.py` | **Autonomous loop** — startup/session, position management, strict gate sequencing, per-cycle resilience, daily summary (`run_forever()` / `trading-agent`) |
| `tests/test_alpaca_trader.py` | 25 offline unit tests |
| `tests/test_data.py` | 15 offline tests — IV percentile, regime gate, realized vol |
| `tests/test_strategy.py` | 14 offline tests — condor construction + IV−RV gate |
| `tests/test_risk_manager.py` | 31 offline tests — the six risk gates + edge cases |
| `tests/test_executor.py` | 18 offline tests — gate-before-submit, MLEG build, logging |
| `tests/test_risk_officer.py` | 36 offline tests — mocked Featherless + Ollama, both providers, fallback path, both-fail VETO, warm-up |
| `tests/test_main.py` | 36 offline tests — position-management triggers + strict gate sequencing + session persistence + daily summary |
| `session.json` / `logs/agent.log` | git-ignored — `main.py` runtime state + log |
| `iv_history.csv` | Generated — daily ATM IV log |
| `PROJECT_STATE.md` | Current architecture status |
| `DEVLOG.md` | Dated change log |
| `WORK_SUMMARY.md` | This document |
| `.venv/` | Python 3.12 virtual environment (git-ignored) |
