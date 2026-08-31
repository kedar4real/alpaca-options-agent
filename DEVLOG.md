# Dev Log

## 2026-08-31 — `main.py`: the autonomous trading loop

`run_forever(Config.from_env())` (also the `trading-agent` console script) runs
one `run_cycle()` every `AGENT_LOOP_INTERVAL_SECONDS` (default 900), only while
`TradingClient.get_clock().is_open`.

- **Startup**: `session.json` — first run persists the REAL Alpaca equity as
  `starting_equity`; every later run loads it and **never re-derives** it (a
  re-derive would silently move the 5% drawdown floor on restart). Then
  `risk_officer.warm_up()` and a startup log line (ts, account id, equities).
- **Manage positions first**: `decide_exit()` (pure) per tracked condor, in the
  spec's order — profit-target (≥ 50% of entry credit captured) → stop-loss
  (loss ≥ 2× entry credit) → expiry (`risk_manager.flag_expiring_positions`,
  ≤ 1 trading day). Closes via `TradingClient.close_position` per leg.
- **Halts**: `update_sticky_halt()` latches the comp-level halt into
  `session.json` once the 5% floor breaks; `halt_status()` (same thresholds as
  `risk_manager`, vs persisted `starting_equity`) skips new-trade evaluation.
- **Pipeline**: `evaluate_cycle_decision()` → capacity + halt prechecks →
  `evaluate_new_trade()` runs **strategy → risk_manager → risk_officer (45s) →
  executor in that exact order**; any rejection returns immediately.
- **Every cycle** logs a `DecisionSummary` (Skipped / Halted / Blocked / Vetoed
  / Executed / Error). Each cycle is wrapped in try/except — one bad cycle logs
  and the loop continues, never crashes.
- **Daily summary** at/after 16:00 ET (once per ET day): `daily_summary_text()`
  — copy-paste-ready recap (equity, day P&L, trades opened/closed, open book).
- **Config = env vars only**: `AGENT_LOOP_INTERVAL_SECONDS`, `AGENT_LOG_LEVEL`,
  `AGENT_ENV_FILE` (loaded first, wins), `AGENT_SESSION_FILE`, `AGENT_LOG_FILE`,
  `AGENT_REVIEW_TIMEOUT_SECONDS`, `AGENT_PROFIT_TARGET_FRACTION`,
  `AGENT_STOP_LOSS_MULTIPLE`. Logs to console + `logs/agent.log` (both UTF-8;
  stdout reconfigured so Windows cp1252 doesn't mangle em dashes).
- `__init__.py` `main()` now delegates to `main.run_forever` (was the uv
  placeholder). Added `tzdata` dep (ET clock on Windows). `session.json`,
  `session.tmp`, `logs/` added to `.gitignore`.

**Tests** — `tests/test_main.py` (36), fully offline: `value_condor`,
`decide_exit` (all triggers + exact boundaries + ordering + configurable
thresholds), `manage_open_positions` (selective close, history/P&L, close_fn
failure keeps the position), gate sequencing with spies (exact order + short-
circuit at every stage + 45s timeout passthrough), halt/capacity prechecks,
session persistence (starting_equity kept verbatim on restart after a drawdown),
sticky-halt latch, `reconcile_account_state`, `daily_summary_text`. Suite
**139 → 175**.

Live check: `startup()` against the paper account created `session.json`
(starting_equity $100,000, account `PA3ARUWVYYGH`), `warm_up()` OK, `get_clock()`
returned `is_open=True`. No live `run_cycle()` run — it would place a real paper
MLEG order.

## 2026-08-31 — `risk_officer`: Featherless AI primary + Ollama fallback

`review_trade()` now tries two providers instead of one:

1. **Featherless AI** (primary) — hosted, OpenAI-compatible, via the new
   `openai` dependency. `OpenAI(base_url=FEATHERLESS_BASE_URL,
   api_key=FEATHERLESS_API_KEY, max_retries=0, timeout=45)` then
   `chat.completions.create(model=FEATHERLESS_MODEL, messages=[{role:"user",
   content:prompt}])`, reading `choices[0].message.content`.
2. **Ollama** (fallback) — the existing local `/api/generate` path, run
   automatically on **any** Featherless failure (connection, timeout, auth/API
   error, no choices, empty content, or an unparseable `VERDICT`-less reply), or
   when no `FEATHERLESS_API_KEY` is set.

Same `build_prompt()` and `parse_review()` for both — one `OfficerReview`
shape, now with a `provider` field (`"featherless"` / `"ollama"` / `"none"`).
**Fail-safe VETO only if BOTH providers fail** (`ok=False, provider="none"`,
`error` names both failures, `raw_response` keeps the last body).

- New env (auto-loaded from `.env` via `_ensure_env_loaded()`, mirroring
  `alpaca_trader`): `FEATHERLESS_API_KEY`, `FEATHERLESS_MODEL`
  (default `Qwen/Qwen2.5-7B-Instruct` — mid-size, non-gated, 32k ctx, free
  tier), `FEATHERLESS_BASE_URL`, `FEATHERLESS_TIMEOUT`.
- `FEATHERLESS_API_KEY` + `FEATHERLESS_MODEL` added to the git-ignored `.env`
  (not source — CLAUDE.md rule 1).
- `openai>=1.40` added to `pyproject.toml` deps (installed 3.6.0); `uv lock`
  regenerated.
- `warm_up()` unchanged — still Ollama-only (Featherless is hosted, no
  cold-load); docstring/log now say so.

**Tests** — `tests/test_risk_officer.py` 23 → **36**: autouse fixture keeps them
fully offline (no real `.env`, no real key); `FakeFeatherless` exposes
`.chat.completions.create`. New coverage: Featherless primary (used; Ollama
untouched; client built from key), Featherless→Ollama fallback (connection /
timeout / auth / malformed / unparseable / empty / no-choices), no-key →
Ollama primary, **both fail → VETO**, one identical prompt across providers,
fallback + both-failed logging. Suite **139**.

Live check: `-m trading_agent.risk_officer` → real `VERDICT` via
`featherless:Qwen/Qwen2.5-7B-Instruct`; with a bogus key → `featherless FAILED
(AuthenticationError 401) -> falling back to ollama` → real verdict via
`ollama:llama3.2`.

## 2026-08-31 — `risk_officer` timeout bump + `warm_up()`

`llama3.2` (3.2B, ~2 GB) pulled locally. First live `review_trade()` after the
model idled out timed out at 60 s (cold disk load) -> fail-safe VETO. Two fixes:

- `DEFAULT_TIMEOUT` 60 -> **120 s** (`OLLAMA_TIMEOUT`); new `WARM_UP_TIMEOUT`
  180 s (`OLLAMA_WARM_UP_TIMEOUT`).
- New `warm_up(*, host=, model=, timeout=, session=) -> bool` — fires a throwaway
  one-token `/api/generate` (`options={"num_predict": 1}`) to force the model
  resident. Never raises; returns `False` if Ollama is down at startup (the
  in-loop review still fails safe). `main.py` will call it once before the loop.
  The `__main__` demo now calls it first.

**Tests** — `tests/test_risk_officer.py` +4 (23 total): one-token request shape,
False-without-raising on connection error / HTTP 404, outcome logging. Suite
**126**.

Live check: model unloaded (`keep_alive:0`), then `-m trading_agent.risk_officer`
-> `warm-up OK: model resident` -> real parsed `VERDICT: APPROVE` / `VETO` (no
fail-safe).

## 2026-08-31 — `risk_officer.py` (LLM second-opinion gate)

New module. Runs **after** `risk_manager.check_order()` approves — an extra
judgment layer, never a replacement.

- `review_trade(order, snapshot, account, *, host=, model=, timeout=, session=)`
  -> `OfficerReview(approved, thesis, model, ok, raw_response, error)`.
- Builds a prompt from the IV regime, ATM IV, realized vol, IV-RV spread, and
  current exposure (order max-loss %, open positions, day P&L, drawdown), POSTs
  to local Ollama `/api/generate` (`stream: false`), parses
  `VERDICT: APPROVE|VETO` + `THESIS:` via `parse_review()`.
- **Fail-safe**: transport error / HTTP error / bad JSON / missing VERDICT ->
  `approved=False, ok=False`. A broken reasoning step never green-lights a trade.
- Logs the prompt, the raw response, and every failure (evidence trail for the
  write-up). `OfficerReview.describe()` marks fail-safe vetoes.
- Config via env: `OLLAMA_HOST` / `OLLAMA_MODEL` (`llama3.2`) / `OLLAMA_TIMEOUT`.
- Added `requests>=2.31` to `pyproject.toml` deps.
- Not wired into `executor.py` yet.

**Tests** — `tests/test_risk_officer.py` (19), fully mocked Ollama: verdict
parsing (several forms + thesis fallback), fail-safe on unparseable / empty /
connection-refused / timeout / HTTP 500 / bad JSON, prompt content + missing
fields, logging on success/failure/unparseable. Suite **122**.

Live check: Ollama is up (v0.33.2) but `/api/tags` -> `{"models":[]}`, so
`-m trading_agent.risk_officer` correctly fail-safe VETOs (404 from
`/api/generate` for an unpulled model). `ollama pull llama3.2` for real verdicts.

## 2026-08-31 — Migrated into `C:\alpaca-hackathon\trading-agent` (git, src layout)

Consolidated the project out of the spaced path
`C:\alpaca options ai agent\alpaca-hackathon` (flat modules, no git) into the
pre-existing empty `trading-agent` scaffold.

- Modules → `src/trading_agent/`: `alpaca_trader`, `data`, `strategy`,
  `risk_manager`, `executor`. Intra-package imports rewritten to relative
  (`from .risk_manager import …`); tests import `from trading_agent.… import …`.
- `alpaca_trader._ENV_CANDIDATES` now checks cwd then the repo root
  (`parents[2]`) instead of the module dir — `.env` lives at the repo root.
- `pyproject.toml`: `requires-python` `>=3.14` → `>=3.12,<3.13`; added
  `pandas-market-calendars>=5.0` and a `[dev]` extra with `pytest`. Dropped the
  standalone `requirements.txt`.
- `.python-version` `3.14` → `3.12`; `.venv` recreated at 3.12; package installed
  editable (`uv pip install -e ".[dev]"`).
- Docs (`CLAUDE.md`, `PROJECT_STATE.md`, `WORK_SUMMARY.md`) moved to the repo
  root; run commands are now `python -m trading_agent.<name>`.
- `pytest tests/` from the new root: **103 passed**; `-m trading_agent.risk_manager`
  / `.executor` demos and `load_credentials()` verified.
- Initial git commit made (repo previously had zero commits).

The old spaced path is left in place (still holds `venv/`, `requirements.txt`,
`alpaca-mcp-server/`, `.pytest_cache/`) pending a separate cleanup decision.

## 2026-08-31 — `from_iron_condor_plan()` rejects ineligible plans

Closed the gap found in the live dry run: a strategy-rejected `IronCondorPlan`
(e.g. blocked on credit/width) still carries `legs` + `suggested_contracts`, so
the old check let it through.

- `executor.from_iron_condor_plan()` now raises `ValueError` first thing if
  `not plan.eligible` — "strategy did not approve this plan; it cannot become an
  order: <reason>". No override.
- Test `test_from_iron_condor_plan_rejects_ineligible_plan_even_with_legs_and_size`
  asserts the plan would have passed the old legs/size check but is now refused.
  Suite **103**.

## 2026-08-31 — `executor.py` (risk-gated MLEG submission)

- `submit_iron_condor(order, account, *, client=None, creds=None) -> ExecutionResult`
  runs `risk_manager.check_order()` **first**; if not approved, returns
  `submitted=False` and sends nothing. No `force` / `skip_checks` / `bypass`
  parameter — a test asserts the signature is exactly `{order, account, client,
  creds}` and that `check_order` is always invoked.
- Every attempt logs the full `RiskDecision.describe()`: `WARNING "ORDER
  BLOCKED"` or `INFO "ORDER APPROVED"` + `"ORDER SUBMITTED id=…"`. API failures
  are caught and returned as `error=`, not raised.
- Approved orders become one `LimitOrderRequest(order_class=OrderClass.MLEG,
  qty=N, time_in_force=DAY, limit_price=round(abs(net_credit), 2),
  legs=[OptionLegRequest(symbol, side, ratio_qty=1) × 4])` submitted via
  `TradingClient.submit_order` (paper per `creds.paper`).
- `from_iron_condor_plan(plan)` maps `strategy.IronCondorPlan` ->
  `risk_manager.ProposedOrder`, carrying the 4 OCC symbols and
  `suggested_contracts`; raises if the plan has no legs / no sizing.
- `risk_manager.OrderLeg` gained an optional `symbol: str | None = None`
  (back-compatible — positional 3-arg construction unchanged).
- `ExecutionResult(submitted, decision, order, submitted_request, error,
  .order_id)`. `__main__` is a no-network demo that never submits.

**Tests** — `tests/test_executor.py` (17): blocked/sticky-halt/oversized never
reach the fake client; approved builds the right MLEG (symbols, SELL/BUY/SELL/BUY,
qty, limit); API error surfaced not raised; approved-but-unbuildable (missing
symbol) not sent; no-bypass signature; plan→order round trip. Suite now **102**.

## 2026-08-31 — Standardize per-trade risk at 1.5%

Removed the 2% / 1.5% drift between `strategy.py`, `risk_manager.py`, and
`CLAUDE.md`.

- `risk_manager.MAX_RISK_PER_TRADE_PCT = 0.015` is now the **single source of
  truth**. `strategy.py` imports it: `MAX_RISK_PER_TRADE = MAX_RISK_PER_TRADE_PCT
  * NOMINAL_EQUITY ($100k) = $1,500` (was a hardcoded `2_000.0`).
- `CLAUDE.md` rule 2: "must never exceed 2% ($2,000)" -> "1.5% ($1,500)".
- `test_plan_position_sizing_respects_risk_cap` now also asserts the chosen
  `suggested_contracts` fits under `MAX_RISK_PER_TRADE` and one more would not,
  and that the constant is `1_500.0`. Suite still 85, all passing.
- Sizing example: max loss $298/contract -> `floor(1500/298)` = 5 (was 6 at 2%).

## 2026-08-31 — `risk_manager.py` (pre-trade gates + expiry monitor)

New module, pure/deterministic (no network, no clock; pass `today`). Decides
only — never places or cancels orders.

- `check_order(order, account) -> RiskDecision` runs gates 1-5 and collects
  **every** failure (not short-circuit):
  1. max risk/trade `<= 1.5% * current_equity`, risk `= (wing_width - net_credit)
     * 100 * quantity`
  2. daily loss `>= 2.5% * starting_equity` (measured `day_start_equity -
     current_equity`) -> no new trades today
  3. total drawdown `>= 5% * starting_equity`, plus a sticky `trading_halted`
     flag -> comp-level halt
  4. `len(open_positions) >= 3` -> blocked
  5. `is_defined_risk(legs)` — per option right, bought contracts == sold
     contracts (rejects naked / mismatched-qty / empty / bad-action)
- `flag_expiring_positions(positions, today=)` / `trading_days_until(target,
  today=)` — gate 6: flags positions `<= 1` NYSE session from expiry
  (holiday-aware via `trading_sessions`; e.g. Fri -> Tue over Labor Day is 1 day).
- Models: `OrderLeg`, `ProposedOrder` (`.risk_dollars`), `OpenPosition`,
  `AccountState`, `RiskDecision` (`.describe()`), `ExpiringPosition`.
- Limits as module constants: `MAX_RISK_PER_TRADE_PCT=0.015`,
  `DAILY_LOSS_HALT_PCT=0.025`, `TOTAL_DRAWDOWN_FLOOR_PCT=0.05`,
  `MAX_CONCURRENT_POSITIONS=3`, `EXPIRY_CLOSE_TRADING_DAYS=1`.

**Tests** — `tests/test_risk_manager.py` (31): one block per gate with the
boundary cases (equity exactly at each threshold, one contract over the risk
cap, 3rd vs 4th position, 0/1/2 DTE, Labor Day holiday skip, all gates failing
at once). Suite now **85**, still offline.

## 2026-08-31 — Realized vol + IV-RV spread

**`alpaca_trader.py`**
- `get_daily_closes(creds, symbol, *, sessions=11, calendar_lookback_days=None)` —
  last N daily closes (oldest first) via `StockBarsRequest` / `TimeFrame.Day`.
  Best-effort: returns `[]` on failure.

**`data.py`**
- `calculate_realized_vol(closes, *, window=10, annualization=252)` — sample
  stdev (ddof=1) of daily log returns over the last `window` sessions, x sqrt(252).
  `None` if < `window + 1` positive closes.
- `get_market_snapshot()` gains `realized_vol` and `iv_rv_spread`
  (`atm_iv - realized_vol`, `None` if either side is missing).

**`strategy.py`**
- New `MIN_IV_RV_SPREAD = 0.02`. `plan_iron_condor(..., iv_rv_spread=None)`:
  after the IV-regime gate, if `iv_rv_spread` is not `None` and below the
  threshold -> ineligible ("IV not richer than recent realized movement").
  `None` skips the check. `IronCondorPlan.iv_rv_spread` records the value.
- Refactored the many `IronCondorPlan(...)` early returns through a local
  `result()` helper (injects `underlying_price` / `iv_regime_mode` /
  `iv_rv_spread`).

**Tests** — +6 (`calculate_realized_vol`) +3 (`iv_rv_spread` gate) -> 54 total,
still offline. Live: RV 0.077, IV-RV spread ~+0.045; forced-open condor builds.

## 2026-08-31 — IV-regime gate + `strategy.py` (iron condor builder)

**`data.py`**
- `evaluate_iv_regime(current_iv, history)` -> `IVRegime(atm_iv, iv_percentile,
  mode, trade_eligible, reason)`. Percentile mode once >= 10 logged IV days
  (eligible at `IV_PERCENTILE_MIN = 50`); otherwise **Hackathon Mode**
  (`mode="hackathon_static"`, eligible when ATM IV > `STATIC_IV_THRESHOLD = 0.15`).
- `calculate_iv_percentile()` now uses `IV_HISTORY_MIN_DAYS` and guards `None` IV.
- `get_market_snapshot()` carries `iv_regime` (the dataclass) alongside the
  existing `iv_percentile` key.

**`strategy.py`** (new) — proposes a defined-risk iron condor, no order placement:
- `build_iron_condor(snapshot=None)` -> `plan_iron_condor(contracts, ...)` ->
  `IronCondorPlan`.
- IV-regime gate first; then `pick_expiry()` = earliest listed expiry in the
  `nth_trading_day(1)`..`nth_trading_day(3)` window.
- `select_short_leg()` ~0.225 delta (prefers the 0.20-0.25 band);
  `select_long_leg()` ~0.10 delta, else the strike ~$5 further OTM
  (rule tag: `delta` / `otm-offset` / `none-further-otm`).
- Net credit at mid; wing width = wider side; requires
  `credit / width >= MIN_CREDIT_TO_WIDTH (0.25)`.
- Position sizing: `floor($2,000 / max-loss-per-contract)` per CLAUDE.md.
- Smoke-tested on the live chain (gate forced open): 763/759 put + 773/776 call,
  credit 1.16 / 4.00 wing = 28.9%, $284 max loss -> 7 contracts.

**Tests** — new `tests/test_data.py` (9) + `tests/test_strategy.py` (11);
suite now 45, still fully offline.

## 2026-08-31 — Real NYSE holiday calendar for `nth_trading_day()`

- Added `pandas-market-calendars` (`>=5.0`) to `requirements.txt`; pulls
  `exchange-calendars` + friends.
- `alpaca_trader.nth_trading_day()` now walks the **NYSE (XNYS)** session list
  instead of skipping weekends only. New helpers: `_market_calendar()`
  (`lru_cache`d, lazy import) and `trading_sessions(start, end)`. Added
  `MARKET_CALENDAR = "XNYS"` constant. `n < 1` now raises `ValueError`.
- Effect: the session after Fri 2026-09-04 is now Tue 2026-09-08 (Mon 09-07 =
  Labor Day), not Mon. Thanksgiving / Christmas windows verified too.
- Tests: +6 (`nth_trading_day` holiday cases + non-positive guard) -> 25 total,
  still fully offline (the calendar ships its own holiday data).

## 2026-08-31 — Consolidate data layer into two tiers + narrow the chain pull

Resolved the overlap between `alpaca_trader.py` and the pasted `data.py`
(chosen approach: two-layer split).

- `data.py` now imports `load_credentials`, `get_spot_price`, `fetch_option_chain`,
  `nth_trading_day`, `parse_occ_symbol` from `alpaca_trader.py`. Deleted its
  duplicated `get_clients()`, inline `.env` loading, and raw SDK client wiring.
- `alpaca_trader.get_spot_price()` gained `method=` ("trade" | "quote_mid");
  `data.py` uses `quote_mid` (bid/ask midpoint, falls back to last trade).
- `alpaca_trader.fetch_option_chain()` generalised: `expiry` optional, added
  `expiry_gte` / `expiry_lte`, `strike_window_pct` now nullable.
- New `alpaca_trader.nth_trading_day(n)` — weekend-aware (no holiday calendar).
- `data.get_current_option_chain()` now filters to contracts expiring in the next
  1-3 trading days and strikes within +/-5% of spot: **~13,160 contracts / ~90s
  -> ~460 / ~3s**. ATM IV also became more sensible (0.256 vs a stale 0.084 that
  the unfiltered scan had latched onto).
- Tests: +5 for `nth_trading_day` (now 19, all offline, passing).


## 2026-08-30 — SPY options chain module (`alpaca_trader.py`)

Built the first data-layer module for the agent.

- `next_friday()` resolves the next upcoming Friday expiry (with `weeks_ahead` to roll forward).
- `parse_occ_symbol()` decodes OCC symbols (e.g. `SPY260904P00763000` → put, 763.0, 2026-09-04).
- `fetch_option_chain()` pulls the SPY chain via `alpaca-py` `OptionHistoricalDataClient`, strike-windowed
  to spot ±15%, using the **indicative** feed (paper account has no OPRA agreement).
- `OptionContract` computes `mid`, `spread` (ask − bid) and `spread_pct` (spread / mid × 100), and carries
  greeks `delta` / `abs_delta` plus `implied_volatility` from the snapshot.
- `filter_delta_band()` selects 0.20–0.30 |delta| puts and calls separately, sorted by |delta|.
- `scan_spy_chain()` ties it together; CLI prints a table or `--json`.
- Added `tests/test_alpaca_trader.py` (14 offline tests) and root `requirements.txt`.
- Set up `venv/` (Python 3.12) with `alpaca-py`, `python-dotenv`, `pytest`.

Verified live against the paper account: expiry 2026-09-04, spot ~769.28, 380 quotable contracts,
4 puts and 3 calls in the 20–30 delta band.
