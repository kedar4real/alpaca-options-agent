# trading-agent

Autonomous SPY options trading agent for the Alpaca x LabLab.ai hackathon.
Defined-risk iron condors on a $100k Alpaca paper account.

## Setup

```
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Put `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `ALPACA_PAPER_TRADE` in a repo-root
`.env` (git-ignored).

## Layout

`src/trading_agent/` — `alpaca_trader` (Alpaca data primitives) → `data`
(market snapshot + IV regime) → `strategy` (iron condor builder) →
`risk_manager` (pre-trade gates) → `executor` (gated MLEG submission).

Run a module: `python -m trading_agent.<name>`. Tests: `pytest tests/`.

See `PROJECT_STATE.md` for the current architecture and `DEVLOG.md` for history.
