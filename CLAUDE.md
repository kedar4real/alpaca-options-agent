# Alpaca Options Trading Agent — System Instructions

## Project Context
Autonomous Options Trading Agent for the Alpaca x LabLab.ai Hackathon.
- Paper Account ID: Dedicated $100k account
- Core Framework: Python 3.12 (pinned — alpaca-py deps lack 3.14 wheels), alpaca-py, LangGraph, OpenAI/Featherless client
- Primary Pair: SPY (Defined-risk credit/debit spreads)

## Hard Safety Rules (DO NOT BREAK)
1. Never hardcode API keys or secrets in source code.
2. Max risk per trade must never exceed 1.5% of portfolio equity ($1,500).
3. Max drawdown stop: If portfolio drops below $95,000, all trading must halt.
4. All trades must be defined-risk spreads (no naked options selling).

## Development Workflow & Commands
- Layout: `src/trading_agent/` package (editable install); run from the repo root.
- Setup: `uv venv --python 3.12 && uv pip install -e ".[dev]"`
- Virtual environment: `.venv\Scripts\activate` (Windows) or `.venv/bin/activate`
- Run test suite: `pytest tests/`
- Run a module: `python -m trading_agent.<name>` (e.g. `trading_agent.strategy`)
- Check Alpaca status: `python -m trading_agent.alpaca_trader`

## State Sync Requirement
After writing or refactoring any code:
1. Update `PROJECT_STATE.md` with the new changes and architecture status.
2. Add a short entry to `DEVLOG.md` detailing what was built.
