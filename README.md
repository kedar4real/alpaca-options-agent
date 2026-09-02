# Options trading agent

An autonomous options agent that trades a basket of liquid ETFs on an Alpaca
paper account. It picks a structure based on the volatility regime, sizes it
against a hard risk budget, has an LLM argue the trade before it goes out, and
manages the position to an exit. Built for the Alpaca × LabLab.ai hackathon.

Everything it trades is **defined-risk** — iron condors, vertical credit spreads
and long strangles. There is no code path that can open a naked short option;
the invariant is enforced in `risk_manager.is_defined_risk()` and re-checked
inside the executor immediately before submission.

```
python -m trading_agent.main
```

---

## How a cycle works

The loop runs every 5 minutes during market hours. Each pass:

**1 — Resolve last cycle's orders.** Every submitted order is checked against
the broker. A confirmed fill becomes an open position; a cancelled or rejected
order is dropped; anything still unfilled after two cycles is cancelled and its
slot released. A position is only ever "open" because the broker says it
filled — never because an order was accepted.

**2 — Reconcile the book.** Tracked positions are rebuilt from
`get_all_positions()`. Anything the broker doesn't hold is dropped as a phantom.
A loose leg the agent doesn't recognise — one side of a strangle that filled
alone — is adopted and handed to the expiry gate to clean up.

**3 — Manage what's open.** Profit target, trailing take-profit, stop loss, and
a forced close within one trading day of expiry. First trigger wins.

**4 — Scan the universe.** One narrowed option-chain snapshot per symbol
(1–3 DTE, strikes ±5% of spot), inside a 150-second budget so a slow venue
can't stall the loop. Every symbol gets a line in the scan table with its gate
values.

**5 — Rank and evaluate.** Eligible names are ordered by IV−RV spread, then news
sentiment. Each runs through the pipeline in strict order — a rejection at any
stage stops the rest:

```
strategy → risk_manager → risk_officer → executor
```

The top-ranked candidate gets a full Bull/Bear/Judge debate; the rest get a
single-pass review.

---

## Choosing the structure

`strategy.select_regime()` reads the volatility surface and picks accordingly:

| Regime | Condition | Structure |
|---|---|---|
| A | IV > RV by ≥ 1.5 vol points, IV elevated | Iron condor |
| B | IV ≪ RV, range-bound (Kaufman ER < 0.45) | Long strangle |
| C | IV ≪ RV, trending | Bull put / bear call, aligned with the trend |
| — | anything else | No trade |

Two overrides sit on top of that.

**ADX filter.** A confirmed strong trend (ADX ≥ 25) is the classic iron-condor
killer — you sell a range right as it breaks. The condor is disabled and a
directional credit spread demanded instead; if the trend has no clear side, the
agent stands aside.

**Macro override.** When a high-impact event (FOMC, CPI, NFP) lands inside 48
hours, or the VIX term structure inverts, every short-volatility selection is
vetoed and swapped for a long strangle. Don't sell premium into a known
catalyst. The override never manufactures a position — a quant "no trade" is
left alone.

A long strangle opened under the macro flag has its expiry floored at the
catalyst date. A strangle bought for Friday's payrolls that expires Thursday is
a guaranteed loss, and that mistake cost real money before the floor existed.

---

## Risk gates

Six hard limits. Gates 1–5 run on a proposed order before it can be sent; gate 6
runs across the open book. `check_order()` collects *every* failure rather than
short-circuiting, so a blocked trade explains itself completely.

| # | Gate | Limit |
|---|---|---|
| 1 | Max risk per trade | 2.0% of current equity (halved on a high-impact macro day) |
| 2 | Daily loss halt | 3.5% of starting equity → no new trades today |
| 3 | Total drawdown floor | 5% of starting equity → sticky halt for the run |
| 4 | Max concurrent positions | 4 |
| 4b | Correlation guard | a >0.8 (10-day) correlated cluster gets one slot, not one each |
| 4c | Long-vol concentration | ≤ 3 debit positions, ≤ 4% of equity in total premium |
| 5 | Defined-risk invariant | matched long/short contracts per right, or all-long |
| 6 | Expiry auto-close | force-close within one trading day of expiry |

Gate 4b exists because three condors on SPY, QQQ and IWM is one leveraged bet on
equity beta wearing a diversification costume. Gate 4c is the same argument for
the other side of the book: under a macro override every structure the regime
switch can pick is a long strangle, and without a cap you end up holding N
correlated bets on a single print.

`MAX_RISK_PER_TRADE_PCT` in `risk_manager.py` is the single source of truth for
sizing. `strategy.py` imports it to size the proposal; `check_order()` re-derives
the dollar cap from *live* equity before the order goes out. The two cannot
drift.

---

## The risk officer

Every order that clears the numeric gates is written up as a prompt — structure,
legs, regime and its quantitative justification, volatility backdrop, current
exposure, macro calendar, VIX term structure, per-ticker RSI and ADX, recent
headlines, and today's intraday realized vol — and handed to an LLM for a final
APPROVE/VETO with a written thesis.

The top-ranked candidate each cycle gets three passes: a Bull agent arguing for,
a Bear agent arguing against, and a Judge weighing both. Closed trades are fed
back through `post_trade_analysis()` into `lessons_learned.json`, which is
injected into later debates.

The prompt carries an explicit quant clarification section, because the judge
kept getting two things backwards:

- VIX **contango** (front < 3-month) is the market's normal state, not stress.
  Only backwardation is a panic signal.
- A **neutral RSI** (40–60) is ideal for a range-bound premium seller, not a
  reason to veto.

Before that section existed the veto rate was near 100%.

Provider: [Featherless](https://featherless.ai) (`Qwen/Qwen2.5-72B-Instruct`)
with a local Ollama model as automatic fallback. A provider outage degrades to
the fallback; a total failure vetoes rather than approves.

---

## Exits

| Trigger | Credit structures | Debit structures |
|---|---|---|
| Profit target | 35% of credit captured | +35% of premium paid |
| Trailing take-profit | armed at +25%, exits on a 10-point giveback | same |
| Stop loss | 2× the credit received | −50% of premium |
| Expiry | within one trading day | within one trading day |
| Hard stop | flatten everything at `AGENT_HARD_STOP_ET` | same |

The trail exists because a 5-minute loop can miss a spike. If a position peaks at
+50% and fades to +20% between samples, the fixed target never fires and the gain
evaporates. Once a position has shown +25% the agent records its high-water mark
and exits on a meaningful giveback while still in profit.

**Catalyst hold.** A long-vol position that outlives the macro event it was
bought for has its stop loss suspended — a −50% mark the day before payrolls is
noise, and stopping out there means paying for the ticket and leaving before the
show. The profit target and hard stop still apply.

---

## Data

**Alpaca** — option chains with greeks and IV, spot prices, daily and intraday
bars, news, and all order routing. Multi-leg structures go out as a single
`OrderClass.MLEG` limit order, never legged.

**yfinance** (`intelligence_hub.py`) — the quantamental layer. VIX / VIX3M term
structure for the panic-regime flag, per-ticker 14-day RSI and Wilder ADX, and
10-day return correlation clusters feeding the correlation guard. Falls back to
Alpaca pipe by pipe; a total outage yields "No Context Available", which the
officer is told to treat as a reason for caution rather than confidence.

**Static macro calendar** — the 2026 FOMC, CPI and Employment Situation
schedule, bundled rather than fetched. Three dates a month don't justify a
dependency.

The per-cycle account read and news can optionally be served through Alpaca's
MCP server instead of `alpaca-py` — see [Alpaca infrastructure](#alpaca-infrastructure).

---

## Observability

The agent is meant to be auditable after the fact, not just watched live.

- **Decision summary** — one per symbol per cycle, naming the exact stage and
  reason a trade did or didn't happen.
- **Scan table** — every universe symbol with price, ATM IV, RV, IV−RV, ER, the
  IV floor, best credit/width, and the outcome, pass/fail per gate.
- **Morning brief** — pre-market gaps vs the prior close, with an alert past a
  configurable threshold.
- **Nightly post-mortem** — the day's funnel (scans → proposed → approved,
  vetoes by gate), open P&L, dominant regime.
- **Heartbeat** — hourly, market open or closed, so a silent log means a dead
  process rather than a quiet market.
- **Trade journal** — `python -m trading_agent.journal` writes `journal.md` and
  `journal.csv`: one row per trade with legs, entry and exit, realized P&L, the
  gate values recorded at entry, and the officer's verdict and thesis.
- **Discord alerts** — optional; posts on fill, close (with P&L), halt,
  hard-stop flatten, and cycle exception.

---

## Setup

Python 3.12 is pinned — some `alpaca-py` dependencies have no 3.13/3.14 wheels.

```
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored.

Run the tests:

```
pytest -q          # 442 tests, no network
```

Run the agent:

```
python -m trading_agent.main
```

Individual modules are runnable for inspection — `python -m trading_agent.data
QQQ` prints a market snapshot, `python -m trading_agent.strategy SPY` prints the
regime decision and the structure it would build.

---

## Configuration

All behaviour is environment-driven; nothing operational is hardcoded.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_UNIVERSE` | 12 ETFs | scan universe (`AGENT_TICKERS` is an alias) |
| `AGENT_LOOP_INTERVAL_SECONDS` | 300 | cycle cadence |
| `AGENT_SCAN_TIMEBOX_SECONDS` | 150 | per-cycle snapshot budget |
| `AGENT_PROFIT_TARGET_FRACTION` | 0.35 | profit target |
| `AGENT_TRAIL_ARM_FRACTION` | 0.25 | arm the trailing take-profit |
| `AGENT_TRAIL_GIVEBACK_FRACTION` | 0.10 | trail giveback that triggers an exit |
| `AGENT_STOP_LOSS_MULTIPLE` | 2.0 | credit-structure stop |
| `AGENT_DEBIT_STOP_FRACTION` | 0.50 | debit-structure stop |
| `AGENT_HARD_STOP_ET` | — | flatten everything at this ET wall-clock time |
| `AGENT_HALT_FILE` | `HALT` | if this file exists: manage only, open nothing |
| `AGENT_DEBATE` | on | Bull/Bear/Judge on the top candidate |
| `AGENT_SELF_CORRECTION` | on | post-trade lessons into later debates |
| `AGENT_DISCORD_WEBHOOK` | — | alert webhook; silent if unset |
| `AGENT_MCP` / `AGENT_MCP_SERVER_DIR` | on / — | MCP read path |
| `FEATHERLESS_MODEL` | `Qwen/Qwen2.5-72B-Instruct` | officer model |

Two operator controls worth knowing:

**`HALT` file.** `touch HALT` at the repo root and the agent finishes managing
open positions but opens nothing new. Delete it to resume. No restart needed.

**Hard stop.** `AGENT_HARD_STOP_ET="2026-09-04 10:30"` flattens the book at that
ET wall-clock time and refuses every trade afterwards, then keeps running to
confirm the book stays flat.

---

## Alpaca infrastructure

The agent reads and trades through Alpaca over two paths.

**Trade path — `alpaca-py` only.** Order submission, risk gates, position
management and the hard-stop flatten all go through `TradingClient`. No optional
dependencies, and nothing about executing a trade can be affected by an
auxiliary service being down.

**Read path — Alpaca MCP server, with `alpaca-py` as fallback.** At startup the
agent opens a stdio MCP session:

```
uv run --directory <AGENT_MCP_SERVER_DIR> alpaca-mcp-server
```

| Read | MCP tool | Fallback |
|---|---|---|
| account snapshot | `get_account_info` | `TradingClient.get_account()` |
| per-symbol headlines | `get_news` | `alpaca_trader.fetch_recent_news()` |

MCP is an enhancement, never a dependency. A missing SDK, a server that won't
start, a handshake timeout, a failing tool call, or an unparseable payload each
degrade silently. Every call logs which route served it. The MCP server reads
credentials from its own environment, so the agent passes them into the
subprocess.

---

## Layout

```
src/trading_agent/
  alpaca_trader.py     Alpaca primitives — chains, greeks, bars, news, OCC parsing
  data.py              market snapshot, IV regime gate, IV history
  intelligence_hub.py  yfinance quantamental layer (VIX curve, RSI, ADX, correlation)
  context_gatherer.py  MarketContext model, macro calendar, news scoring
  strategy.py          regime switch and structure builders
  risk_manager.py      the six gates — pure, deterministic, no IO
  risk_officer.py      LLM review, Bull/Bear/Judge debate, post-trade lessons
  executor.py          gated MLEG submission
  main.py              the loop: reconcile, manage, scan, evaluate, report
  journal.py           journal.md / journal.csv from session history
  alerts.py            Discord notifications
  mcp_client.py        optional MCP read path
```

`session.json` holds durable state — starting equity, open positions, pending
orders, event history, the sticky halt latch. `starting_equity` is persisted on
first run and never re-derived, or the drawdown floor would silently drift on
every restart.

---

## Testing

442 tests, entirely offline. No test touches the network or the broker; every
external boundary is injected. The pure logic — gates, regime selection, exit
triggers, reconciliation, ranking, prompt construction — is tested directly, and
the IO layer is tested against fakes.

Everything past the first working version was built test-first. The interesting
cases came from live failures: a position recorded open on submission instead of
fill, a strangle expiring before the catalyst it was bought for, a fill freeing a
slot that the ranker immediately refilled with the same ticker. Each one is now a
named test.

```
pytest -q
pytest tests/test_risk_manager.py -v      # one block per gate
```

---

## Known limitations

- **ETFs only.** Single-name equities would need an earnings-date gate; the macro
  calendar only knows FOMC, CPI and NFP, so the agent would happily sell premium
  into an earnings print it can't see.
- **Delayed option quotes.** The chain uses Alpaca's INDICATIVE feed. Switching
  to OPRA real-time is a one-line change but needs the subscription.
- **The macro calendar is bundled for 2026** and needs extending each year. Past
  the last entry it simply reports nothing upcoming.
- **IV percentile needs history.** Below 10 days of readings the IV gate falls
  back to a static floor, which is an uncalibrated bootstrap rather than a real
  edge check — the IV−RV spread is the governing gate.
- **Paper trading only.** Nothing here has run against a live account, and the
  fill assumptions (mid-price limits on multi-leg orders) are optimistic in a way
  that only shows up with real money.

---

## License

MIT — see [LICENSE](LICENSE).
