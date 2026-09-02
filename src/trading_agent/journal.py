"""
journal.py — turn ``session.json``'s event history into a shareable trade journal.

    python -m trading_agent.journal            # -> journal.md + journal.csv
    python -m trading_agent.journal --session other.json --md a.md --csv b.csv

One row per trade: symbol, structure, legs, entry credit/debit, entry and exit
timestamps, exit reason, realized P&L, the gate values recorded at entry, and
the risk officer's verdict / thesis / provider. Trades still open show a blank
exit and ``open`` as the reason.

Read-only: it never touches the broker and never mutates the session file.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

CSV_FIELDS = [
    "id", "symbol", "structure", "regime", "quantity", "expiry",
    "entry_at", "entry_credit", "exit_at", "exit_reason", "pnl",
    "iv_regime_mode", "underlying_price", "iv_rv_spread", "credit_to_width",
    "max_loss_per_contract", "order_risk", "max_risk_allowed",
    "officer_verdict", "officer_provider", "officer_thesis", "legs", "detail",
]


def load_history(session_file: str = "session.json") -> list[dict]:
    p = Path(session_file)
    if not p.is_file():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("history", []))
    except (ValueError, OSError):
        return []


def _verdict(officer: dict) -> str:
    approved = officer.get("approved")
    if approved is True:
        return "APPROVE"
    if approved is False:
        return "VETO"
    return ""


def build_rows(history: list[dict]) -> list[dict]:
    """Pair each entry event with its close (by tracking id), oldest first.

    ``submitted`` is the entry of record — it carries the gate values and the
    officer verdict. A bare ``opened`` (a fill promoted without a prior
    ``submitted``, e.g. an adopted position) still produces a row.
    """
    # A trade ends either with a real close (P&L) or with a terminal order event
    # (never filled / reconciled away). A real close always wins.
    TERMINAL = {
        "order_stale_cancelled": "cancelled-unfilled",
        "order_abandoned": "abandoned-unfilled",
        "position_dropped": "dropped-phantom",
    }
    closes = {}
    for e in history:
        kind = e.get("kind")
        if kind in TERMINAL and e.get("id") not in closes:
            closes[e.get("id")] = {**e, "reason": TERMINAL[kind], "pnl": ""}
    for e in history:
        if e.get("kind") == "closed":
            closes[e.get("id")] = e
    entries: dict[str, dict] = {}
    order: list[str] = []
    for e in history:
        if e.get("kind") not in ("submitted", "opened"):
            continue
        tid = e.get("id")
        if tid is None:
            continue
        if tid not in entries:
            order.append(tid)
            entries[tid] = e
        elif e.get("kind") == "submitted":
            merged = dict(entries[tid])
            merged.update(e)
            entries[tid] = merged

    rows: list[dict] = []
    for tid in order:
        e = entries[tid]
        gates = e.get("gates") or {}
        officer = e.get("officer") or {}
        c = closes.get(tid)
        thesis = (officer.get("thesis") or "").replace("\n", " ").strip()
        rows.append({
            "id": tid,
            "symbol": e.get("symbol", ""),
            "structure": e.get("structure", ""),
            "regime": e.get("regime") or "",
            "quantity": e.get("quantity", ""),
            "expiry": e.get("expiry") or "",
            "entry_at": e.get("at", ""),
            "entry_credit": e.get("entry_credit", ""),
            "exit_at": c.get("at", "") if c else "",
            "exit_reason": c.get("reason", "") if c else "open",
            "pnl": c.get("pnl", "") if c else "",
            "iv_regime_mode": gates.get("iv_regime_mode") or "",
            "underlying_price": gates.get("underlying_price", ""),
            "iv_rv_spread": gates.get("iv_rv_spread", ""),
            "credit_to_width": gates.get("credit_to_width", ""),
            "max_loss_per_contract": gates.get("max_loss_per_contract", ""),
            "order_risk": gates.get("order_risk", ""),
            "max_risk_allowed": gates.get("max_risk_allowed", ""),
            "officer_verdict": _verdict(officer),
            "officer_provider": officer.get("provider") or "",
            "officer_thesis": thesis,
            "legs": " ".join(e.get("legs") or []),
            "detail": e.get("detail", ""),
        })
    rows.sort(key=lambda r: r["entry_at"])
    return rows


def _num(x, spec=".2f") -> str:
    try:
        return format(float(x), spec)
    except (TypeError, ValueError):
        return "—"


def _pnl_str(value) -> str:
    try:
        return f"${float(value):+,.0f}"
    except (TypeError, ValueError):
        return "—"


def render_markdown(rows: list[dict]) -> str:
    out = ["# Trade Journal", ""]
    if not rows:
        out += ["_No trades recorded yet._", ""]
        return "\n".join(out)

    closed = [r for r in rows if r["exit_reason"] != "open"]
    realized = 0.0
    for r in closed:
        try:
            realized += float(r["pnl"])
        except (TypeError, ValueError):
            pass

    out += [
        f"**{len(rows)} trade(s)** — {len(closed)} closed, "
        f"{len(rows) - len(closed)} open. "
        f"Realized P&L **${realized:+,.2f}**.",
        "",
        "| # | Symbol | Structure | Qty | Entry | Credit | Exit | Reason | P&L | Officer |",
        "|---|--------|-----------|-----|-------|--------|------|--------|-----|---------|",
    ]
    for i, r in enumerate(rows, 1):
        exit_at = r["exit_at"][:16] if r["exit_at"] else "—"
        out.append(
            f"| {i} | {r['symbol']} | {r['structure']} | {r['quantity']} | "
            f"{r['entry_at'][:16]} | {_num(r['entry_credit'])} | "
            f"{exit_at} | {r['exit_reason']} | {_pnl_str(r['pnl'])} | "
            f"{r['officer_verdict'] or '—'} |"
        )

    out += ["", "## Detail", ""]
    for i, r in enumerate(rows, 1):
        exit_line = f"- **Exit:** {r['exit_at'] or '— still open'} — {r['exit_reason']}"
        if r["pnl"] not in ("", None):
            exit_line += f" — P&L {_pnl_str(r['pnl'])}"
        out += [
            f"### {i}. {r['symbol']} — {r['structure']}  (`{str(r['id'])[:8]}`)",
            "",
            f"- **Regime:** {r['regime'] or '—'}",
            f"- **Legs:** `{r['legs'] or '—'}`  (expiry {r['expiry'] or '—'})",
            f"- **Entry:** {r['entry_at']} — credit/debit {_num(r['entry_credit'])} "
            f"x{r['quantity']}",
            exit_line,
            f"- **Gates at entry:** IV-RV {_num(r['iv_rv_spread'], '+.4f')}, "
            f"credit/width {_num(r['credit_to_width'], '.2%')}, "
            f"order risk ${_num(r['order_risk'], ',.0f')} of "
            f"${_num(r['max_risk_allowed'], ',.0f')} allowed",
            f"- **Officer:** {r['officer_verdict'] or '—'} "
            f"({r['officer_provider'] or 'n/a'}) — {r['officer_thesis'] or '—'}",
            "",
        ]
    return "\n".join(out)


def write_journal(
    *,
    session_file: str = "session.json",
    md_path: str = "journal.md",
    csv_path: str = "journal.csv",
) -> tuple[str, str]:
    rows = build_rows(load_history(session_file))
    Path(md_path).write_text(render_markdown(rows), encoding="utf-8")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    return md_path, csv_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build journal.md / journal.csv from session.json"
    )
    ap.add_argument("--session", default="session.json")
    ap.add_argument("--md", default="journal.md")
    ap.add_argument("--csv", default="journal.csv")
    args = ap.parse_args(argv)
    md, csv_out = write_journal(
        session_file=args.session, md_path=args.md, csv_path=args.csv
    )
    n = len(build_rows(load_history(args.session)))
    print(f"wrote {md} and {csv_out} — {n} trade(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
