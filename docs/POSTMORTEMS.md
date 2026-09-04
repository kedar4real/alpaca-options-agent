# Incident postmortems

Four real incidents from the build, in the order they happened. Commit
hashes below were verified against the current `master` branch (`git
merge-base --is-ancestor <hash> master`) at the time this doc was written —
worth restating because this repo's entire commit history was rewritten
once already this session (an author-identity correction via
`git filter-branch`), which silently invalidated every hash previously
cited in `DEVLOG.md` and `audit/HANDOFF.md`. Don't trust a hash in this repo
without checking it's still reachable from `master`.

---

## 1 — The phantom-order overhaul (2026-09-02)

**Symptom.** One morning, `session.json` was tracking four open positions
the broker did not actually hold, three orders stuck unfilled, and the
agent stuck at its position cap doing nothing — capacity was full of
positions that didn't exist.

**Root cause.** A position was recorded as *open* the moment an order was
*submitted*, not when the broker confirmed it filled. An order that never
filled, or filled partially, still occupied a position slot forever.

**Fix.** Four commits, same root cause:

| Commit | Change |
|---|---|
| `b75a04a` | `PendingOrder` + `reconcile_pending_orders` — a trade is open only once the broker confirms `filled`; an order still unfilled after 2 cycles is cancelled and its slot released; pending orders count toward the position cap in the meantime |
| `55ad952` | `reconcile_open_book` — every cycle, the tracked book is rebuilt from `get_all_positions()` (broker truth): phantoms the broker doesn't hold are dropped, and a loose leg the agent doesn't recognise (one side of a strangle that filled alone) is adopted and handed to the expiry gate to clean up |
| `dd917f8` | Per-symbol dedup — a related bug surfaced in the same pass: a fill freeing a position slot let the ranker immediately reopen a *second* structure on the same ticker (a fill was interpreted as "slot free," and the very next line of the same cycle re-picked the same name). Now a ticker with a position *or* a working order is excluded before ranking. |
| `04665e8` | Gate 4c, long-vol concentration cap — added in the same pass as a related hardening: under the macro override, every new proposal is a long strangle, so without a count + premium cap the book becomes N correlated bets on one catalyst. |

**Verification.** Live check the same day: MCP connected (72 tools), a
12-name scan running clean, book held at 2 long-vol strangles (IWM 16x, SPY
8x) at 3.7% of equity against the 4% cap. 433 tests green at the time.

---

## 2 — The risk officer vetoed almost everything (2026-09-02)

**Symptom.** The LLM second-opinion gate (`risk_officer.py`) was vetoing
close to 100% of trades that cleared the deterministic risk gates —
functionally a second risk gate stacked on the first, not an independent
judgment.

**Root cause.** The original judge model (`Qwen2.5-7B-Instruct`) was
misreading two market-structure facts backwards:

- **VIX contango** (front-month below the 3-month) is the market's *normal*
  state — it read contango as a stress signal and vetoed on it.
- A **neutral RSI** (40–60) is the *ideal* condition for a range-bound
  premium seller, not a reason for caution — it read neutral as "no edge,
  veto."

**Fix.** `d77fe3a` — swapped the default judge model to
`Qwen/Qwen2.5-72B-Instruct` (non-gated, no HF OAuth needed, unlike Llama
70B), capped `max_tokens` to control cost/latency, and added an explicit
`### QUANT CLARIFICATION` block to the prompt stating both facts directly
rather than relying on the model to infer them.

**Verification.** First live cycle after the change: the officer APPROVED
and the agent executed real QQQ and IWM condors — a concrete before/after,
not just a lower veto rate in aggregate.

---

## 3 — The leg-by-leg close bug (2026-09-03)

**Symptom.** Closing a multi-leg spread one leg at a time occasionally left
the account holding a transient naked short: a partial fill on the first
leg, before the second leg's closing order cleared, meant the position was
briefly unbalanced. The broker then rejected the second leg's close,
leaving an orphaned position that needed manual cleanup.

**Root cause.** `manage_open_positions()` originally called
`TradingClient.close_position()` once per leg, in sequence — two (or four)
independent broker requests for what is conceptually one atomic unwind.

**Fix.** `fcafbbd` — not a patch, a structural redesign.
`executor.build_close_request()` now reverses every leg's side (sold-to-open
becomes buy-to-close, and vice versa) and submits all of them in a single
`OrderClass.MLEG` **market** order:

```python
def build_close_request(legs, quantity: int) -> MarketOrderRequest:
    """One reversing MLEG market order that flattens a multi-leg position
    atomically. [...] the broker fills or rejects the combo as a unit, so
    the unwind can never leave one leg done and another open — the
    transient naked-short state that gets a leg-by-leg close rejected.

    Market, not limit: a forced exit [...] has to actually fill; on a
    defined-risk structure the worst case is already the known max loss,
    so there is nothing to protect with a limit.
    """
```

The choice of a **market** order (not limit) is deliberate: every forced
exit this covers — profit target, stop loss, expiry, hard-stop flatten — is
already at a known, bounded max loss by construction (gate 5, the
defined-risk invariant), so there's no economic reason to risk a non-fill
by using a limit price during an unwind.

---

## 4 — The final hard-stop flatten needed a retry (2026-09-04, observed, not a code fix)

**What happened, from the agent's own log** (`agent.log`, wall-clock IST on
the host machine — subtract 9h30m for ET):

```
19:00:44  COMPETITION HARD STOP reached — flattening the book
19:00:59  HARD STOP FINAL SUMMARY: legs closed 3 (remaining: 1), equity $96,887.04
19:01:00  ERROR  HARD STOP: 1 position leg(s) did NOT close — retrying next cycle
19:02:00  COMPETITION HARD STOP reached — flattening the book
19:02:04  HARD STOP FINAL SUMMARY: legs closed 1 (remaining: 0), equity $96,868.97
```

The competition's ET wall-clock hard stop had already passed while the
market was closed; `run_forever`'s hard-stop check only runs while
`get_clock().is_open` is true, so the actual flatten didn't fire until the
next market open. When it did, the first pass closed 3 of 4 remaining legs
and one attempt failed outright (logged as an `ERROR`, not a silent drop);
the retry one cycle later succeeded, and the account confirmed flat.

**Why this isn't incident #3 again.** The close mechanism itself was
already the atomic MLEG order from the fix above — this was a single
rejected or unfilled *attempt* at an atomic close, not a partial multi-leg
fill leaving a naked position. The retry loop (already present in the
hard-stop logic for exactly this reason) did what it was built to do.

**Independent confirmation.** The final flat state — $96,868.97, 0 open
positions, 0 open orders — was cross-checked three ways: this log's own
`HARD STOP FINAL SUMMARY`, a background poller querying the live Alpaca API
every ~90 seconds through the market open, and a direct manual re-query of
`/v2/account` and `/v2/positions` afterward. All three agree exactly.
