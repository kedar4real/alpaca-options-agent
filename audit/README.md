# The Volatility Arbiter — Post-Competition Audit Dashboard

A **read-only** retrospective evidence viewer for hackathon judges. It shows what
the agent did, and — the point of the whole exercise — what it *declined* to do.

Nothing here can place, cancel, or modify an order. The "Emergency Flatten"
button is permanently disabled; the account is presented as **STOPPED / FLAT**.

## Run

```bash
# from the repo root (C:\alpaca-hackathon\trading-agent)
uv pip install -r audit/requirements.txt      # one-time: streamlit + altair + pandas
uv run streamlit run audit/dashboard.py       # opens http://localhost:8501
```

`streamlit` pins `websockets<17`; the agent's lock uses `17.1`. It runs fine on
`17.1` in practice, so no separate venv is needed — but if `uv sync` ever forces
the downgrade, install streamlit in its own venv instead.

## What it reads (all on-disk, no network)

| Source | Used for |
|---|---|
| `session.json` | starting equity, account id, trade history, still-open positions |
| `logs/agent_activity.log` | VIX term structure, per-ticker RSI/IV, SCAN-TABLE no-trade log, nightly post-mortem counts, RUN MODE banner |
| `REPORTS/FINAL_SESSION_AUDIT.md` | full Bull / Bear / Judge debate transcripts |
| `REPORTS/audit_snapshot.json` | frozen closing equity + position legs (regenerate with the snippet below) |

Point it elsewhere with `AUDIT_ROOT=/path/to/repo`.

### Regenerating the closing snapshot

```bash
uv run python - <<'PY'
from dotenv import load_dotenv; load_dotenv(".env")
import os, json, datetime
from alpaca.trading.client import TradingClient
tc = TradingClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
a, pos = tc.get_account(), tc.get_all_positions()
json.dump({
    "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "account_id": a.account_number, "equity": float(a.equity),
    "last_equity": float(a.last_equity), "cash": float(a.cash),
    "position_legs": [{"symbol": p.symbol, "qty": int(p.qty),
                       "market_value": float(p.market_value),
                       "unrealized_pl": float(p.unrealized_pl)} for p in pos],
}, open("REPORTS/audit_snapshot.json", "w"), indent=2)
PY
```

## Layout

* **Status bar** — agent id, live VIX / VXV / term state, `STOPPED / FLAT`.
* **Sidebar** — status, equity metrics, the hard invariants ($95k floor, 1.5% cap,
  defined-risk-only), disabled Emergency Flatten.
* **Col 1 · Market Context** — VIX vs VXV term-structure chart (amber when inverted)
  + SPY/QQQ/IWM RSI & IV table.
* **Col 2 · The Logic** — Bull/Bear/Judge transcripts as chat messages; the last
  20 "No-Trade" decisions.
* **Col 3 · The Evidence** — trade history with per-trade risk vs cap; the Atomic
  MLEG execution-invariant note.
* **The Gate is the Hero** — funnel bar chart (Gate vetoes vs AI vetoes vs
  approved) + regime mix.
* **Incident Response** — before/after diff for commit `b83438d` (atomic close).

## Tests

```bash
uv run pytest audit/tests/ -q
```

The parsers in `audit_data.py` are pure and fully unit-tested; `dashboard.py` is
the Streamlit presentation layer only.
