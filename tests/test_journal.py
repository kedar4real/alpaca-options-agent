"""Offline tests for journal.py — build journal.md / journal.csv from session history."""

from __future__ import annotations

import csv
import json

from trading_agent import journal


def _session(tmp_path, history):
    p = tmp_path / "session.json"
    p.write_text(json.dumps({
        "starting_equity": 100_000.0, "account_id": "PA_TEST",
        "open_condors": [], "pending_orders": [], "history": history,
    }), encoding="utf-8")
    return str(p)


SUBMITTED = {
    "kind": "submitted", "at": "2026-09-02T12:00:00-04:00", "id": "ord-1",
    "symbol": "GLD", "structure": "long_strangle", "regime": "Regime B",
    "detail": "8x GLD long_strangle exp 2026-09-04 debit $2.45",
    "quantity": 8, "entry_credit": -2.45, "expiry": "2026-09-04",
    "legs": ["GLD260904P00395000", "GLD260904C00406000"],
    "gates": {"iv_rv_spread": -0.024, "credit_to_width": None,
              "order_risk": 1960.0, "max_risk_allowed": 1992.0},
    "officer": {"provider": "featherless", "approved": True,
                "thesis": "Long vol into the NFP catalyst is warranted."},
}
CLOSED = {
    "kind": "closed", "at": "2026-09-04T10:05:00-04:00", "id": "ord-1",
    "symbol": "GLD", "structure": "long_strangle", "reason": "profit-target",
    "pnl": 611.25, "quantity": 8,
}


def test_build_rows_pairs_a_submitted_event_with_its_close(tmp_path) -> None:
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [SUBMITTED, CLOSED])))
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "ord-1" and r["symbol"] == "GLD"
    assert r["structure"] == "long_strangle" and r["quantity"] == 8
    assert r["entry_credit"] == -2.45
    assert r["entry_at"].startswith("2026-09-02")
    assert r["exit_at"].startswith("2026-09-04")
    assert r["exit_reason"] == "profit-target"
    assert r["pnl"] == 611.25
    assert r["officer_verdict"] == "APPROVE"
    assert r["officer_provider"] == "featherless"
    assert "NFP catalyst" in r["officer_thesis"]
    assert r["iv_rv_spread"] == -0.024
    assert r["order_risk"] == 1960.0
    assert "GLD260904P00395000" in r["legs"]


def test_build_rows_keeps_an_open_trade_with_a_blank_exit(tmp_path) -> None:
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [SUBMITTED])))
    assert len(rows) == 1
    assert rows[0]["exit_at"] == "" and rows[0]["pnl"] == ""
    assert rows[0]["exit_reason"] == "open"


def test_build_rows_is_chronological(tmp_path) -> None:
    later = dict(SUBMITTED, id="ord-2", symbol="SPY", at="2026-09-03T09:40:00-04:00")
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [later, SUBMITTED])))
    assert [r["id"] for r in rows] == ["ord-1", "ord-2"]


def test_write_journal_emits_markdown_and_csv(tmp_path) -> None:
    sess = _session(tmp_path, [SUBMITTED, CLOSED])
    md, csv_path = journal.write_journal(
        session_file=sess, md_path=str(tmp_path / "journal.md"),
        csv_path=str(tmp_path / "journal.csv"),
    )
    text = open(md, encoding="utf-8").read()
    assert "# Trade Journal" in text and "GLD" in text and "profit-target" in text
    assert "featherless" in text

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "GLD" and rows[0]["pnl"] == "611.25"
    for col in journal.CSV_FIELDS:
        assert col in rows[0]


def test_write_journal_on_an_empty_history_still_writes_both_files(tmp_path) -> None:
    sess = _session(tmp_path, [])
    md, csv_path = journal.write_journal(
        session_file=sess, md_path=str(tmp_path / "j.md"), csv_path=str(tmp_path / "j.csv"),
    )
    assert "no trades recorded" in open(md, encoding="utf-8").read().lower()
    assert open(csv_path, encoding="utf-8").read().strip().startswith("id,")


def test_a_cancelled_or_abandoned_order_is_terminal_not_open(tmp_path) -> None:
    cancelled = {"kind": "order_stale_cancelled", "at": "2026-09-02T12:10:00-04:00",
                 "id": "ord-1", "symbol": "GLD", "cycles": 2}
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [SUBMITTED, cancelled])))
    assert rows[0]["exit_reason"] == "cancelled-unfilled"
    assert rows[0]["exit_at"].startswith("2026-09-02T12:10")
    assert rows[0]["pnl"] == ""


def test_an_abandoned_order_and_a_dropped_phantom_are_labelled(tmp_path) -> None:
    abandoned = {"kind": "order_abandoned", "at": "2026-09-02T12:10:00-04:00",
                 "id": "ord-1", "symbol": "GLD", "reason": "canceled"}
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [SUBMITTED, abandoned])))
    assert rows[0]["exit_reason"] == "abandoned-unfilled"

    dropped = {"kind": "position_dropped", "at": "2026-09-02T12:20:00-04:00",
               "id": "ord-1", "symbol": "GLD", "reason": "broker holds none of its legs"}
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [SUBMITTED, dropped])))
    assert rows[0]["exit_reason"] == "dropped-phantom"


def test_a_real_close_wins_over_a_terminal_order_event(tmp_path) -> None:
    stale = {"kind": "order_stale_cancelled", "at": "2026-09-02T12:10:00-04:00", "id": "ord-1"}
    rows = journal.build_rows(journal.load_history(_session(tmp_path, [SUBMITTED, stale, CLOSED])))
    assert rows[0]["exit_reason"] == "profit-target" and rows[0]["pnl"] == 611.25
