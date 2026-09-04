"""Pure parsers for the post-competition audit dashboard.

No Streamlit, no network — every function takes a string or a dict and returns
plain data, so the whole surface is unit-tested in ``tests/test_audit_data.py``.

Data sources
------------
* ``session.json``            — starting equity, account id, open positions, trade history
* ``logs/agent_activity.log`` — MARKET CONTEXT / DEBATE / SCAN TABLE / NIGHTLY POST-MORTEM blocks
* ``REPORTS/FINAL_SESSION_AUDIT.md`` — full Bull / Bear / Judge transcripts (final session)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# The "cage" — hard invariants the agent lived inside (shown, never computed).
# --------------------------------------------------------------------------- #
SAFETY_FLOOR_USD = 95_000          # absolute equity floor: panic-flatten + halt
PER_TRADE_CAP_PCT = 1.5            # max defined loss per trade, % of live equity

_VETO = ("skipped", "blocked", "vetoed")


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def load_text(path) -> str:
    """File contents, or ``""`` when the file is missing / unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return ""


def load_session(path="session.json") -> dict:
    """Parsed ``session.json``, or ``{}`` when missing / invalid."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_json(path) -> dict:
    """Parsed JSON object at ``path``, or ``{}``."""
    return load_session(path)


# --------------------------------------------------------------------------- #
# market context / VIX term structure
# --------------------------------------------------------------------------- #
_VIX_RE = re.compile(
    r"VIX:\s*VIX\s+([\d.]+)\s*/\s*VXV\s+([\d.]+)\s*\(ratio\s+([\d.]+),\s*([A-Za-z]+)\)"
)
_CTX_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*MARKET CONTEXT\s*$")


def last_market_context(activity_text: str) -> dict | None:
    """The most recent ``MARKET CONTEXT`` block's VIX term structure.

    Returns ``{vix, vxv, ratio, state, ts, macro_line}`` or ``None``.
    """
    ts = None
    found = None
    for line in activity_text.splitlines():
        m_ts = _CTX_TS_RE.match(line.strip())
        if m_ts:
            ts = m_ts.group(1)
            continue
        m = _VIX_RE.search(line)
        if m:
            found = {
                "vix": float(m.group(1)),
                "vxv": float(m.group(2)),
                "ratio": float(m.group(3)),
                "state": m.group(4).lower(),
                "ts": ts,
                "macro_line": line.strip(),
            }
    return found


_RSI_RE = re.compile(r"RSI\s+([A-Z]{1,6}):\s+([\d.]+)\s+\(([a-zA-Z ]+)\)")
_SCAN_ROW_RE = re.compile(
    r"^\s+([A-Z]{1,6})\s+px\s+([\d.]+)\s+IV\s+([\d.]+)\s+RV\s+([\d.]+)\s+"
    r"IVRV\s+([+\-][\d.]+)\s+\S+\s+ER\s+([\d.]+)"
)


def _last_scan_block(activity_text: str) -> str:
    idx = activity_text.rfind("SCAN TABLE")
    if idx == -1:
        return ""
    tail = activity_text[idx:]
    end = re.search(r"\n=+\n|\n\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", tail)
    return tail[: end.start()] if end else tail


def ticker_metrics(activity_text: str, symbols) -> list[dict]:
    """One row per requested symbol, merging the newest RSI (MARKET CONTEXT)
    with the newest price / IV / RV / ER (SCAN TABLE)."""
    ctx = last_market_context(activity_text)
    rsi: dict[str, tuple[float, str]] = {}
    if ctx:
        for m in _RSI_RE.finditer(ctx["macro_line"]):
            rsi[m.group(1)] = (float(m.group(2)), m.group(3).strip())

    scan: dict[str, dict] = {}
    for line in _last_scan_block(activity_text).splitlines():
        m = _SCAN_ROW_RE.match(line)
        if m:
            scan[m.group(1)] = {
                "price": float(m.group(2)),
                "iv": float(m.group(3)),
                "rv": float(m.group(4)),
                "ivrv": float(m.group(5)),
                "er": float(m.group(6)),
            }

    rows = []
    for sym in symbols:
        row = {"symbol": sym, "price": None, "iv": None, "rv": None,
               "ivrv": None, "er": None, "rsi": None, "rsi_label": None}
        row.update(scan.get(sym, {}))
        if sym in rsi:
            row["rsi"], row["rsi_label"] = rsi[sym]
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# multi-agent debates
# --------------------------------------------------------------------------- #
_ROLE_RE = re.compile(
    r"^---\s*(BULL|BEAR|JUDGE)\s*(?:\(([^)]+)\))?\s*---\s*$", re.MULTILINE
)


def _parse_rounds(block: str) -> list[dict]:
    """Bull / Bear / Judge rows from one transcript body. Corrupt bodies
    (no ``--- ROLE ---`` markers) yield ``[]``."""
    marks = list(_ROLE_RE.finditer(block))
    rounds = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        seg = block[m.end():end]
        vm = re.search(r"VERDICT:\s*(APPROVE|VETO)", seg, re.IGNORECASE)
        tm = re.search(r"THESIS:\s*(.+)", seg, re.DOTALL)
        thesis = " ".join(tm.group(1).split())[:1200] if tm else ""
        rounds.append({
            "role": m.group(1),
            "provider": (m.group(2) or "").strip() or None,
            "verdict": vm.group(1).upper() if vm else None,
            "thesis": thesis,
        })
    return rounds


def _outcome(text: str) -> str:
    low = text.lower()
    if "executed at" in low or "submitted order" in low:
        return "executed"
    if "vetoed at" in low or "veto" in low:
        return "vetoed"
    if "blocked at" in low or "rejected" in low:
        return "blocked"
    return "debated"


_MD_SECTION_RE = re.compile(r"^##\s+([A-Z]{1,6})\s+[—\-]\s+(.+?)\s*(?:ET)?\s*$", re.MULTILINE)
_ACT_DEBATE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*DEBATE \[([A-Z]{1,6})\]\s*$", re.MULTILINE
)


def parse_debates(*, audit_md: str = "", activity_text: str = "") -> list[dict]:
    """Bull/Bear/Judge transcripts, newest last. Prefers the markdown audit
    file; falls back to ``DEBATE [SYM]`` blocks in the activity log."""
    debates: list[dict] = []

    if audit_md.strip():
        secs = list(_MD_SECTION_RE.finditer(audit_md))
        for i, m in enumerate(secs):
            end = secs[i + 1].start() if i + 1 < len(secs) else len(audit_md)
            body = audit_md[m.end():end]
            fence = re.search(r"```(.*?)```", body, re.DOTALL)
            outcome_m = re.search(r"\*\*Pipeline outcome:\*\*\s*(.+)", body)
            debates.append({
                "symbol": m.group(1),
                "when": m.group(2).strip(),
                "outcome": _outcome(outcome_m.group(1) if outcome_m else ""),
                "pipeline": " ".join(outcome_m.group(1).split()) if outcome_m else "",
                "rounds": _parse_rounds(fence.group(1)) if fence else [],
            })
        return debates

    marks = list(_ACT_DEBATE_RE.finditer(activity_text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(activity_text)
        body = activity_text[m.end():end]
        debates.append({
            "symbol": m.group(2),
            "when": m.group(1),
            "outcome": "debated",
            "pipeline": "",
            "rounds": _parse_rounds(body),
        })
    return debates


# --------------------------------------------------------------------------- #
# no-trade decision log
# --------------------------------------------------------------------------- #
_DECISION_RE = re.compile(
    r"^\s+([A-Z]{1,6})\s+px\s+([\d.]+)\s+IV\s+([\d.]+)\s+RV\s+([\d.]+)\s+"
    r"IVRV\s+([+\-][\d.]+)\s+\S+\s+ER\s+([\d.]+).*?->\s+(\w+)\s+\[(\w+)\]\s+(.+?)\s*$"
)


def no_trade_decisions(activity_text: str, limit: int = 20) -> list[dict]:
    """Every SCAN-TABLE row that did NOT execute, newest first."""
    rows = []
    for line in activity_text.splitlines():
        m = _DECISION_RE.match(line)
        if not m or m.group(7).lower() not in _VETO:
            continue
        rows.append({
            "symbol": m.group(1),
            "price": float(m.group(2)),
            "iv": float(m.group(3)),
            "rv": float(m.group(4)),
            "ivrv": float(m.group(5)),
            "er": float(m.group(6)),
            "decision": m.group(7).lower(),
            "stage": m.group(8),
            "reason": m.group(9).strip(),
        })
    rows.reverse()
    return rows[:limit]


# --------------------------------------------------------------------------- #
# "the Gate is the Hero" — veto ratio + regime mix
# --------------------------------------------------------------------------- #
def _last_postmortem(activity_text: str) -> str:
    idx = activity_text.lower().rfind("nightly post-mortem")
    if idx == -1:
        return ""
    tail = activity_text[idx:]
    end = re.search(r"\n=+\n\d{4}-\d\d-\d\d", tail)
    return tail[: end.start()] if end else tail


def veto_ratio(activity_text: str) -> dict:
    """Counts from the last NIGHTLY POST-MORTEM, or ``{}`` when there is none."""
    pm = _last_postmortem(activity_text)
    if not pm:
        return {}

    def num(label: str) -> int | None:
        m = re.search(rf"{re.escape(label)}\s*:?\s+(\d+)", pm)
        return int(m.group(1)) if m else None

    return {
        "scans": num("Ticker scans today"),
        "proposed": num("Trades proposed"),
        "approved": num("Trades approved"),
        "gate_vetoes": num("Vetoed by risk_manager"),
        "ai_vetoes": num("Vetoed by risk_officer"),
        "open_positions": num("Open positions"),
    }


def regime_breakdown(activity_text: str) -> list[tuple[str, int]]:
    """``[(label, count), ...]`` from the last post-mortem's Regime breakdown."""
    pm = _last_postmortem(activity_text)
    if "Regime breakdown" not in pm:
        return []
    tail = pm.split("Regime breakdown", 1)[1]
    out = []
    for line in tail.splitlines():
        m = re.match(r"\s*-\s*(.+?):\s*(\d+)\s*$", line)
        if m:
            out.append((m.group(1).strip(), int(m.group(2))))
    return out


# --------------------------------------------------------------------------- #
# trade history
# --------------------------------------------------------------------------- #
_KEEP_KINDS = {"submitted", "opened", "closed", "cancelled", "cancelled-unfilled"}


def _width_from_detail(detail: str) -> float | None:
    m = re.search(r"width \$([\d.]+)", detail or "")
    return float(m.group(1)) if m else None


def _credit_from_detail(detail: str) -> float | None:
    m = re.search(r"credit \$([\d.]+)", detail or "")
    return float(m.group(1)) if m else None


def _qty_from_detail(detail: str) -> int | None:
    m = re.match(r"\s*(\d+)x", detail or "")
    return int(m.group(1)) if m else None


def trade_history(session: dict) -> list[dict]:
    """Normalised trade rows from ``session['history']`` (notes/reconciles dropped),
    de-duplicated by order id (latest lifecycle state wins)."""
    raw = [h for h in session.get("history", [])
           if h.get("kind") in _KEEP_KINDS and h.get("symbol")]

    best: dict[str, dict] = {}
    passthrough: list[dict] = []
    for h in raw:
        oid = h.get("id")
        row = _normalise_trade(h)
        if not oid:
            passthrough.append(row)
            continue
        cur = best.get(oid)
        if cur is None:
            best[oid] = row
        else:
            # history is chronological: a later 'opened'/'closed' overlays the
            # 'submitted' row, keeping gates/officer that only the first row had.
            for k, v in row.items():
                if v not in (None, "", []):
                    cur[k] = v

    rows = list(best.values()) + passthrough
    rows.sort(key=lambda r: r.get("when") or "")
    for r in rows:
        r.pop("_kind", None)
    return rows


def _normalise_trade(h: dict) -> dict:
    detail = h.get("detail", "") or ""
    gates = h.get("gates") or {}
    officer = h.get("officer") or {}
    return {
        "when": h.get("at"),
        "kind": h.get("kind"),
        "_kind": h.get("kind"),
        "symbol": h.get("symbol"),
        "structure": h.get("structure"),
        "qty": h.get("quantity") or _qty_from_detail(detail),
        "credit": h.get("entry_credit") if h.get("entry_credit") is not None
        else _credit_from_detail(detail),
        "width": h.get("wing_width") or _width_from_detail(detail),
        "expiry": h.get("expiry"),
        "regime": h.get("regime"),
        "iv_rv_spread": gates.get("iv_rv_spread"),
        "credit_to_width": gates.get("credit_to_width"),
        "order_risk": gates.get("order_risk"),
        "max_risk_allowed": gates.get("max_risk_allowed"),
        "officer_provider": officer.get("provider"),
        "officer_approved": officer.get("approved"),
        "officer_thesis": officer.get("thesis"),
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# account summary
# --------------------------------------------------------------------------- #
def account_summary(session: dict, closing_equity: float,
                    *, force_stopped_flat: bool = False) -> dict:
    """Headline numbers for the sidebar. ``force_stopped_flat`` presents the
    account as STOPPED / FLAT regardless of the live position count (this is a
    retrospective audit view — the agent's session is over)."""
    starting = float(session.get("starting_equity") or 0.0)
    current = float(closing_equity)
    pnl_abs = current - starting
    pnl_pct = (pnl_abs / starting * 100.0) if starting else 0.0
    open_positions = len(session.get("open_condors") or [])
    halted = bool(session.get("trading_halted"))

    if force_stopped_flat:
        status_label, is_flat = "STOPPED / FLAT", True
    elif open_positions == 0:
        status_label = "STOPPED / FLAT" if halted else "RUNNING — FLAT"
        is_flat = True
    else:
        status_label, is_flat = f"RUNNING — {open_positions} OPEN", False

    return {
        "account_id": session.get("account_id"),
        "starting": starting,
        "current": current,
        "pnl_abs": pnl_abs,
        "pnl_pct": pnl_pct,
        "open_positions": open_positions,
        "halted": halted,
        "status_label": status_label,
        "is_flat": is_flat,
    }


def open_positions_detail(session: dict) -> list[dict]:
    """Rows for any still-open structures recorded in ``session.json``."""
    out = []
    for c in session.get("open_condors") or []:
        out.append({
            "symbol": c.get("symbol"),
            "structure": c.get("structure"),
            "expiry": c.get("expiry"),
            "qty": c.get("quantity"),
            "entry_credit": c.get("entry_credit"),
            "peak_gain_fraction": c.get("peak_gain_fraction"),
            "legs": [lg.get("symbol") for lg in c.get("legs") or []],
        })
    return out


def latest_run_mode(activity_text: str) -> list[str]:
    """The bullet lines of the last ``RUN MODE`` banner (the agent's 'cage')."""
    idx = activity_text.rfind("RUN MODE")
    if idx == -1:
        return []
    tail = activity_text[idx:]
    end = tail.find("=" * 20 + "\n", tail.find("\n"))
    block = tail[:end] if end != -1 else tail[:2000]
    bullets = []
    for line in block.splitlines()[1:]:
        s = line.strip().lstrip("=").strip()
        if s and not set(s) <= {"="}:
            bullets.append(s)
    return bullets
