# Audit Dashboard — Handoff / Improvement Brief

_For the next agent picking this up._ Everything you need to understand the
current state, extend it safely, and not re-break the parts that were fiddly.

---

## 1. What this is

A **read-only, retrospective** Streamlit dashboard that reconstructs what the
"Volatility Arbiter" options-trading agent did during the Alpaca × LabLab.ai
hackathon (paper account `PA3FCNG4S7EO`, sessions Sep 1–4 2026).

Audience: **hackathon judges.** The narrative it sells:

1. The agent lived inside a hard **cage** ($95k equity floor, 1.5% max loss per
   trade, defined-risk structures only).
2. **The deterministic risk gate is the hero** — of ~165 proposed structures in
   the final session, 98 were killed by `risk_manager` before any LLM ran; the
   Bull/Bear/Judge debate is the _second_ line of defence, not the first.
3. When a real execution bug appeared (leg-by-leg closes → transient naked short
   → broker rejection → orphan legs), it was fixed structurally with an atomic
   multi-leg close (**commit `fcafbbd`**).

It has **no control surface**. It never imports an order-capable Alpaca client.
The "Emergency Flatten" button is permanently `disabled=True`.

---

## 2. File map

```
audit/
  dashboard.py            Streamlit app  (presentation only; ~570 lines)
  audit_data.py           Pure parsers   (no Streamlit, no network; fully tested)
  tests/test_audit_data.py 36 unit tests
  requirements.txt        streamlit, altair, pandas   (pandas already a core dep)
  README.md               how to run
  HANDOFF.md              this file
.streamlit/config.toml    pinned light theme (repo root — Streamlit reads $CWD/.streamlit)
REPORTS/audit_snapshot.json   frozen closing snapshot the dashboard reads
```

`pyproject.toml` has an optional extra: `[project.optional-dependencies] audit`.

Run: `uv pip install -r audit/requirements.txt` then
`uv run streamlit run audit/dashboard.py` (from repo root). Tests:
`uv run pytest audit/tests/ -q`.

---

## 3. Data sources (all on-disk, no network)

| Path | Tracked in git? | Shape |
|---|---|---|
| `session.json` | **no** (gitignored) | dict: `starting_equity`, `account_id`, `trading_halted`, `open_condors[]`, `history[]` |
| `logs/agent_activity.log` | **no** (gitignored) | plain text, block-structured (see below) |
| `REPORTS/FINAL_SESSION_AUDIT.md` | untracked (exists on disk) | markdown, one `## SYM — <when> ET` section per weighed trade |
| `REPORTS/audit_snapshot.json` | **yes** (committed) | `{captured_at, account_id, equity, last_equity, cash, position_legs[], open_orders}` |
| `journal.md` | no (gitignored) | markdown table; **currently not read by the dashboard** — candidate source |

Point the dashboard at another checkout with `AUDIT_ROOT=/path/to/repo`.

### 3a. `session.json` → `history[]` records

`kind` ∈ `opened | submitted | closed | cancelled | cancelled-unfilled |
reconciled`. `reconciled` rows are notes (no `symbol`) and are dropped.
`submitted` rows carry the rich stuff:

```jsonc
{
  "kind": "submitted", "at": "2026-09-03T10:33:28-04:00",
  "id": "d9b7efb8-…", "symbol": "QQQ", "structure": "iron_condor",
  "quantity": 3, "entry_credit": 1.775, "expiry": "2026-09-04",
  "regime": "Regime A: High Volatility -> Iron Condor",
  "detail": "3x QQQ iron_condor exp 2026-09-04 credit $1.77 width $7.00 [SP:… BP:… SC:… BC:…]",
  "gates":  { "iv_rv_spread": .071, "credit_to_width": .253,
              "order_risk": 1567.5, "max_risk_allowed": 1982.7 },
  "officer":{ "provider": "featherless", "approved": true, "thesis": "…" }
}
```

`opened` rows for the same `id` come later with fewer fields → `trade_history()`
de-dupes by `id`, overlaying non-empty fields (chronological order guarantees
`submitted` → `opened`).

### 3b. `agent_activity.log` blocks

**Timestamps in this file are the machine's local wall clock = IST (UTC+5:30)**,
_not_ ET. During the hackathon `ET = IST − 9:30` (EDT is UTC−4:00), so a log line
stamped `2026-09-04 01:29:59` is really `2026-09-03 15:59:59 ET`. The dashboard
does **not** convert — it labels these "log local time". `FINAL_SESSION_AUDIT.md`
section headers, by contrast, are already in ET (`## QQQ — 2026-09-03 13:49 ET`).

* `MARKET CONTEXT` block — a header line, then one long `Macro: … | VIX: VIX
  14.36 / VXV 17.40 (ratio 0.82, contango) | … | RSI SPY: 62.8 (neutral) | ADX
  SPY: 12.8 (…) | News QQQ: … | RSI QQQ: …` line. `parse` targets: the `VIX:`
  clause and every `RSI <SYM>: <n> (<label>)`.
* `DEBATE [SYM]` block — `--- BULL ---` / `--- BEAR ---` / `--- JUDGE (provider)
  ---`, each followed by `VERDICT: APPROVE|VETO` and `THESIS: …`.
* `SCAN TABLE` block — `  SPY   px 773.12  IV 0.085  RV 0.083  IVRV +0.002 FAIL
  ER 0.31 ok  floor ok  c/w  --  -> skipped [precheck] <reason>`. **The reason
  is truncated at ~72 chars in the log itself** ("no tradeable regim").
* `NIGHTLY POST-MORTEM` — `Ticker scans today: 447`, `Trades proposed: 165`,
  `Trades approved: 40`, `Vetoed by risk_manager: 98`, `Vetoed by risk_officer:
  27`, `Open positions: 2`, then a `Regime breakdown:` list of `- <label>: <n>`.
* `RUN MODE` — the configured "cage" banner, bullet lines between `===` fences.
* `HEARTBEAT` — `[<ts>] HEARTBEAT: Status: Idle | Connectivity: OK | Memory: N`.

### 3c. `FINAL_SESSION_AUDIT.md`

```
## QQQ — 2026-09-03 13:49 ET

**Pipeline outcome:** DECISION SUMMARY — Vetoed at [risk_officer]: …
    order: 3x QQQ iron_condor exp 2026-09-04 credit $1.71 width $6.00 [SP:…]

```
--- BULL ---
VERDICT: APPROVE
THESIS: …

--- BEAR ---
VERDICT: VETO
THESIS: …

--- JUDGE (featherless) ---
VERDICT: VETO
THESIS: …
```
```

~20 sections. **2 of them are corrupt** — the LLM returned a runaway string of
`!` instead of a verdict/thesis. Parsers must not choke on these (they don't).

---

## 4. `audit_data.py` API (stable contract — tests pin all of it)

```python
SAFETY_FLOOR_USD = 95_000        # $ equity floor  (panic-flatten + halt)
PER_TRADE_CAP_PCT = 1.5          # % of live equity, max defined loss per trade

load_text(path) -> str                       # "" if missing/unreadable
load_session(path="session.json") -> dict    # {} if missing/invalid
load_json(path) -> dict                       # alias of load_session

last_market_context(activity_text) -> dict | None
    # {vix: float, vxv: float, ratio: float, state: "contango"|"backwardation",
    #  ts: str|None, macro_line: str}   — the LAST block wins

ticker_metrics(activity_text, symbols) -> list[dict]
    # one row per symbol (order preserved):
    # {symbol, price, iv, rv, ivrv, er, rsi, rsi_label}   — any field may be None
    # RSI/label come from the last MARKET CONTEXT; px/iv/rv/ivrv/er from the last
    # SCAN TABLE. If those two blocks are from different cycles the row mixes them.

parse_debates(*, audit_md="", activity_text="") -> list[dict]   # newest LAST
    # prefers audit_md; falls back to DEBATE[] blocks in the activity log.
    # {symbol, when, outcome: "executed"|"vetoed"|"blocked"|"debated",
    #  pipeline: str, rounds: [{role:"BULL"|"BEAR"|"JUDGE", provider:str|None,
    #                           verdict:"APPROVE"|"VETO"|None, thesis:str}]}
    # corrupt transcript -> rounds == [] or verdict None. thesis capped at 1200 chars.

no_trade_decisions(activity_text, limit=20) -> list[dict]        # newest FIRST
    # every SCAN TABLE row that did NOT execute:
    # {symbol, price, iv, rv, ivrv, er, decision:"skipped"|"blocked"|"vetoed",
    #  stage, reason}

veto_ratio(activity_text) -> dict     # {} if no NIGHTLY POST-MORTEM
    # {scans, proposed, approved, gate_vetoes, ai_vetoes, open_positions}
    # reads only the LAST post-mortem.

regime_breakdown(activity_text) -> list[tuple[str, int]]   # last post-mortem

trade_history(session) -> list[dict]  # chronological; de-duped by order id
    # {when, kind, symbol, structure, qty, credit, width, expiry, regime,
    #  iv_rv_spread, credit_to_width, order_risk, max_risk_allowed,
    #  officer_provider, officer_approved, officer_thesis, detail}

account_summary(session, closing_equity, *, force_stopped_flat=False) -> dict
    # {account_id, starting, current, pnl_abs, pnl_pct, open_positions,
    #  halted, status_label, is_flat}
    # force_stopped_flat=True -> status_label "STOPPED / FLAT", is_flat True,
    # regardless of open_condors.  The dashboard passes True (see §7).

open_positions_detail(session) -> list[dict]
    # {symbol, structure, expiry, qty, entry_credit, peak_gain_fraction, legs[]}

latest_run_mode(activity_text) -> list[str]   # bullet lines of the last RUN MODE

# ---- added in the polish pass (all additive, all reading logs/agent.log) ----
daily_equity_marks(agent_log_text) -> list[dict]
    # {date:"YYYY-MM-DD", equity, day_pnl, source:"logged"} from DAILY PERFORMANCE SUMMARY

equity_series(agent_log_text, session, snapshot) -> list[dict]
    # start anchor + logged marks + snapshot anchor; each point tagged
    # source: "start"|"logged"|"snapshot". Marks before session.created_at dropped.

decision_log(agent_log_text, *, stage=None, limit=40) -> list[dict]   # newest first
    # {ts, symbol|None, outcome, stage, reason}  — untruncated DECISION SUMMARY text
decision_log_stage_counts(agent_log_text) -> dict   # {stage: count} across all lines

trade_history_grouped(session) -> list[dict]   # sorted by last-activity ascending
    # one row per (symbol, structure): {submitted, opened, closed, cancelled, qty,
    #   first, last, credit_lo, credit_hi, realized_pnl, closes_with_pnl,
    #   officer_ok, officer_seen}   — realized_pnl sums closed.pnl

debate_agreement(debates) -> dict          # over parse_debates() output
    # {n, judge_veto, judge_approve, split, judge_with_bear, judge_with_bull,
    #  both_veto, both_approve}
```

These functions are **pure** — string/dict in, plain data out. Keep them that
way; that's why the whole surface is unit-tested (36 tests) and the Streamlit
file has zero logic worth testing. `logs/agent.log` is now a data source too
(equity marks + full decision reasons); `agent_activity.log` stays the source
for market context, debates, scan table, and the nightly post-mortem.

---

## 5. Current UI layout (top → bottom)

Verified via headless render. Light theme, `layout="wide"`, IBM Plex Mono for
numerics / Inter for prose. Single-page scroll (no tabs — kept for the judge
"one skim" review).

Post-polish order: **status bar → equity curve + 4-metric strip → 3 columns
(Market Context / The Logic / The Evidence) → full-width Decision Log → cage
compliance → Gate-is-the-Hero funnel + regime mix → Incident Response diff →
Data provenance & disclosures.** The blocks below describe the columns as they
were; the Decision Log now lives full-width between the columns and the cage
strip, and the disclosures are consolidated into the final section (the inline
log-local-time captions stay in place).

### Status bar (custom HTML, always dark)
`THE VOLATILITY ARBITER · v1.0 · session PA3FCNG4S7EO · Sep 1–4 2026`
&nbsp;·&nbsp; `[VIX: 14.36] [VXV·3M: 17.40] [RATIO: 0.82] [TERM: CONTANGO]`
&nbsp;·&nbsp; `[PA3FCNG4S7EO] — STOPPED / FLAT` (amber). Chips turn amber if
`state == "backwardation"`.

### Sidebar — "the Account Seal"
* `## The Volatility Arbiter` / caption `Post-Competition Audit`
* `st.error("System Status: STOPPED / FLAT")`
* 3 metrics: **Starting Equity** `$99,870.90`, **Current Equity** `$96,977.27`
  (Δ `-2,893.63`), **P&L** `-2.90%`
* "Safety Invariants — the cage": 3 custom `.invariant` cards — `HARD FLOOR
  $95,000`, `PER-TRADE CAP 1.5%`, `DEFINED RISK ONLY`
* `st.button("🛑 Emergency Flatten", disabled=True, help="Agent is already flat
  and stopped.")`

### Body — `st.columns([1.05, 1.25, 1.15])`

**Col 1 · Market Context**
* Altair 2-point line: `VIX · 1M` → `VXV · 3M`. `configure_view(fill=…)` turns
  the plot background amber when `vix > vxv` (inversion). Below it a
  contango/inversion callout + "Last reading logged … (log local time)".
* `Basket — last cycle` dataframe: `Ticker | Price | RSI | Zone | IV | RV` for
  SPY/QQQ/IWM.

**Col 2 · The Logic**
* `Multi-Agent Debate · N transcript(s)`. A `st.selectbox` of
  `"NN. SYM · <when> · OUTCOME"`, defaulting to the most recent `vetoed` one.
* For the picked debate: `st.caption` of the pipeline outcome, then one
  `st.chat_message(role, avatar="🐂"|"🐻"|"⚖️")` per round, each with a coloured
  role label + a `.verdict-pill` (`APPROVE` green / `VETO` red / `NO VERDICT`
  grey) + the thesis text.
* `st.divider()`, then **Decision Log** — the last-20 `no_trade_decisions`
  dataframe (`Sym | Call | Stage | IV | RV | IV-RV | ER | Reason`), height 340.
* `st.caption` with the session veto totals from `veto_ratio`.

**Col 3 · The Evidence**
* **Trade History** dataframe (`When | Sym | Structure | Qty | Credit | Width |
  Risk $ | Cap $ | Officer | OK?`), height 300, ~48 rows.
* `st.caption`: "every Risk $ sits under its Cap $ …".
* **Still-open at session end** dataframe (SPY / IWM bull_put, exp 2026-09-08).
* `.note-box` (amber): the Atomic MLEG execution-invariant note referencing
  commit `fcafbbd`.

### "The Gate is the Hero" — `st.columns([1, 1])`
* Left: Altair horizontal bar — `Rejected by Gate (98) | Vetoed by AI (27) |
  Approved / Executed (40)`, slate / amber / green. Caption drives the point
  home.
* Right: `Regime mix` dataframe (top 8 of `regime_breakdown`) +
  `st.expander("Final RUN MODE banner")` → `st.code(latest_run_mode)`.

### "Incident Response: Infrastructure Hardening (Commit `fcafbbd`)"
Two `st.code` blocks side by side — **Before** (leg-by-leg `close_position`
loop) vs **After** (`build_close_request` → one reversing `OrderClass.MLEG`
market order). Caption on why it's a market order. Final `st.caption` = the
snapshot footnote (honest disclosure, see §7).

---

## 6. Styling / theme

* `.streamlit/config.toml` pins `[theme] base = "light"` with explicit colours.
  **This is load-bearing** — without it the page inherits the viewer's dark-mode
  preference and the CSS's dark ink lands on dark surfaces (the original
  "colors are trash" bug).
* The `<style>` block in `dashboard.py`:
  * forces the light canvas on `.stApp`, `stAppViewContainer`, `stMain`,
    `stHeader`, `stSidebar` with `!important`;
  * sets dark ink on `p/li/label/td/th/headings/stMarkdown` — **deliberately
    NOT on bare `span`**, because that clobbers Streamlit's Material icon font
    (symptom: the literal text `keyboard_double_arrow_right` where the sidebar
    chevron should be). There's an explicit rule re-asserting the icon font.
  * every custom component (`.status-bar`, `.invariant`, `.note-box`,
    `[data-testid="stChatMessage"]`, `.verdict-pill`, `.role-*`) sets **both**
    background and colour so it survives any theme.
* `toolbarMode = "viewer"` keeps the `⋮` menu reachable (Settings → Theme) as a
  manual fallback; the header is de-cluttered, not hidden.
* `use_container_width=` is deprecated in this Streamlit (1.63, past the cutoff
  date) — the code uses `width='stretch'`.

---

## 7. Known limitations / honest caveats

1. **`force_stopped_flat=True` is hard-coded.** At build time the agent process
   was still running and `session.json` still listed 2 open SPY/IWM legs (the
   midnight-ET hard-stop couldn't fire while the market was closed; it flattens
   at the 09:30 ET open). The dashboard presents STOPPED / FLAT per the judge-
   facing spec, with **one honest footnote** at the very bottom citing the
   snapshot's real equity + leg count. If you regenerate the snapshot after the
   real flatten, consider switching to
   `force_stopped_flat=(len(snapshot["position_legs"]) == 0)`.
2. **Decision Log reasons are truncated** (~72 chars) because the SCAN TABLE in
   the log truncates them. The full reasons live in `logs/agent.log` decision
   lines (`… DECISION SUMMARY — …`) — a richer but differently-formatted source.
3. **`veto_ratio` / `regime_breakdown` read only the last post-mortem.** Multiple
   trading days → only the newest is shown. No day picker.
4. **Trade History shows the raw churn** (~48 rows, many near-duplicate SPY bull
   puts from the 15%-TP flip loop). No grouping/collapse.
5. **Term structure is VIX vs VXV (3-month)**, not VIX vs VIX9D (9-day) as the
   original spec's header text implied — the log only carries VXV. Labelled
   faithfully as `VXV · 3M`.
6. **Log timestamps are IST (ET = IST − 9:30)**, surfaced as "log local time".
   Not converted. The `FINAL_SESSION_AUDIT.md` headers are already ET.
7. **`websockets`**: `streamlit` pins `<17`; the repo lock has `17.1`. It runs
   fine on 17.1 (verified) but a `uv sync` could force the downgrade. If that
   bites, put the dashboard in its own venv.
8. Verdicts are encoded by **colour + text** (pills say APPROVE/VETO) — OK for
   accessibility. The term-structure amber cue also has a text callout.
9. No `dashboard.py` tests. `streamlit.testing.v1.AppTest` works here
   (`AppTest.from_file("audit/dashboard.py").run()`; assert `len(at.exception)==0`).
10. Binds `0.0.0.0:8501` by default, no auth. Read-only by construction (grep
    the file — it never constructs a `TradingClient`).

---

## 8. Improvement backlog (rough priority order)

**DONE in the polish pass** (commits `ab170aa`, `0c78084`, `326f5bc`, `6c208b2`,
`3cc9e35`, `8df0fef`):
* ~~Equity curve~~ — `equity_series()` from `logs/agent.log` DAILY PERFORMANCE
  SUMMARY marks (directly logged); Altair line, top of page.
* ~~Full-text Decision Log~~ — `decision_log()` + `st.segmented_control` stage
  filter, now a full-width section.
* ~~Cage-compliance strip~~ — utilisation bars vs a 100% cap line.
* ~~Collapse the churn~~ — `trade_history_grouped()` + "Expand raw" expander.
* ~~Debate filter + agreement~~ — `debate_agreement()` + outcome filter.

**Still open — high value, low risk**
* **Equity curve resolution.** Only 4 daily marks exist. For an intraday curve,
  snapshot `alpaca` `get_portfolio_history` into a series file, or mine the
  parallel React dashboard's `dashboard/server/readers.py` / `live.py`.
* **Layered-Altair height bug.** `alt.layer(...)` / `alt.Chart` with certain
  axis opts (`nice=False`, `gridColor`, `domain=False`) collapses to ~0 plot
  height in this Streamlit build — the equity chart hit this. Working formula:
  single mark, `mark_line` (not `mark_area`, which forces a zero baseline),
  `scale=alt.Scale(zero=False, domain=[...])`, plain axes, `.properties(height=N)`,
  `.configure_view(...)`, `st.altair_chart(..., width="stretch")`. The
  cage-compliance chart layers a `mark_rule` fine, so it's the axis-opt combo.

**Medium**
* **Multi-session support** — day picker feeding `veto_ratio` /
  `regime_breakdown` / the funnel; parse every post-mortem, not just the last.
* **Regime timeline** — stacked area / step chart of regime-per-cycle over the
  session (data: `REGIME [SYM]:` lines in `logs/agent.log`, or the SCAN TABLE
  decision column).
* **Derive status from the snapshot** instead of the hard `force_stopped_flat`,
  with an explicit `?live=1` query-param override for a genuinely live view.
* **Timezone normalisation** — convert IST log stamps to ET for display.
* **Downloadable audit bundle** — a "Download evidence pack" that zips the
  three source files + a rendered PDF (via `st.download_button`).

**Nice-to-have**
* AppTest smoke test in `audit/tests/`.
* `st.tabs` instead of one long scroll (Overview / Logic / Evidence / Post-mortem).
* Wire `journal.md` in as a cross-check of `trade_history`.
* Sparkline of VIX/VXV over the session in the status bar.
* Theme toggle that actually persists (localStorage) rather than relying on
  config.toml.

---

## 9. Gotchas for whoever edits this

* **Don't add a broad `span { color }` or `[class*="css"] { … }` rule** — it
  breaks the Material icon font and/or Altair tooltips. Scope everything.
* **`parse_debates` prefers `audit_md`** — pass `activity_text` only when the MD
  is empty, or you'll get the log's debates (no outcome labels) instead.
* The `_last_scan_block` / `_last_postmortem` helpers are **regex-anchored to the
  log's exact framing** (`\n=+\n`, timestamp prefix). If the agent's log format
  changes, these break silently (return `""`). The tests use realistic fixtures
  — extend them if you touch the log format.
* `trade_history` de-dupe assumes history is chronological. It is (the agent
  appends). Don't sort the input.
* `account_summary` P&L is `closing_equity − starting_equity`; `closing_equity`
  comes from `REPORTS/audit_snapshot.json` (`equity`), falling back to
  `starting_equity` if the snapshot is missing (→ shows 0% P&L, not a crash).
* Cache: `load_all` is `@st.cache_data` keyed on a tuple of file mtimes — edits
  to the source files auto-refresh; edits to `audit_data.py` need a rerun.
* Regenerate the snapshot with the `uv run python - <<PY … PY` block in
  `audit/README.md` (needs `.env` with the paper keys).
