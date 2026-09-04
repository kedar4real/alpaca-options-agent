"""
readers.py - pure parsers over the files the trading agent writes.

The dashboard is a strictly read-only observer. Everything here takes text (or
already-loaded plain data) and returns plain data; nothing opens a file for
writing, and nothing here can reach the broker. That keeps the dashboard
incapable of perturbing a live trading loop even if it has a bug.

The agent emits em-dashes in its log lines; test fixtures and some terminals
use plain hyphens. Every separator below accepts either.
"""

from __future__ import annotations

import csv
import io
import re

DASH = r"[—–-]"  # em dash, en dash, hyphen

# The pipeline a ticker walks, in order. A decision names the stage it died at.
PIPELINE_STAGES = ["precheck", "strategy", "risk_manager", "risk_officer", "executor"]

# Mirrors risk_manager.MAX_LONG_VOL_DEBIT_PCT. Kept as a default argument rather
# than imported so the dashboard never imports the agent package at runtime.
DEFAULT_MAX_LONG_VOL_PCT = 0.04


def _f(text: str | None) -> float | None:
    """Float, or None for blanks and junk - never raises."""
    if text is None:
        return None
    t = text.strip().replace(",", "")
    if not t or t in {"--", "-", "n/a"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# agent.log
# --------------------------------------------------------------------------- #
_TS = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

_DECISION_RE = re.compile(
    _TS + r".*?\[([A-Z]{1,6})\]\s+DECISION SUMMARY\s*" + DASH
    + r"\s*(\w+)\s+at\s+\[(\w+)\]:\s*(.*)"
)

_ORDER_RE = re.compile(
    _TS + r".*?\b(OPENED|SUBMITTED)\s+([A-Z]{1,6})\s+([0-9a-fA-F-]{36})\s+"
    r"\[(\w+)\]\s*" + DASH + r"\s*(.*)"
)


def parse_decisions(log_text: str, *, newest_first: bool = False) -> list[dict]:
    """Every per-ticker DECISION SUMMARY line, oldest first by default.

    Each row is ``{ts, symbol, outcome, stage, reason}`` - the outcome verb
    (Skipped/Blocked/Vetoed/Executed) and the pipeline stage that produced it.
    """
    rows = [
        {
            "ts": m.group(1),
            "symbol": m.group(2),
            "outcome": m.group(3),
            "stage": m.group(4),
            "reason": m.group(5).strip(),
        }
        for m in (_DECISION_RE.search(line) for line in log_text.splitlines())
        if m
    ]
    return rows[::-1] if newest_first else rows


def parse_orders(log_text: str, *, newest_first: bool = False) -> list[dict]:
    """OPENED / SUBMITTED order lines with their tracking id and structure."""
    rows = [
        {
            "ts": m.group(1),
            "event": m.group(2),
            "symbol": m.group(3),
            "order_id": m.group(4),
            "structure": m.group(5),
            "detail": m.group(6).strip(),
        }
        for m in (_ORDER_RE.search(line) for line in log_text.splitlines())
        if m
    ]
    return rows[::-1] if newest_first else rows


def decision_funnel(decisions: list[dict]) -> list[dict]:
    """How many tickers stopped at each pipeline stage, in pipeline order.

    Stages with no decisions are still present with a zero count, so the chart
    keeps a stable shape between cycles.
    """
    counts = {stage: 0 for stage in PIPELINE_STAGES}
    for d in decisions:
        stage = d.get("stage", "")
        if stage in counts:
            counts[stage] += 1
    return [{"stage": s, "count": counts[s]} for s in PIPELINE_STAGES]


# --------------------------------------------------------------------------- #
# agent_activity.log - the basket scan table
# --------------------------------------------------------------------------- #
_SCAN_HEADER_RE = re.compile(r"SCAN TABLE\s*\((.*?)\)")
_SCAN_ROW_RE = re.compile(
    r"^\s*([A-Z]{1,6})\s+px\s+([\d.]+)\s+"
    r"IV\s+([\d.]+)\s+RV\s+([\d.]+)\s+"
    r"IVRV\s+([+-][\d.]+)\s+(ok|FAIL)\s+"
    r"ER\s+([\d.]+)\s+(ok|FAIL)\s+"
    r"floor\s+(ok|FAIL)\s+"
    r"c/w\s+(\S+)"
)
_SCAN_OUTCOME_RE = re.compile(r"->\s*(\w+)\s*\[(\w+)\]\s*(.*)")


def parse_scan_table(activity_text: str) -> dict:
    """The most recent basket scan block.

    Returns ``{thresholds, rows}`` where each row carries the measured values,
    a pass/fail per gate, and the outcome/stage/reason that ended it.
    """
    starts = [m.start() for m in re.finditer(r"SCAN TABLE", activity_text)]
    if not starts:
        return {"thresholds": "", "rows": []}

    block = activity_text[starts[-1]:]
    header = _SCAN_HEADER_RE.search(block)

    rows = []
    for line in block.splitlines()[1:]:
        if line.startswith("====") and rows:
            break  # end of this block
        row = _SCAN_ROW_RE.match(line)
        if not row:
            continue
        outcome = _SCAN_OUTCOME_RE.search(line)
        rows.append({
            "symbol": row.group(1),
            "price": _f(row.group(2)),
            "iv": _f(row.group(3)),
            "rv": _f(row.group(4)),
            "iv_rv": _f(row.group(5)),
            "iv_rv_ok": row.group(6) == "ok",
            "er": _f(row.group(7)),
            "er_ok": row.group(8) == "ok",
            "floor_ok": row.group(9) == "ok",
            "credit_to_width": _f(row.group(10)),
            "outcome": outcome.group(1) if outcome else "",
            "stage": outcome.group(2) if outcome else "",
            "reason": outcome.group(3).strip() if outcome else "",
        })

    return {"thresholds": header.group(1).strip() if header else "", "rows": rows}


# --------------------------------------------------------------------------- #
# agent_activity.log - the macro / VIX / RSI / ADX context line
# --------------------------------------------------------------------------- #
_MACRO_RE = re.compile(r"Macro:\s*(.*)\((\d{4}-\d{2}-\d{2})\)")
_VIX_RE = re.compile(
    r"VIX\s+([\d.]+)\s*/\s*VXV\s+([\d.]+)\s*\(ratio\s+([\d.]+),\s*(\w+)\)"
)
_CLUSTER_RE = re.compile(r"\{([A-Z,\s]+)\}")
_RSI_RE = re.compile(r"RSI\s+([A-Z]{1,6}):\s*([\d.]+)\s*\(([^)]*)\)")
_ADX_RE = re.compile(r"ADX\s+([A-Z]{1,6}):\s*([\d.]+)\s*\(([^)]*)\)")
_NEWS_RE = re.compile(r"News\s+([A-Z]{1,6}):\s*(.*)")


def _symbol_slot(symbols: dict, sym: str) -> dict:
    return symbols.setdefault(
        sym, {"rsi": None, "rsi_label": "", "adx": None, "adx_label": "", "news": []}
    )


def parse_signals(activity_text: str) -> dict:
    """Macro event, VIX term structure, regime flags, correlation clusters and
    the per-symbol RSI / ADX / headlines from the context block.

    Every field degrades to ``None`` / empty rather than raising, so a partial
    or missing line still renders.
    """
    out: dict = {
        "macro_event": "", "macro_date": "",
        "vix": None, "vix3m": None, "vix_ratio": None, "vix_state": "",
        "regime_signals": [], "correlated": [], "symbols": {},
    }
    if not activity_text.strip():
        return out

    # Use the newest context line - the one carrying a VIX reading.
    lines = [ln for ln in activity_text.splitlines() if "VIX:" in ln or "REGIME SIGNALS" in ln]
    line = lines[-1] if lines else activity_text

    if m := _MACRO_RE.search(line):
        out["macro_event"] = m.group(1).strip()
        out["macro_date"] = m.group(2)
    if m := _VIX_RE.search(line):
        out["vix"] = _f(m.group(1))
        out["vix3m"] = _f(m.group(2))
        out["vix_ratio"] = _f(m.group(3))
        out["vix_state"] = m.group(4)

    for segment in line.split("|"):
        seg = segment.strip()
        if seg.startswith("REGIME SIGNALS:"):
            body = seg.split(":", 1)[1]
            out["regime_signals"] = [s.strip() for s in body.split(",") if s.strip()]
        elif seg.startswith("CORRELATED"):
            out["correlated"] = [
                [p.strip() for p in cluster.split(",") if p.strip()]
                for cluster in _CLUSTER_RE.findall(seg)
            ]
        elif m := _NEWS_RE.match(seg):
            slot = _symbol_slot(out["symbols"], m.group(1))
            slot["news"] = [h.strip() for h in m.group(2).split(";") if h.strip()]

    for m in _RSI_RE.finditer(line):
        slot = _symbol_slot(out["symbols"], m.group(1))
        slot["rsi"] = _f(m.group(2))
        slot["rsi_label"] = m.group(3).strip()
    for m in _ADX_RE.finditer(line):
        slot = _symbol_slot(out["symbols"], m.group(1))
        slot["adx"] = _f(m.group(2))
        slot["adx_label"] = m.group(3).strip()

    return out


# --------------------------------------------------------------------------- #
# session.json - concentration against the long-vol cap
# --------------------------------------------------------------------------- #
def long_vol_exposure(
    positions: list[dict],
    *,
    equity: float,
    max_pct: float = DEFAULT_MAX_LONG_VOL_PCT,
) -> dict:
    """Total premium paid across debit structures vs the ``max_pct`` cap.

    Only debit structures count - a credit structure (``entry_credit > 0``)
    collects premium rather than spending it, so it is outside this cap. This
    mirrors the gate in ``risk_manager`` that decides whether a new long-vol
    position may open.
    """
    debit = 0.0
    count = 0
    for p in positions:
        credit = float(p.get("entry_credit") or 0.0)
        if credit >= 0:
            continue
        debit += abs(credit) * float(p.get("quantity") or 0) * 100.0
        count += 1

    cap = equity * max_pct
    debit = round(debit, 2)
    return {
        "debit": debit,
        "cap": round(cap, 2),
        "pct": round(debit / equity, 6) if equity else 0.0,
        "max_pct": max_pct,
        "count": count,
        "headroom": round(max(0.0, cap - debit), 2),
        "breached": debit > cap,
    }


# --------------------------------------------------------------------------- #
# iv_history.csv
# --------------------------------------------------------------------------- #
def iv_series(csv_text: str, symbols: list[str] | None = None) -> dict[str, list[dict]]:
    """IV/RV history grouped by symbol, oldest first.

    Rows with no IV reading are dropped - the agent writes those when a quote
    was unavailable, and they would render as gaps in the chart.
    """
    if not csv_text.strip():
        return {}

    wanted = set(symbols) if symbols else None
    out: dict[str, list[dict]] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        sym = (row.get("symbol") or "").strip()
        iv = _f(row.get("iv"))
        if not sym or iv is None:
            continue
        if wanted and sym not in wanted:
            continue
        out.setdefault(sym, []).append({
            "t": (row.get("timestamp") or "").strip(),
            "iv": iv,
            "rv": _f(row.get("rv")),
            "spread": _f(row.get("spread")),
        })
    return out
