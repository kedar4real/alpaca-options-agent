# Risk gates — full specification

Source of truth: `src/trading_agent/risk_manager.py`. Pure and deterministic —
no network call, no wall clock (the caller passes `today` for date logic).
This module *decides*; it never places or cancels an order. `check_order()`
runs gates 1–5 against a single proposed order and collects **every** failing
gate rather than short-circuiting on the first, so a blocked trade explains
itself completely in one `RiskDecision`. Gate 6 runs separately, over the
whole open book.

All limit values below are the literal current constants in `risk_manager.py`
as of this session — quoted, not paraphrased from an older doc or a comment.

## Gate 1 — Max risk per trade

```python
MAX_RISK_PER_TRADE_PCT = 0.015     # 1.5% of current equity
```

`order_risk <= MAX_RISK_PER_TRADE_PCT * account.current_equity * macro_mult`
(`macro_mult` is gate-1-specific — see the macro guard below). `order_risk`
is `ProposedOrder.risk_dollars`:

- If the order carries an explicit `max_loss` (set by debit structures, e.g.
  a long strangle, where the credit-spread formula doesn't apply):
  `max_loss * quantity`.
- Otherwise, the classic credit-spread formula:
  `(wing_width - net_credit) * 100 * quantity`.

The cap is checked against *current* equity, not the equity the trade idea
was proposed against — a drawdown mid-cycle tightens the cap for every trade
still to be evaluated that cycle.

**Macro guard.** On a High-Impact macro day (FOMC / CPI / NFP inside 48h),
the caller sets `AccountState.risk_multiplier = 0.5`
(`macro_risk_multiplier(macro_high_impact=True)`), which gate 1 clamps to
`min(risk_multiplier, 1.0)` before multiplying — a multiplier can only
*tighten* the cap, never raise it above 1.5%. `MAX_RISK_PER_TRADE_PCT` itself
is never touched.

## Gate 2 — Daily loss halt

```python
DAILY_LOSS_HALT_PCT = 0.035        # 3.5% of starting equity
```

`daily_loss = account.day_start_equity - account.current_equity`. Blocks all
new trades for the day once `daily_loss >= DAILY_LOSS_HALT_PCT *
account.starting_equity`. `day_start_equity` resets each trading day;
`starting_equity` is the persisted, never-re-derived session baseline (see
"why `starting_equity` is sacred" below).

## Gate 3 — Total drawdown floor (sticky)

```python
TOTAL_DRAWDOWN_FLOOR_PCT = 0.05    # 5% of starting equity
```

`total_drawdown = account.starting_equity - account.current_equity`. Once
`total_drawdown >= TOTAL_DRAWDOWN_FLOOR_PCT * account.starting_equity`, the
run latches a **sticky** halt (`AccountState.trading_halted`) — even if
equity recovers above the floor on a later mark, the halt does not
self-clear. Every subsequent `check_order()` call short-circuits to blocked
for the rest of the run.

For this session (`starting_equity` $99,870.90 in `session.json`, or
$100,000.00 against the API-verified account baseline — see the README), the
floor sat at **$95,000** either way to a rounding difference. The account's
lowest equity mark was $96,868.97 — **$1,869 clear of the floor**, the floor
was never approached.

## Gate 4 — Max concurrent positions

```python
MAX_CONCURRENT_POSITIONS = 4
```

`len(account.open_positions) < MAX_CONCURRENT_POSITIONS`. Global across the
whole basket, not per-ticker — `main.py` assembles one shared `AccountState`
and rebuilds it after every fill so the cap is enforced across the entire
scan cycle, not just within one ticker's evaluation.

## Gate 4b — Correlation guard

Optional; only runs when the `IntelligenceHub` supplies
`correlation_clusters` (groups of basket tickers whose 10-day return
correlation exceeds 0.8). If the proposed order's `underlying` sits in a
cluster that already holds an open position, the trade is blocked — the
whole cluster gets **one** slot toward the gate-4 cap, not one each. Three
condors on SPY, QQQ and IWM is one leveraged bet on equity beta, not three
diversified ones.

## Gate 4c — Long-volatility concentration

```python
MAX_LONG_VOL_DEBIT_PCT = 0.04      # 4% of current equity
MAX_LONG_VOL_POSITIONS = 3
```

Only applies when the proposed order is itself a net debit (a long
strangle, `net_credit < 0`). Two independent checks against every other
open long-vol position (`entry_credit < 0`):

- **Count**: fewer than `MAX_LONG_VOL_POSITIONS` (3) already open.
- **Total premium**: `sum(open long-vol debit) + this order's debit <=
  MAX_LONG_VOL_DEBIT_PCT * current_equity` (4%).

Exists because the macro override (below) forces every new structure into a
long strangle during a danger window — without this cap, the book becomes N
correlated long-vol bets on the same catalyst instead of one sized position.
`DEVLOG.md` (2026-09-02) notes DIA/GLD/SLV/TLT/XLF/XLE/XLK/EEM blocking here
on a single day the macro flag was active across the whole 12-name universe.

## Gate 5 — Defined-risk invariant

`is_defined_risk(order.legs)` — `True` for either of two shapes:

- **All-long**: every leg is `buy`, quantity > 0 (a long strangle/straddle).
  The most that can be lost is the premium paid; inherently defined-risk,
  never naked short.
- **Matched legs**: for every option right (put/call) present, total bought
  contracts equal total sold contracts (an iron condor or vertical credit
  spread — every short leg is covered by a long one).

A single `sell` leg drops a position out of the all-long branch and it must
satisfy the matched-legs rule — there is no code path that can build or pass
a naked short.

## Gate 6 — Expiry auto-close

```python
EXPIRY_CLOSE_TRADING_DAYS = 1
```

Runs over the whole open book, separately from gates 1–5.
`flag_expiring_positions()` flags any position `<= 1` NYSE trading session
from expiry (holiday-aware via `pandas-market-calendars` — e.g. a Friday
position is flagged Thursday if the following Monday is a holiday, so the
count is real sessions, not calendar days). Flagged positions are
force-closed rather than held into expiration.

## Why `starting_equity` is sacred

`session.json`'s `starting_equity` is fetched from the live account **once**,
on the very first run, and every later run loads it and **never
re-derives** it. Re-deriving on restart would silently move the 5%
drawdown floor every time the process restarted — a floor that can drift is
not a floor.
