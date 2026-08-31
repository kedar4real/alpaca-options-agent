"""Offline tests for main.py.

Two focal points the spec calls out:
  * position-management triggers — profit target / stop loss / expiry close
  * gate sequencing — strategy -> risk_manager -> risk_officer -> executor must
    run in that exact order, and a rejection at any stage skips the rest.

Plus session persistence (starting_equity is never re-derived on restart) and
the halt prechecks. No network anywhere.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

import trading_agent.main as agent
from trading_agent.main import (
    CondorValuation,
    Config,
    DecisionSummary,
    Session,
    TrackedCondor,
    decide_exit,
    evaluate_cycle_decision,
    evaluate_new_trade,
    halt_status,
    load_or_init_session,
    manage_open_positions,
    reconcile_account_state,
    update_sticky_halt,
    value_condor,
)
from trading_agent.risk_manager import AccountState, OrderLeg

CFG = Config()  # defaults: 0.50 profit target, 2.0x stop, 45s review timeout

CONDOR_LEGS = (
    OrderLeg("sell", "put", 3, "SPY260904P00760000"),
    OrderLeg("buy", "put", 3, "SPY260904P00755000"),
    OrderLeg("sell", "call", 3, "SPY260904C00775000"),
    OrderLeg("buy", "call", 3, "SPY260904C00780000"),
)


def tracked(cid="c1", credit=1.00, qty=3, expiry=date(2099, 1, 1)):
    return TrackedCondor(id=cid, expiry=expiry, quantity=qty,
                         entry_credit=credit, legs=CONDOR_LEGS, opened_at="2026-08-31T10:00")


def valuation(cost_to_close, *, credit=1.00, qty=3):
    return CondorValuation(tracked(credit=credit, qty=qty), cost_to_close)


def account(*, starting=100_000.0, current=100_000.0, day_start=None,
            positions=(), halted=False) -> AccountState:
    return AccountState(
        starting_equity=starting,
        current_equity=current,
        day_start_equity=current if day_start is None else day_start,
        open_positions=positions,
        trading_halted=halted,
    )


# ======================================================================= #
# Position management: value_condor
# ======================================================================= #
def test_value_condor_nets_sold_minus_bought_mids() -> None:
    mids = {
        "SPY260904P00760000": 1.20, "SPY260904P00755000": 0.40,
        "SPY260904C00775000": 1.10, "SPY260904C00780000": 0.35,
    }
    # (1.20 + 1.10) sold back  -  (0.40 + 0.35) bought back  = 1.55
    assert value_condor(CONDOR_LEGS, mids) == pytest.approx(1.55)


def test_value_condor_returns_none_when_a_leg_quote_is_missing() -> None:
    mids = {"SPY260904P00760000": 1.20, "SPY260904P00755000": 0.40}
    assert value_condor(CONDOR_LEGS, mids) is None


# ======================================================================= #
# Position management: decide_exit triggers + ordering
# ======================================================================= #
def test_profit_target_fires_at_half_the_credit() -> None:
    # entry 1.00, buy back for 0.50 -> captured 0.50 -> exactly the 50% target
    assert decide_exit(valuation(0.50), is_expiring=False) == "profit-target"


def test_profit_target_not_fired_just_under_target() -> None:
    assert decide_exit(valuation(0.51), is_expiring=False) is None


def test_stop_loss_fires_at_two_times_the_credit_lost() -> None:
    # entry 1.00, now costs 3.00 to close -> loss 2.00 = 2x credit -> stop
    assert decide_exit(valuation(3.00), is_expiring=False) == "stop-loss"


def test_stop_loss_not_fired_just_inside_the_stop() -> None:
    assert decide_exit(valuation(2.99), is_expiring=False) is None


def test_expiry_close_fires_when_flagged_and_no_pnl_trigger() -> None:
    assert decide_exit(valuation(1.00), is_expiring=True) == "expiry"


def test_no_trigger_when_healthy_and_not_expiring() -> None:
    assert decide_exit(valuation(0.90), is_expiring=False) is None


def test_profit_target_beats_expiry_when_both_true() -> None:
    assert decide_exit(valuation(0.20), is_expiring=True) == "profit-target"


def test_stop_loss_beats_expiry_when_both_true() -> None:
    assert decide_exit(valuation(3.50), is_expiring=True) == "stop-loss"


def test_decide_exit_respects_configurable_thresholds() -> None:
    v = valuation(0.75)  # captured 0.25 of a 1.00 credit
    assert decide_exit(v, is_expiring=False, profit_target_fraction=0.20) == "profit-target"
    assert decide_exit(v, is_expiring=False, profit_target_fraction=0.50) is None


# ======================================================================= #
# Position management: manage_open_positions (session mutation + IO hook)
# ======================================================================= #
def _session_with(*condors) -> Session:
    return Session(starting_equity=100_000.0, open_condors=list(condors))


def test_manage_closes_only_triggered_positions_and_records_history() -> None:
    winner = tracked("win", credit=1.00)     # will hit profit target
    holder = tracked("hold", credit=1.00)    # healthy, stays
    sess = _session_with(winner, holder)
    closed_calls: list[str] = []

    events = manage_open_positions(
        sess,
        [CondorValuation(winner, 0.30), CondorValuation(holder, 0.95)],
        expiring_ids=set(),
        close_fn=lambda c: closed_calls.append(c.id),
        config=CFG,
        now_iso="2026-08-31T15:30:00-04:00",
    )

    assert closed_calls == ["win"]
    assert [c.id for c in sess.open_condors] == ["hold"]
    assert len(events) == 1 and events[0]["reason"] == "profit-target"
    assert events[0]["id"] == "win"
    # P&L per spread = 1.00 - 0.30 = 0.70 ; * 3 spreads * 100 = 210
    assert events[0]["pnl"] == pytest.approx(210.0)
    assert sess.history[-1]["kind"] == "closed"


def test_manage_uses_expiring_ids_for_the_expiry_trigger() -> None:
    c = tracked("exp", credit=1.00)
    sess = _session_with(c)
    events = manage_open_positions(
        sess, [CondorValuation(c, 1.00)], expiring_ids={"exp"},
        close_fn=lambda _c: None, config=CFG, now_iso="2026-08-31T15:30:00-04:00",
    )
    assert events and events[0]["reason"] == "expiry"
    assert sess.open_condors == []


def test_manage_keeps_position_if_close_fn_raises() -> None:
    c = tracked("boom", credit=1.00)
    sess = _session_with(c)

    def bad_close(_c):
        raise RuntimeError("alpaca 500")

    events = manage_open_positions(
        sess, [CondorValuation(c, 0.10)], expiring_ids=set(),
        close_fn=bad_close, config=CFG, now_iso="2026-08-31T15:30:00-04:00",
    )
    assert events == []
    assert [x.id for x in sess.open_condors] == ["boom"]   # still tracked
    assert not sess.history


# ======================================================================= #
# Gate sequencing: strategy -> risk_manager -> risk_officer -> executor
# ======================================================================= #
def _leg(action, right, symbol):
    return SimpleNamespace(action=action, right=right, symbol=symbol,
                           quantity=3, contract=SimpleNamespace(symbol=symbol))


class PipelineSpies:
    def __init__(self, *, eligible=True, approved=True, vetoed=False,
                 submitted=True, submit_error=None):
        self.calls: list[str] = []
        self.seen_timeout = None
        self.plan = SimpleNamespace(
            eligible=eligible, reason="ok" if eligible else "IV gate blocked",
            expiry=date(2026, 9, 4), net_credit=1.2, wing_width=4.0,
            suggested_contracts=3,
            legs=[_leg("sell", "put", "P1"), _leg("buy", "put", "P2"),
                  _leg("sell", "call", "C1"), _leg("buy", "call", "C2")],
        )
        self.order = SimpleNamespace(
            legs=self.plan.legs, quantity=3, net_credit=1.2, wing_width=4.0,
        )
        self.decision = SimpleNamespace(approved=approved,
                                        blocks=[] if approved else ["daily loss limit"])
        self.review = SimpleNamespace(approved=not vetoed, provider="featherless",
                                      thesis="thin edge" if vetoed else "looks fine")
        self.result = SimpleNamespace(submitted=submitted, order_id="ord-9",
                                      error=submit_error, submitted_request=None, order=None)

    # injected hooks -------------------------------------------------- #
    def plan_fn(self, snapshot, today=None):
        self.calls.append("strategy")
        return self.plan

    def to_order_fn(self, plan):
        self.calls.append("to_order")
        return self.order

    def check_fn(self, order, account):
        self.calls.append("risk_manager")
        return self.decision

    def review_fn(self, order, snapshot, account, timeout=None):
        self.calls.append("risk_officer")
        self.seen_timeout = timeout
        return self.review

    def submit_fn(self, order, account):
        self.calls.append("executor")
        return self.result

    def run(self, **kw):
        return evaluate_new_trade(
            {"snap": True}, account(), config=CFG,
            plan_fn=self.plan_fn, to_order_fn=self.to_order_fn,
            check_fn=self.check_fn, review_fn=self.review_fn, submit_fn=self.submit_fn,
            **kw,
        )


def test_full_approval_runs_every_stage_in_exact_order() -> None:
    spies = PipelineSpies()
    summary = spies.run()
    assert spies.calls == ["strategy", "to_order", "risk_manager", "risk_officer", "executor"]
    assert summary.outcome == "executed"
    assert "ord-9" in summary.reason


def test_strategy_rejection_stops_before_everything_else() -> None:
    spies = PipelineSpies(eligible=False)
    summary = spies.run()
    assert spies.calls == ["strategy"]
    assert summary.stage == "strategy" and summary.outcome == "skipped"


def test_risk_manager_rejection_stops_before_officer_and_executor() -> None:
    spies = PipelineSpies(approved=False)
    summary = spies.run()
    assert spies.calls == ["strategy", "to_order", "risk_manager"]
    assert summary.stage == "risk_manager" and summary.outcome == "blocked"
    assert "daily loss limit" in summary.reason


def test_risk_officer_veto_stops_before_executor() -> None:
    spies = PipelineSpies(vetoed=True)
    summary = spies.run()
    assert spies.calls == ["strategy", "to_order", "risk_manager", "risk_officer"]
    assert summary.stage == "risk_officer" and summary.outcome == "vetoed"
    assert "thin edge" in summary.reason


def test_executor_non_submission_is_reported_as_error() -> None:
    spies = PipelineSpies(submitted=False, submit_error="alpaca 403")
    summary = spies.run()
    assert spies.calls[-1] == "executor"
    assert summary.outcome == "error" and "alpaca 403" in summary.reason


def test_review_fn_receives_the_configured_timeout() -> None:
    spies = PipelineSpies()
    spies.run()
    assert spies.seen_timeout == CFG.review_timeout_s == 45.0


# ---- prechecks in evaluate_cycle_decision ----------------------------- #
def test_precheck_halt_skips_the_pipeline_entirely() -> None:
    calls: list[str] = []
    summary = evaluate_cycle_decision(
        {}, account(current=90_000.0), config=CFG, call_log=calls,
        plan_fn=lambda *a, **k: calls.append("strategy"),
    )
    assert summary.outcome == "halted" and summary.evaluated is False
    assert calls == []   # strategy never ran


def test_precheck_capacity_skips_the_pipeline_entirely() -> None:
    calls: list[str] = []
    three = tuple(tracked(f"c{i}").as_open_position() for i in range(3))
    summary = evaluate_cycle_decision(
        {}, account(positions=three), config=CFG, call_log=calls,
        plan_fn=lambda *a, **k: calls.append("strategy"),
    )
    assert summary.outcome == "skipped" and "max positions" in summary.reason
    assert calls == []


def test_cycle_decision_runs_pipeline_when_clear() -> None:
    spies = PipelineSpies()
    summary = evaluate_cycle_decision(
        {"snap": 1}, account(), config=CFG,
        plan_fn=spies.plan_fn, to_order_fn=spies.to_order_fn, check_fn=spies.check_fn,
        review_fn=spies.review_fn, submit_fn=spies.submit_fn,
    )
    assert summary.outcome == "executed"
    assert spies.calls == ["strategy", "to_order", "risk_manager", "risk_officer", "executor"]


# ======================================================================= #
# Halt status + sticky latch
# ======================================================================= #
def test_halt_status_none_when_healthy() -> None:
    assert halt_status(account(current=99_000.0, day_start=99_500.0)) is None


def test_halt_status_flags_total_drawdown_floor() -> None:
    s = halt_status(account(starting=100_000.0, current=95_000.0))
    assert s and "total drawdown" in s


def test_halt_status_flags_daily_loss() -> None:
    s = halt_status(account(starting=100_000.0, current=97_400.0, day_start=100_000.0))
    assert s and "daily loss" in s


def test_halt_status_reports_sticky_flag_first() -> None:
    s = halt_status(account(current=100_000.0, halted=True))
    assert s and "sticky halt" in s


def test_update_sticky_halt_latches_and_signals_change() -> None:
    sess = Session(starting_equity=100_000.0)
    changed = update_sticky_halt(sess, account(starting=100_000.0, current=94_900.0))
    assert changed is True and sess.trading_halted is True
    # already latched -> no further change
    assert update_sticky_halt(sess, account(starting=100_000.0, current=94_900.0)) is False


def test_update_sticky_halt_noop_when_within_floor() -> None:
    sess = Session(starting_equity=100_000.0)
    assert update_sticky_halt(sess, account(current=96_000.0)) is False
    assert sess.trading_halted is False


# ======================================================================= #
# Session persistence — starting_equity is authoritative on restart
# ======================================================================= #
def test_first_run_seeds_starting_equity_from_live_equity(tmp_path) -> None:
    path = str(tmp_path / "session.json")
    sess = load_or_init_session(path, account_id="PA123", live_equity=100_000.0)
    assert sess.starting_equity == 100_000.0
    assert sess.account_id == "PA123"
    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["starting_equity"] == 100_000.0


def test_restart_keeps_persisted_starting_equity_even_if_equity_dropped(tmp_path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "starting_equity": 100_000.0, "account_id": "PA123",
        "trading_halted": False, "open_condors": [], "history": [],
    }))
    # equity has since fallen to 90k — must NOT become the new starting_equity
    sess = load_or_init_session(str(path), account_id="PA123", live_equity=90_000.0)
    assert sess.starting_equity == 100_000.0


def test_session_round_trips_tracked_condors(tmp_path) -> None:
    path = str(tmp_path / "session.json")
    original = Session(starting_equity=100_000.0, open_condors=[tracked("rt", credit=1.25)])
    agent.save_session(original, path)
    back = agent.load_session(path)
    assert back is not None
    assert back.starting_equity == 100_000.0
    c = back.open_condors[0]
    assert c.id == "rt" and c.entry_credit == 1.25 and c.expiry == date(2099, 1, 1)
    assert [lg.symbol for lg in c.legs] == [lg.symbol for lg in CONDOR_LEGS]


# ======================================================================= #
# reconcile_account_state
# ======================================================================= #
def test_reconcile_uses_session_starting_equity_not_live_equity() -> None:
    sess = Session(starting_equity=100_000.0, trading_halted=True,
                   open_condors=[tracked("x")])
    st = reconcile_account_state(sess, current_equity=88_000.0, day_start_equity=95_000.0)
    assert st.starting_equity == 100_000.0        # from the session, not current
    assert st.current_equity == 88_000.0
    assert st.day_start_equity == 95_000.0
    assert st.trading_halted is True              # sticky carried forward
    assert len(st.open_positions) == 1


# ======================================================================= #
# Daily performance summary
# ======================================================================= #
def test_daily_summary_is_copy_paste_ready() -> None:
    sess = Session(starting_equity=100_000.0, open_condors=[tracked("open1", credit=1.10)])
    sess.history = [
        {"kind": "opened", "at": "2026-08-31T10:05:00-04:00", "id": "open1",
         "detail": "3x condor exp 2026-09-04"},
        {"kind": "closed", "at": "2026-08-31T14:00:00-04:00", "id": "old7",
         "reason": "profit-target", "pnl": 180.0},
    ]
    text = agent.daily_summary_text(
        sess, current_equity=99_600.0, day_start_equity=100_000.0, et_date=date(2026, 8, 31),
    )
    assert "Daily Performance Summary" in text
    assert "day P&L $-400" in text
    assert "1 opened, 1 closed" in text
    assert "old7" in text and "profit-target" in text
    assert "Open positions: 1" in text
    assert "#ironcondor" in text


def test_daily_summary_notes_the_sticky_halt() -> None:
    sess = Session(starting_equity=100_000.0, trading_halted=True)
    text = agent.daily_summary_text(
        sess, current_equity=94_000.0, day_start_equity=96_000.0, et_date=date(2026, 9, 1),
    )
    assert "trading is halted" in text


# ======================================================================= #
# DecisionSummary.render
# ======================================================================= #
def test_decision_summary_render_has_stage_and_reason() -> None:
    s = DecisionSummary(True, "risk_officer", "vetoed", "risk_officer VETO — thin edge",
                        order_detail="3x condor exp 2026-09-04")
    out = s.render()
    assert "Vetoed at [risk_officer]" in out
    assert "thin edge" in out
    assert "order: 3x condor" in out
