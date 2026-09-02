# Dev Log

## 2026-09-03 — trailing take-profit + a 3rd long-vol slot

- `MAX_LONG_VOL_POSITIONS` 2 -> 3 (operator call, last session before NFP). The
  4% total-debit cap and the correlation guard are unchanged, so the extra slot
  only fills if an uncorrelated name shows an edge while the book is still under
  4% of equity in premium.
- **Trailing take-profit** (`decide_exit`): once a position shows +25% favourable
  P&L (`AGENT_TRAIL_ARM_FRACTION`) the agent tracks its peak; a 10-point giveback
  (`AGENT_TRAIL_GIVEBACK_FRACTION`) while still positive exits
  `trailing-take-profit`. Locks a catalyst spike that peaked above the fixed
  +35% target but faded below it between 5-minute cycles.
  `TrackedCondor.peak_gain_fraction` persists across restarts. Debit and credit
  structures; the fixed target and hard stop still take precedence.

## 2026-09-02 (evening) — Steps 4-7 + the phantom-order overhaul

The morning's jam (session.json tracking four positions the broker did not hold,
three stuck unfilled orders, the agent stuck at 4/4 doing nothing) was a single
root cause: a position was recorded OPEN on order *submission*, not on fill.
Fixing that pulled in five more defects, then the remaining handoff steps.

| commit | change |
|--------|--------|
| `06264ec` | `PendingOrder` + `reconcile_pending_orders` — a trade is open only when the broker confirms `filled`; unfilled orders are cancelled after 2 cycles and free their slot; pending orders count toward the position cap |
| `478dc53` | long-vol expiry floored at the macro catalyst — no more Thursday strangles bought for a Friday print |
| `72dc762` | `reconcile_open_book` — every cycle the open book is rebuilt from broker truth; phantoms dropped, orphan legs adopted and closed by the expiry gate; all structures asserted to submit as one atomic MLEG |
| `376f69c` | per-symbol dedup — a ticker with a position *or* a working order is excluded before ranking (a fill was freeing a slot and the ranker re-picked the same name, opening a second IWM strangle) |
| `bd78cfb` | gate 4c, long-vol concentration: `MAX_LONG_VOL_DEBIT_PCT = 0.04`, `MAX_LONG_VOL_POSITIONS = 2` |
| `d3851b5` | catalyst hold — a long-vol position that outlives the catalyst has its -50% stop suspended; profit target and hard stop still apply |
| `7a69764` | Step 4: `AGENT_UNIVERSE` (12 ETFs), per-cycle scan table with gate pass/fail, 150s scan time-box |
| `9f4a9cf` | Step 5: Alpaca news + 5-minute intraday realized vol into the officer prompt; fetched only for orders that clear risk_manager; neither is gated |
| `43149ad` | Step 7: `alerts.py` (Discord webhook, five events, silent when unset or failing) and `journal.py` (`journal.md` + `journal.csv` with gate values and officer verdict per trade) |
| `67089ab` | Step 6: optional Alpaca MCP read path (`get_account_info`, `get_news`) with an alpaca-py fallback; the trade path never touches MCP |

433 tests green. Live on PA3FCNG4S7EO: MCP connected (72 tools), 12-name scan
running clean, book held at 2 long-vol strangles (IWM 16x, SPY 8x, both 09-04)
at 3.7% of equity against the 4% cap.

A note on the concentration cap: with MACRO_DANGER active through Friday every
structure the regime switch picks is a long strangle, so the cap is what stops
the book becoming N correlated bets on one NFP print. It is doing exactly that —
DIA/GLD/SLV/TLT/XLF/XLE/XLK/EEM all block at gate 4c each cycle.

## 2026-09-02 — Competition-window configuration (two sessions remaining)

Recalibrated for the final competition window. **The defined-risk invariant, the
5% total drawdown floor, and `MAX_RISK_PER_TRADE_PCT` as the single source of
truth are unchanged.** The static IV floor was an uncalibrated bootstrap
placeholder — the IV−RV spread is the governing edge gate and was already
passing, so the floor is dropped to a nominal 8%.

| Parameter | Old | New | File |
|---|---|---|---|
| Static IV floor (Hackathon Mode) | 0.12 | **0.08** | `data.py STATIC_IV_THRESHOLD` |
| Min credit ÷ width | 0.20 | 0.20 (unchanged) | `strategy.py MIN_CREDIT_TO_WIDTH` |
| Short-leg delta band / target | 0.20–0.25 / 0.225 | **0.25–0.30 / 0.275** | `strategy.py SHORT_DELTA_*` |
| IV-relative delta scaling | 0.15 / 0.25 | **clamped to 0.25 / 0.30** band | `strategy.py DYN_DELTA_*_IV` |
| Long-leg delta / OTM offset | 0.10 / $5 | unchanged | `strategy.py LONG_*` |
| Profit target | 0.50 | **0.35** of credit | `main.py` Config + `.env` |
| Stop loss | 2× credit | unchanged | `main.py` Config |
| Max concurrent positions | 3 | **4** (combined across symbols) | `risk_manager.py MAX_CONCURRENT_POSITIONS` |
| Max risk per trade | 0.015 | **0.02** of equity | `risk_manager.py MAX_RISK_PER_TRADE_PCT` |
| Daily loss halt | 0.025 | **0.035** of starting equity | `risk_manager.py DAILY_LOSS_HALT_PCT` |
| Efficiency-ratio gate | 0.30 | **0.45** | `strategy.py RANGE_BOUND_ER` |
| IV−RV spread minimum | 0.02 | **0.015** | `strategy.py MIN_IV_RV_SPREAD` |
| Loop interval | 900s | **300s** | `main.py` Config + `.env` `AGENT_LOOP_INTERVAL_SECONDS` |

Account switched to **PA3FCNG4S7EO** ($100k competition account); `session.json`
deleted so `starting_equity` reads live from the Alpaca API on startup. 22 test
assertions/fixtures updated to the new thresholds. 333 tests green.

## 2026-09-02 — Officer model swap + 3 alpha filters (5 commits)

The **1.5% per-trade cap is byte-unchanged**; modular flow intact. 306 → **331 tests**.

**1. risk_officer — 72B judge + QUANT CLARIFICATION (`fd91c61`).** The
`Qwen2.5-7B` officer vetoed ~100% of trades (called VIX 16 "elevated", read
contango as stress). `FEATHERLESS_MODEL` default → `Qwen/Qwen2.5-72B-Instruct`
(non-gated; Meta Llama 70B needs HF OAuth). `_call_featherless` now caps
`max_tokens` (`OFFICER_MAX_TOKENS`, 1024). `build_prompt` gains a
`### QUANT CLARIFICATION` block: **contango is NOT a veto trigger, only
backwardation is; a neutral RSI (~40-60) is IDEAL for range-bound premium
selling.** Live: first cycle after → APPROVED + executed QQQ and IWM condors.

**2. strategy — IV-relative delta (`e2ef454`).** Inverted the vol→delta map:
high IV → `DYN_DELTA_HIGH_IV` 0.15 (further OTM, more PoP); low/crushed IV →
`DYN_DELTA_LOW_IV` 0.25 (closer, keep credit); 0.225 unchanged in the normal
band. `DYN_DELTA_LOW/HIGH` renamed `_LOW_IV/_HIGH_IV`.

**3. strategy — ADX trend-strength filter (`2e61e0d`).** `context_gatherer`
gains `wilder_adx` / `classify_adx`; `intelligence_hub.yf_ohlc` pulls the OHLC
(one call); per-ticker `adx` / `adx_direction` on `MarketContext`. In
`select_regime`, after the PANIC/DANGER override: **ADX ≥ 25 + base iron_condor
→ override to a directional credit spread (Bull Put up / Bear Call down), no
clear side → stand aside. ADX < 20 → condor as-is. 20-25 → fall back to the
Kaufman ER.** yfinance OHLC failure → `adx` None → byte-identical ER logic.

**4. risk_manager — correlation guard (`32e249f`).**
`context_gatherer.correlation_clusters(closes_map)` — union-find over the
pairwise 10-day return-correlation graph, groups of ≥2 names ≥ 0.8. On
`MarketContext.correlation_clusters` (+ `cluster_for()`), from the RSI closes
(no extra fetch); `synthesis()` shows `CORRELATED (>0.8, 10d): {SPY,QQQ,IWM}`.
**Gate 4b:** if the order's `underlying` is in a cluster that already holds an
open position, block it — the cluster gets ONE slot toward the 3-position cap.
No clusters → unchanged. `ProposedOrder.underlying` / `OpenPosition.underlying`
carry the ticker; `evaluate_new_trade` threads the clusters into `check_order`.

## 2026-09-01 — Quantamental upgrade (4 modules, 4 commits)

Built on the Contextual Intelligence layer below. **The 1.5% per-trade cap is
still the sole source of truth**; the modular flow is intact. Dep added:
**yfinance** (+ its transitive deps). 278 → **306 tests**.

**1. IntelligenceHub (`intelligence_hub.py`).** yfinance-primary context: ^VIX +
^VIX3M **term structure** (ratio > 1.0 = backwardation → `PANIC_REGIME`),
`.news`, closes for RSI. Pipe-by-pipe fallback to the Alpaca fetchers in
`context_gatherer`, then `MarketContext.unavailable()`. `MarketContext` gained
`vix` / `vxv` / `vix_vxv_ratio` / `panic_regime` / `macro_danger`
(High-Impact event within 48h) + `regime_flags()`. `main._gather_market_context`
now calls it; a set flag logs `REGIME SIGNALS`.

**2. Quant strategy (`strategy.py`).** *Relative-value optimiser* —
`rank_basket(symbols, snapshots, context)` orders the basket by IV-RV spread,
news score as tiebreak; `run_cycle` evaluates in that order. *Dynamic delta
scaling* — `dynamic_short_delta(atm_iv)`: 0.10 at/below 15% IV, 0.30 at/above
30%, unchanged 0.225 in between; condor / vertical short legs use it only when it
actually deviates. *Regime override* — `select_regime(snapshot, *, context=)`:
`MACRO_DANGER` / `PANIC_REGIME` vetoes any short-vol selection and forces a long
strangle (a quant "No trade" is left alone). `select_regime` body moved into
`_quant_regime`.

**3. Multi-agent debate (`risk_officer.py`).** `debate_review()` — a **Bull**
argues for, a **Bear** against (no verdict), then a **Judge** (both cases +
MarketContext + risk_manager numbers + lessons) returns APPROVE/VETO. Same
drop-in contract as `review_trade` (which is unchanged and still handles every
non-top candidate). Failed advocate → "(unavailable)"; Judge down on both
providers → fail-safe VETO. Transcript on `OfficerReview.debate` /
`.transcript()`; `DecisionSummary.debate` carries it; `run_cycle` runs the debate
**only for the #1-ranked ticker** (`AGENT_DEBATE`, default on) and logs a
`DEBATE [SYM]` block. `build_prompt` gained optional `bull_case` / `bear_case` /
`lessons` → `### DEBATE` / `### LESSONS LEARNED` sections.

**4. Self-correction (`risk_officer.py`).** `post_trade_analysis(closed_event)` —
after a close, one LLM call turns exit reason + P&L + price action into a
`LESSON: …` line, appended (atomic, last 50) to `lessons_learned.json`.
`load_lessons()` (cap 12) feeds every future judge prompt. `run_cycle` runs it on
each close (`AGENT_SELF_CORRECTION`, default on). Best-effort — any failure
writes nothing and never blocks the loop.

New env: `AGENT_DEBATE`, `AGENT_SELF_CORRECTION`. New git-ignored runtime file:
`lessons_learned.json`.


## 2026-09-01 — Contextual Intelligence & Macro-Filter layer

Bolted a context layer onto the quantitative pipeline: macro-event guard, a VIX
proxy, ticker news, and market internals (RSI) — synthesised into one string that
the risk_officer sees and the daily audit log records. **No change to the 1.5%
cap. No new dependency** (numpy for RSI; Alpaca News API + VIXY via the existing
`alpaca-py`; macro dates are a bundled static schedule).

**1. `context_gatherer.py` (new).**
* `wilder_rsi(closes, 14)` + `classify_rsi` — pure numpy; 100 on a pure rally, 0
  on a pure selloff, 50 flat, None if < 15 closes.
* `HIGH_IMPACT_CALENDAR` — bundled 2026 FOMC decisions + CPI releases + monthly
  NFP (first-Friday). `upcoming_high_impact(now, 48h)` / `high_impact_today(now)`;
  `calendar=` is injectable for a live feed later.
* `fetch_vix_proxy` — VIXY latest trade + ~5-session % change; `+/-10%` over 5d
  flags "possibly spiking" / "falling". (^VIX isn't on Alpaca's feed.)
* `fetch_headlines` — top 4 recent headlines/ticker via the Alpaca News API,
  de-duped, long wire-dumps truncated to 180 chars.
* `score_headlines` — crude keyword net sentiment, used only as a tiebreak.
* `MarketContext.synthesis()` — `Macro: … | VIX: … | News SYM: … | RSI SYM: … | …`
  on one line; `MarketContext.unavailable()` → `"No Context Available"` and
  `macro_today_high_impact = False` (fail-safe: never trips the guard).
* `gather_context()` — orchestrator; every sub-pull degrades independently, a
  total wipe-out returns `unavailable`, nothing raises into the loop.
* `prioritize(symbols, snapshots, context)` — orders eligible tickers by IV-RV
  spread desc, news score as the tiebreak.

**2. `main.py` integration.**
`run_cycle` now: (0) `gather_context` first thing, logs the synthesis to
`agent_activity.log` (`MARKET CONTEXT` block); (0b) `macro_risk_multiplier` — a
High-Impact day sets `AccountState.risk_multiplier = 0.5` for the whole cycle and
logs `MACRO GUARD ACTIVE`; (3) pre-fetches every ticker snapshot then evaluates
in `prioritize()` order under the shared 3-cap. `DecisionSummary` gained
`market_context`; it's threaded through `evaluate_cycle_decision` /
`evaluate_new_trade` into the risk_officer's `review_snapshot` and stamped on
every summary. `reconcile_account_state` gained `risk_multiplier=`.

**3. `risk_manager.py` (surgical).** `AccountState.risk_multiplier: float = 1.0`;
gate 1's effective cap is `1.5% * equity * min(risk_multiplier, 1.0)` — a
multiplier can only *tighten*, never raise the ceiling, and `MAX_RISK_PER_TRADE_PCT`
is byte-for-byte unchanged. New `is_macro_safe(macro_high_impact=)` /
`macro_risk_multiplier(macro_high_impact=)` (→ 0.5 on a High-Impact day).

**4. `risk_officer.py`.** `build_prompt` gained a `### MACRO CONTEXT` block
carrying the synthesis string (or `No Context Available`), with the standing
instruction: analyse Macro + VIX, VETO / add scrutiny to short-vol trades when VIX
is spiking or a Red-Folder event is imminent, and use RSI to check overbought /
oversold before approving a directional credit spread.

**Tests** — +28 (250 → **278**). New `test_context_gatherer.py` (19): RSI bands,
48h calendar window + today flag, sentiment score, synthesis format, partial-vs-
total failure degradation, `prioritize` ordering. `test_risk_manager.py` +5
(macro predicate / multiplier / halved cap / constant untouched / clamp > 1.0).
`test_risk_officer.py` +2 (MACRO CONTEXT section + fail-safe). `test_main.py` +3
(context logged, priority order + macro multiplier threaded, context failure
non-fatal); the multi-ticker `run_cycle` tests now stub `_gather_market_context`
to stay offline.


## 2026-09-01 — Off-Hours Intelligence (Heartbeat / Morning Brief / Post-Mortem)

Added a "genuine autonomy" layer that runs *around* the 15-min trading loop, not
inside it. **No change to `risk_manager.py`, no change to the `strategy.py` trade
logic, no new dependency** — pure stdlib observability. New module `offhours.py`
(pure functions) + timed wiring in `main.py` (gated by markers persisted in the
session, exactly like the existing daily summary).

**1. Heartbeat.** `main._maybe_heartbeat` emits one line per
`AGENT_HEARTBEAT_MINUTES` (default 60), market open *or* closed, so the audit
trail is unbroken 24/7:
`[YYYY-MM-DD HH:MM] HEARTBEAT: Status: Idle/Active | Connectivity: OK/Error | Memory: N IV readings stored.`
`Status` = Active when the clock says open; `Connectivity` = Error when
`get_clock()` threw this loop (the heartbeat still fires from `datetime.now(ET)`);
`Memory` = raw row count of `iv_history.csv` (`offhours.count_iv_readings`).

**2. Morning Brief.** `_maybe_morning_brief` runs once per day, only inside
09:00–09:30 ET. `AlpacaConnection.premarket_gaps` pulls each basket ticker's
reference price and prior daily close → `offhours.TickerGap`. A move of
`|gap| > AGENT_GAP_ALERT_PCT` (default 0.5%) logs a **PRE-MARKET ALERT** naming
the ticker and reading it as TRENDING (directional open → favour credit spreads);
an all-flat basket logs "RANGE-BOUND bias intact".

**3. Nightly Post-Mortem.** `run_cycle` now folds each cycle's `DecisionSummary`
list into a persisted per-day funnel (`offhours.DailyActivity` in
`session.daily_activity`, last 10 days kept): ticker scans, trades proposed
(reached `risk_manager`+), approved (executed), vetoes by `risk_manager` and by
`risk_officer`, and a regime tally. `_maybe_post_mortem` runs once per day at/after
16:00 ET and renders the digest — funnel + open-position unrealized P&L
(`value_condors` sum) + `dominant_regime(...)` ("Overall Range-Bound" etc.) —
copy-pasteable for a social post or judge review.

**Logging.** `setup_logging` gained a third handler: the `agent.offhours` logger
writes to `AGENT_ACTIVITY_LOG` (default `logs/agent_activity.log`) **and**
propagates to the main log + console, so the events are both in one audit file and
in the normal stream. `run_forever` restructured so a `get_clock()` failure still
produces an Error heartbeat before the loop sleeps.

**Tests** — +30 (220 → **250**). New `test_offhours.py` (23): exact heartbeat
format, Active/Idle + OK/Error, hourly `interval_elapsed` gate, 09:00–09:30
window, `TickerGap` gap %/significance, PRE-MARKET ALERT vs flat, `accumulate_activity`
funnel (cumulative, plan-less tolerant), `dominant_regime` bucketing,
`post_mortem_text`, `DailyActivity` round-trip. `test_main.py` +7: `run_cycle`
accumulates the funnel, session round-trips the off-hours markers, legacy
`session.json` still loads, `_maybe_heartbeat` once-per-interval + Error path,
`_maybe_morning_brief` window/once-a-day, `_maybe_post_mortem` after-close/once-a-day.


## 2026-09-01 — Multi-ticker basket + dynamic market-regime switch

Expanded the agent from SPY-only iron condors to a 4-ticker basket with a
volatility-regime strategy switch, without touching the risk limits.

**1. Multi-ticker.** Basket `SPY, QQQ, IWM, TLT` (env `AGENT_TICKERS`).
`data.get_market_snapshot(symbol, creds=)` and `alpaca_trader.fetch_option_chain(..., underlying=)`
are now per-ticker; `data.get_current_option_chain` passes the underlying through.
`main.run_cycle` evaluates each ticker independently in one cycle, logging a
`DecisionSummary` per ticker. **Exposure stays global** — the max-3-position cap
is enforced on the shared `AccountState.open_positions`, and `account` is rebuilt
after each open so the next ticker sees it. One ticker's data failure logs and
the others continue. `TrackedCondor` gained `symbol` + `structure`.

**2. Efficiency ratio (range-bound filter).** `strategy.efficiency_ratio(prices,
window=10)` = |net change| / Σ|daily abs changes|; `is_range_bound(prices,
window=10, threshold=0.3)` -> ER < 0.3 range-bound, ER >= 0.3 trending;
`trend_direction()` for up/down. `data.get_market_snapshot` now returns
`daily_closes` (the same 11 closes used for realized vol).

**3. Regime switch.** `strategy.select_regime(snapshot)` -> `RegimeDecision`:
* **A** — IV-RV spread >= +0.02 *and* IV elevated -> **iron condor**.
* **B** — spread <= -0.02 *and* ER < 0.3 -> **long strangle** (`plan_long_strangle`,
  buy ~0.25Δ put + call, net debit, sized so debit ≤ 1.5%).
* **C** — spread <= -0.02 *and* ER >= 0.3 -> **bull put** (trend up) /
  **bear call** (trend down) credit spread (`plan_bull_put` / `plan_bear_call`,
  same 25%-credit and 1.5% gates as the condor).
`build_strategy_plan()` is the regime-aware entry point (replaces
`build_iron_condor` in `main`); it **logs the selection explicitly**
(`REGIME [SPY]: Regime B: Low IV / Range-Bound -> Long Strangle | <detail>`) and
attaches `regime` / `regime_reason` / `structure` to the plan, which
`main.evaluate_new_trade` forwards into the `risk_officer` prompt and the daily
summary. `IronCondorPlan` gained `structure/regime/regime_reason/symbol/direction`
(alias `StrategyPlan`).

**risk_manager — minimal Gate 5 change only (per the approved plan).**
`is_defined_risk()` now also returns True for an **all-long** position (every leg
`buy`, qty > 0) — a long strangle can't lose more than the premium and is never
naked short; any position with a `sell` leg still goes through the unchanged
matched-legs rule. `ProposedOrder.max_loss` (optional) lets a debit structure
hand its true per-contract worst case straight to gate 1, so the **1.5% cap is
still the source of truth**. No limit value changed.

**executor.** `_build_mleg_request` accepts 2- or 4-leg orders (vertical /
strangle / condor); `from_plan` alias; `from_iron_condor_plan` passes
`max_loss=plan.max_loss_per_contract` through.

**risk_officer.** `build_prompt` gained a "STRATEGY REGIME" block (ticker,
structure, regime, detail) and labels net debit vs credit.

**Tests** — +41 (179 -> **220**): efficiency ratio / range-bound / trend
direction; `select_regime` A/B/C/none; `plan_long_strangle` + credit-spread
plans + the 25% gate still applies; `is_defined_risk` all-long (+ still rejects a
mixed short leg); `ProposedOrder.max_loss` in gate 1; 2-leg MLEG + `from_plan`;
`decide_exit` debit-aware (strangle +50%/-50% of premium); multi-ticker
`run_cycle` with the global 3-cap and one-bad-ticker isolation; `TrackedCondor`
symbol/structure round-trip.

Live check: `python -m trading_agent.strategy {SPY,QQQ,IWM,TLT}` — SPY/QQQ/IWM
neutral (no trade), **TLT -> Regime B Long Strangle** (`IV 0.061 << RV -0.062`,
`ER 0.230`), real 82P/83C strangle proposed at $0.20 debit. `iv_history.csv`
migrated to `timestamp,symbol,iv,rv,spread`, one shared file, appended per ticker
per cycle (`read_iv_history(symbol)` collapses to one point per day for the
percentile gate).

## 2026-08-31 — `log_daily_iv()`: one row per calendar day

`log_daily_iv()` appended a row on every call — the loop would have written
`iv_history.csv` every 15 min, flooding the IV-percentile history with same-day
points. Now it checks `_has_iv_row_for_date(log_path, today)` first and is a
**no-op if today's date is already present** (first reading of the day wins), so
`main.py` can call it every cycle unchanged. `iv_history.csv` cleaned up:
5 same-day test rows from today collapsed to the day's first reading
(`2026-08-31T19:13:28 … 0.1337`). `tests/test_data.py` +4 (15 → 19); suite
**175 → 179**.

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
