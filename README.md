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

## Alpaca infrastructure

The agent reads and trades through Alpaca over two paths.

**Trade path — `alpaca-py` only.** Order submission, the risk gates, position
management and the hard-stop flatten all go through `alpaca-py`'s
`TradingClient` (`executor.py`, `main.AlpacaConnection`). This path has no
optional dependencies and never routes through MCP, so nothing about the
execution of a trade can be affected by an auxiliary service being down.

**Read path — Alpaca MCP server, with `alpaca-py` as the fallback.** At startup
the agent opens a stdio MCP session against Alpaca's official server:

```
uv run --directory C:/alpaca-hackathon/alpaca-mcp-server alpaca-mcp-server
```

Two per-cycle reads are served over it when the session is up:

| read | MCP tool | fallback |
|------|----------|----------|
| account snapshot (equity, day-start equity) | `get_account_info` | `TradingClient.get_account()` |
| per-symbol headlines for the risk officer | `get_news` | `alpaca_trader.fetch_recent_news()` |

The MCP path is an enhancement, never a dependency. A missing `mcp` package, a
server that will not start, a handshake timeout, a failing tool call, or a
payload the agent cannot parse each degrade silently to `alpaca-py`. Every call
logs which route served it (`account served by mcp` / `news served by
alpaca-py`), and startup logs one of:

```
MCP session connected — N tool(s) available
Alpaca read path — MCP session active (tools: ...); alpaca-py is the fallback
Alpaca read path — MCP session unavailable — every call served by alpaca-py
```

Configuration:

| variable | default | meaning |
|----------|---------|---------|
| `AGENT_MCP` | `true` | set to `off`/`false` to skip MCP entirely |
| `AGENT_MCP_SERVER_DIR` | `C:/alpaca-hackathon/alpaca-mcp-server` | checkout the server runs from |

The MCP server needs its own Alpaca credentials in its own `.env`; it does not
inherit the agent's.
