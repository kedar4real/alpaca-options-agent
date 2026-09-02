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
from datetime import date, datetime
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

def test_load_env_early_makes_dotenv_agent_keys_visible(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "AGENT_TICKERS=AAA,BBB,CCC\nAGENT_PROFIT_TARGET_FRACTION=0.4\n"
    )
    monkeypatch.chdir(tmp_path)
    for k in ("AGENT_TICKERS", "AGENT_PROFIT_TARGET_FRACTION", "AGENT_ENV_FILE"):
        monkeypatch.delenv(k, raising=False)

    agent.load_env_early()
    cfg = agent.Config.from_env()

    assert cfg.tickers == ("AAA", "BBB", "CCC")
    assert cfg.profit_target_fraction == 0.4


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
    # entry credit 1.00; cost-to-close 0.70 -> captured 30% < 35% target
    assert decide_exit(valuation(0.70), is_expiring=False) is None


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
    full = tuple(tracked(f"c{i}").as_open_position() for i in range(4))
    summary = evaluate_cycle_decision(
        {}, account(positions=full), config=CFG, call_log=calls,
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
    # -$3,600 on the day -> over the 3.5% daily-loss halt
    s = halt_status(account(starting=100_000.0, current=96_400.0, day_start=100_000.0))
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
    sess = Session(starting_equity=100_000.0,
                   open_condors=[tracked("open1", credit=1.10)])  # symbol defaults to SPY
    sess.history = [
        {"kind": "opened", "at": "2026-08-31T10:05:00-04:00", "id": "open1",
         "symbol": "QQQ", "structure": "long_strangle", "regime": "Regime B: Low IV",
         "detail": "2x QQQ long_strangle debit $2.10"},
        {"kind": "closed", "at": "2026-08-31T14:00:00-04:00", "id": "old7",
         "symbol": "IWM", "structure": "bull_put", "reason": "profit-target", "pnl": 180.0},
    ]
    text = agent.daily_summary_text(
        sess, current_equity=99_600.0, day_start_equity=100_000.0, et_date=date(2026, 8, 31),
    )
    assert "Daily Performance Summary" in text
    assert "day P&L $-400" in text
    assert "1 opened, 1 closed" in text
    assert "IWM old7" in text and "bull_put / profit-target" in text
    assert "QQQ open1" in text and "long_strangle" in text
    assert "regime: Regime B: Low IV" in text
    assert "Open positions: 1/3" in text
    assert "#QQQ" in text and "#TLT" in text


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


# ======================================================================= #
# decide_exit — debit structures (long strangle)
# ======================================================================= #
def _debit_val(cost_to_close, *, debit=2.00, qty=3):
    c = TrackedCondor(id="ls", expiry=date(2099, 1, 1), quantity=qty,
                      entry_credit=-debit, legs=CONDOR_LEGS[:2],
                      structure="long_strangle")
    return CondorValuation(c, cost_to_close)


def test_strangle_profit_target_at_plus_50pct_of_the_debit() -> None:
    # paid 2.00 ; worth 3.00 now -> +50% -> close for profit.
    # all-long: value_condor gives cost_to_close = -(worth) = -3.00
    assert decide_exit(_debit_val(-3.00), is_expiring=False) == "profit-target"


def test_strangle_not_closed_when_only_up_30pct() -> None:
    # debit 2.00 paid; worth 2.60 now -> +30% < 35% target
    assert decide_exit(_debit_val(-2.60), is_expiring=False) is None


def test_strangle_stop_loss_at_minus_50pct_of_the_debit() -> None:
    # worth 1.00 now -> down 50% of the 2.00 premium
    assert decide_exit(_debit_val(-1.00), is_expiring=False) == "stop-loss"


def test_strangle_expiry_still_forces_a_close() -> None:
    assert decide_exit(_debit_val(-2.00), is_expiring=True) == "expiry"


def test_credit_structure_logic_is_unchanged() -> None:
    # a plain iron condor still scores on captured fraction of the credit
    c = TrackedCondor(id="ic", expiry=date(2099, 1, 1), quantity=3,
                      entry_credit=1.00, legs=CONDOR_LEGS, structure="iron_condor")
    assert decide_exit(CondorValuation(c, 0.50), is_expiring=False) == "profit-target"
    assert decide_exit(CondorValuation(c, 3.00), is_expiring=False) == "stop-loss"


# ======================================================================= #
# Multi-ticker run_cycle — independent evaluation, GLOBAL 3-position cap
# ======================================================================= #
class _FakeAcct:
    equity = 100_000.0
    last_equity = 100_000.0
    account_number = "PA_TEST"


class _FakeConn:
    creds = SimpleNamespace(api_key="k", secret_key="s", paper=True)

    def get_account(self):
        return _FakeAcct()

    def value_condors(self, condors):
        return []

    def close_condor(self, condor):
        pass


def _fake_executed_summary(symbol):
    plan = SimpleNamespace(
        suggested_contracts=1, expiry=date(2026, 9, 4), net_credit=1.2,
        symbol=symbol, structure="iron_condor", regime="Regime A",
        legs=[SimpleNamespace(action="sell", right="put",
                              contract=SimpleNamespace(symbol=f"{symbol}_P")),
              SimpleNamespace(action="buy", right="put",
                              contract=SimpleNamespace(symbol=f"{symbol}_Q"))],
    )
    result = SimpleNamespace(order_id=f"{symbol}-ord", submitted=True, error=None)
    return DecisionSummary(True, "executor", "executed", f"opened {symbol}",
                           order_detail=f"1x {symbol} iron_condor", plan=plan, result=result)


def _stub_context(monkeypatch, *, macro=False, priority=None, clusters=()):
    """Keep run_cycle offline: no real context_gatherer network calls."""
    from trading_agent.context_gatherer import MarketContext
    mc = MarketContext(
        as_of="2026-09-01T10:00:00+00:00", macro_events=(),
        macro_today_high_impact=macro, vix_proxy=17.0, vix_change_5d_pct=0.0,
        vix_note="calm", tickers=(), ok=True, errors=(),
        correlation_clusters=tuple(clusters),
    )
    monkeypatch.setattr(agent, "_gather_market_context", lambda conn, config, now_et: mc)
    if priority is not None:
        monkeypatch.setattr(agent, "rank_basket",
                            lambda symbols, snaps, ctx: list(priority))
    return mc


def test_run_cycle_evaluates_every_ticker_and_caps_positions_globally(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"),
                 tickers=("SPY", "QQQ", "IWM", "TLT"))
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, priority=("SPY", "QQQ", "IWM", "TLT"))

    monkeypatch.setattr(agent, "get_market_snapshot",
                        lambda symbol, creds=None: {"symbol": symbol})

    # every ticker "would" execute; the global cap must stop it at 3
    def fake_eval(snapshot, account, *, config, today=None, **kw):
        n = len(account.open_positions)
        if n >= 3:
            return DecisionSummary(False, "precheck", "skipped",
                                   f"max positions reached ({n}/3)")
        return _fake_executed_summary(snapshot["symbol"])

    monkeypatch.setattr(agent, "evaluate_cycle_decision", fake_eval)

    report = agent.run_cycle(_FakeConn(), session, cfg, now_et=None)

    assert [d.outcome for d in report.decisions] == \
        ["executed", "executed", "executed", "skipped"]
    assert len(session.open_condors) == 3                      # global cap held
    assert {c.symbol for c in session.open_condors} == {"SPY", "QQQ", "IWM"}
    assert len(report.opened) == 3
    assert report.decision.outcome == "executed"               # most-consequential


def test_run_cycle_one_bad_ticker_does_not_stop_the_others(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, priority=("QQQ",))

    def boom_snapshot(symbol, creds=None):
        if symbol == "SPY":
            raise RuntimeError("alpaca 500 for SPY")
        return {"symbol": symbol}

    monkeypatch.setattr(agent, "get_market_snapshot", boom_snapshot)
    monkeypatch.setattr(agent, "evaluate_cycle_decision",
                        lambda snapshot, account, **kw: DecisionSummary(
                            True, "strategy", "skipped", f"{snapshot['symbol']} no regime"))

    report = agent.run_cycle(_FakeConn(), session, cfg, now_et=None)
    assert [d.stage for d in report.decisions] == ["precheck", "strategy"]
    assert report.decisions[0].outcome == "error" and "SPY" in report.decisions[0].reason


def test_run_cycle_gathers_context_logs_it_and_threads_the_macro_guard(tmp_path, monkeypatch, caplog) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, macro=True, priority=("QQQ", "SPY"))
    monkeypatch.setattr(agent, "get_market_snapshot", lambda symbol, creds=None: {"symbol": symbol})

    seen = {"order": [], "mult": []}

    def fake_eval(snapshot, account, *, config, today=None, market_context="", **kw):
        seen["order"].append(snapshot["symbol"])
        seen["mult"].append(account.risk_multiplier)
        seen["ctx"] = market_context
        return DecisionSummary(True, "strategy", "skipped", "no setup", market_context=market_context)

    monkeypatch.setattr(agent, "evaluate_cycle_decision", fake_eval)

    with caplog.at_level("INFO"):
        report = agent.run_cycle(_FakeConn(), session, cfg, now_et=None)

    assert seen["order"] == ["QQQ", "SPY"]           # priority order honoured
    assert seen["mult"] == [0.5, 0.5]               # macro day -> gate-1 cap halved
    assert seen["ctx"] and all(d.market_context == seen["ctx"] for d in report.decisions)
    assert any("MARKET CONTEXT" in r.message for r in caplog.records)
    assert any("MACRO GUARD ACTIVE" in r.message for r in caplog.records)


def test_run_cycle_debates_only_the_top_ranked_candidate(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, priority=("QQQ", "SPY"))
    monkeypatch.setattr(agent, "get_market_snapshot", lambda symbol, creds=None: {"symbol": symbol})
    monkeypatch.setattr(agent.risk_officer, "load_lessons", lambda *a, **k: [])

    debated: list[str] = []

    def fake_debate(o, s, a, *, timeout=None, lessons=None):
        debated.append(s["symbol"])
        return SimpleNamespace(approved=False, ok=True, provider="debate/featherless",
                               thesis="stand aside",
                               transcript=lambda: "--- BULL ---\nx\n--- BEAR ---\ny\n--- JUDGE ---\nVETO")
    monkeypatch.setattr(agent.risk_officer, "debate_review", fake_debate)

    reviewed: list[str] = []

    def fake_review(o, s, a, *, timeout=None):
        reviewed.append(s["symbol"])
        return SimpleNamespace(approved=False, ok=True, provider="featherless", thesis="nope")
    monkeypatch.setattr(agent.risk_officer, "review_trade", fake_review)

    def fake_eval(snapshot, account, *, config, today=None, market_context="", context=None, **kw):
        return agent.evaluate_new_trade(snapshot, account, config=config, today=today,
                                        market_context=market_context, context=context, **kw)
    monkeypatch.setattr(agent, "evaluate_cycle_decision", fake_eval)
    monkeypatch.setattr(agent, "build_strategy_plan",
                        lambda snap, today=None, context=None: SimpleNamespace(
                            eligible=True, reason="ok", structure="iron_condor", regime="A",
                            regime_reason="r", expiry=date(2026, 9, 4), net_credit=1.0,
                            wing_width=4.0, suggested_contracts=1,
                            legs=[SimpleNamespace(action="sell", right="put",
                                                  contract=SimpleNamespace(symbol="P"))]))
    monkeypatch.setattr(agent.executor_mod, "from_plan",
                        lambda plan: SimpleNamespace(
                            legs=[SimpleNamespace(action="sell", right="put", symbol="P")],
                            quantity=1, net_credit=1.0, wing_width=4.0))
    monkeypatch.setattr(agent, "check_order",
                        lambda o, a, **kw: SimpleNamespace(approved=True, blocks=[]))

    report = agent.run_cycle(_FakeConn(), session, cfg, now_et=None)

    assert debated == ["QQQ"]                       # only the #1 ranked ticker
    assert reviewed == ["SPY"]                      # the rest get the single-pass review
    qqq = next(d for d in report.decisions if "stand aside" in d.reason)
    assert "BULL" in qqq.debate and "JUDGE" in qqq.debate


def test_run_cycle_threads_correlation_clusters_into_the_risk_check(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, priority=("SPY", "QQQ"),
                  clusters=(frozenset({"SPY", "QQQ"}),))
    monkeypatch.setattr(agent, "get_market_snapshot", lambda symbol, creds=None: {"symbol": symbol})
    monkeypatch.setattr(agent.risk_officer, "load_lessons", lambda *a, **k: [])
    monkeypatch.setattr(agent.risk_officer, "review_trade",
                        lambda o, s, a, *, timeout=None: SimpleNamespace(
                            approved=False, ok=True, provider="featherless", thesis="n/a"))
    monkeypatch.setattr(agent, "build_strategy_plan",
                        lambda snap, today=None, context=None: SimpleNamespace(
                            eligible=True, reason="ok", structure="iron_condor", regime="A",
                            regime_reason="r", expiry=date(2026, 9, 4), net_credit=1.0,
                            wing_width=4.0, suggested_contracts=1, symbol=snap.get("symbol"),
                            legs=[SimpleNamespace(action="sell", right="put",
                                                  contract=SimpleNamespace(symbol="P"))]))
    monkeypatch.setattr(agent.executor_mod, "from_plan",
                        lambda plan: SimpleNamespace(
                            legs=[SimpleNamespace(action="sell", right="put", symbol="P")],
                            quantity=1, net_credit=1.0, wing_width=4.0,
                            underlying=getattr(plan, "symbol", None)))

    seen: list = []

    def spy_check(o, a, **kw):
        seen.append(kw.get("correlation_clusters"))
        return SimpleNamespace(approved=False, blocks=["stop here"], checks={})
    monkeypatch.setattr(agent, "check_order", spy_check)

    agent.run_cycle(_FakeConn(), session, cfg, now_et=None)

    assert seen and seen[0] == (frozenset({"SPY", "QQQ"}),)


def test_run_cycle_runs_self_correction_on_each_close(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY",))
    session = Session(starting_equity=100_000.0,
                      open_condors=[tracked("QQQ-1", credit=1.0)])
    session.open_condors[0].symbol = "QQQ"
    _stub_context(monkeypatch, priority=())
    monkeypatch.setattr(agent, "get_market_snapshot", lambda symbol, creds=None: {"symbol": symbol})
    monkeypatch.setattr(agent, "evaluate_cycle_decision",
                        lambda *a, **k: DecisionSummary(True, "strategy", "skipped", "no setup"))

    analyzed: list[dict] = []
    monkeypatch.setattr(agent.risk_officer, "post_trade_analysis",
                        lambda ev, **kw: analyzed.append(ev))

    class _ClosingConn(_FakeConn):
        def value_condors(self, condors):
            return [agent.CondorValuation(condors[0], 0.20)]   # deep profit -> closes

    agent.run_cycle(_ClosingConn(), session, cfg, now_et=None)
    assert len(analyzed) == 1 and analyzed[0]["reason"] == "profit-target"
    assert analyzed[0]["symbol"] == "QQQ"


def test_run_cycle_context_failure_is_non_fatal(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY",))
    session = Session(starting_equity=100_000.0)
    # real _gather_market_context, but the IntelligenceHub blows up -> unavailable fallback
    monkeypatch.setattr(agent.intelligence_hub, "gather",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down")))
    monkeypatch.setattr(agent, "get_market_snapshot", lambda symbol, creds=None: {"symbol": symbol})
    monkeypatch.setattr(agent, "evaluate_cycle_decision",
                        lambda snapshot, account, **kw: DecisionSummary(
                            True, "strategy", "skipped", "no setup",
                            market_context=kw.get("market_context", "")))

    report = agent.run_cycle(_FakeConn(), session, cfg, now_et=None)
    assert report.decisions[0].market_context == "No Context Available"


# ======================================================================= #
# TrackedCondor — symbol + structure persist
# ======================================================================= #
def test_tracked_condor_round_trips_symbol_and_structure(tmp_path) -> None:
    c = TrackedCondor(id="x", symbol="TLT", structure="long_strangle",
                      expiry=date(2026, 9, 4), quantity=2, entry_credit=-2.1,
                      legs=CONDOR_LEGS[:2])
    session = Session(starting_equity=100_000.0, open_condors=[c])
    path = str(tmp_path / "s.json")
    agent.save_session(session, path)
    back = agent.load_session(path).open_condors[0]
    assert back.symbol == "TLT" and back.structure == "long_strangle"
    assert back.entry_credit == -2.1


def test_describe_order_labels_a_debit_structure() -> None:
    order = SimpleNamespace(
        quantity=2, net_credit=-2.10, wing_width=0.0,
        legs=[SimpleNamespace(action="buy", right="put", symbol="QQQ_P"),
              SimpleNamespace(action="buy", right="call", symbol="QQQ_C")],
    )
    plan = SimpleNamespace(expiry=date(2026, 9, 4), structure="long_strangle", symbol="QQQ")
    out = agent._describe_order(order, plan)
    assert "QQQ long_strangle" in out and "debit $2.10" in out


# ======================================================================= #
# Off-hours intelligence — scheduling / wiring (pure logic in test_offhours)
# ======================================================================= #
def test_run_cycle_accumulates_the_daily_activity_funnel(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, priority=("SPY", "QQQ"))
    monkeypatch.setattr(agent, "get_market_snapshot",
                        lambda symbol, creds=None: {"symbol": symbol})
    monkeypatch.setattr(agent, "evaluate_cycle_decision",
                        lambda snapshot, account, **kw: _fake_executed_summary(snapshot["symbol"]))

    agent.run_cycle(_FakeConn(), session, cfg, now_et=datetime(2026, 9, 1, 12, 0, tzinfo=agent.ET))

    (iso, act), = session.daily_activity.items()
    assert iso == "2026-09-01"
    assert act["ticker_scans"] == 2
    assert act["approved"] == 2
    assert act["basket_size"] == 2
    assert act["regimes"]["Regime A"] == 2

    # a second cycle the same day keeps accumulating into the same bucket
    agent.run_cycle(_FakeConn(), session, cfg, now_et=datetime(2026, 9, 1, 12, 15, tzinfo=agent.ET))
    assert session.daily_activity["2026-09-01"]["ticker_scans"] == 4


# --------------------------------------------------------------------------- #
# Step 3 — competition hard stop
# --------------------------------------------------------------------------- #
def test_hard_stop_reached_boundary() -> None:
    cutoff = "2026-09-04 10:30"
    assert agent.hard_stop_reached(datetime(2026, 9, 4, 10, 29, tzinfo=agent.ET), cutoff) is False
    assert agent.hard_stop_reached(datetime(2026, 9, 4, 10, 30, tzinfo=agent.ET), cutoff) is True
    assert agent.hard_stop_reached(datetime(2026, 9, 4, 11, 0, tzinfo=agent.ET), cutoff) is True
    assert agent.hard_stop_reached(datetime(2026, 9, 3, 23, 59, tzinfo=agent.ET), cutoff) is False


def test_hard_stop_reached_fails_open_on_bad_config() -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=agent.ET)
    assert agent.hard_stop_reached(now, "") is False
    assert agent.hard_stop_reached(now, "not a date") is False
    assert agent.hard_stop_reached(now, None) is False


class _HardStopConn(_FakeConn):
    def __init__(self, positions=None):
        self._positions = list(positions or [])
        self.flatten_calls = 0

    def get_positions(self):
        return list(self._positions)

    def flatten_all(self):
        self.flatten_calls += 1
        n = len(self._positions)
        self._positions = []
        return n, 0


def _no_context(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("context pulled after hard stop")
    monkeypatch.setattr(agent, "_gather_market_context", boom)


def test_run_cycle_hard_stop_flattens_and_refuses_new_trades(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"),
                 hard_stop_et="2026-09-04 10:30")
    session = Session(starting_equity=100_000.0, open_condors=[tracked("c1")])
    conn = _HardStopConn(positions=[SimpleNamespace(symbol="SPY_P"),
                                    SimpleNamespace(symbol="SPY_Q")])
    calls: list[str] = []
    monkeypatch.setattr(agent, "build_strategy_plan", lambda *a, **k: calls.append("strategy"))
    _no_context(monkeypatch)

    report = agent.run_cycle(conn, session, cfg,
                             now_et=datetime(2026, 9, 4, 10, 31, tzinfo=agent.ET))

    assert conn.flatten_calls == 1
    assert session.open_condors == []
    assert session.hard_stop_done is True
    assert report.decisions == [] and report.opened == []
    assert calls == []                          # strategy pipeline never ran


def test_run_cycle_hard_stop_second_pass_confirms_flat_without_reflatten(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), hard_stop_et="2026-09-04 10:30")
    session = Session(starting_equity=100_000.0, hard_stop_done=True)
    conn = _HardStopConn(positions=[])
    _no_context(monkeypatch)
    report = agent.run_cycle(conn, session, cfg,
                             now_et=datetime(2026, 9, 4, 15, 0, tzinfo=agent.ET))
    assert conn.flatten_calls == 0             # already flat -> no re-flatten
    assert report.decisions == []


def test_run_cycle_before_hard_stop_runs_the_normal_pipeline(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY",),
                 hard_stop_et="2026-09-04 10:30")
    session = Session(starting_equity=100_000.0)
    _stub_context(monkeypatch, priority=("SPY",))
    monkeypatch.setattr(agent, "get_market_snapshot", lambda symbol, creds=None: {"symbol": symbol})
    ran: list[str] = []
    monkeypatch.setattr(agent, "evaluate_cycle_decision",
                        lambda *a, **k: ran.append("x") or DecisionSummary(True, "strategy", "skipped", "x"))
    agent.run_cycle(_FakeConn(), session, cfg,
                    now_et=datetime(2026, 9, 4, 9, 0, tzinfo=agent.ET))
    assert ran == ["x"] and session.hard_stop_done is False


def test_session_round_trips_offhours_markers(tmp_path) -> None:
    sess = Session(starting_equity=100_000.0)
    sess.last_heartbeat_at = "2026-09-01T10:00:00-04:00"
    sess.last_morning_brief_date = "2026-09-01"
    sess.last_post_mortem_date = "2026-08-31"
    sess.daily_activity = {"2026-09-01": {"date": "2026-09-01", "ticker_scans": 5}}
    path = str(tmp_path / "s.json")
    agent.save_session(sess, path)
    back = agent.load_session(path)
    assert back.last_heartbeat_at == "2026-09-01T10:00:00-04:00"
    assert back.last_morning_brief_date == "2026-09-01"
    assert back.last_post_mortem_date == "2026-08-31"
    assert back.daily_activity["2026-09-01"]["ticker_scans"] == 5


def test_from_dict_defaults_offhours_fields_for_a_legacy_session() -> None:
    back = Session.from_dict({"starting_equity": 100_000.0})   # pre-offhours session.json
    assert back.last_heartbeat_at == ""
    assert back.daily_activity == {}


def test_maybe_heartbeat_fires_once_per_interval(tmp_path, monkeypatch) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), heartbeat_minutes=60)
    sess = Session(starting_equity=100_000.0)
    iv = tmp_path / "iv.csv"
    iv.write_text("timestamp,symbol,iv,rv,spread\n2026-09-01T01:00,SPY,0.1,0.1,0.0\n")
    monkeypatch.setattr(agent, "IV_HISTORY_PATH", str(iv))

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=agent.ET)
    hb = agent._maybe_heartbeat(sess, cfg, now_et=t0, market_open=False, connectivity_ok=True)
    assert hb is not None and hb.iv_readings == 1 and hb.status == "Idle"
    assert sess.last_heartbeat_at == t0.isoformat()

    # 20 min later -> not due
    assert agent._maybe_heartbeat(
        sess, cfg, now_et=datetime(2026, 9, 1, 10, 20, tzinfo=agent.ET),
        market_open=False, connectivity_ok=True) is None

    # just over an hour later -> due again, and now market is open
    hb2 = agent._maybe_heartbeat(
        sess, cfg, now_et=datetime(2026, 9, 1, 11, 5, tzinfo=agent.ET),
        market_open=True, connectivity_ok=True)
    assert hb2 is not None and hb2.status == "Active"


def test_maybe_heartbeat_reports_connectivity_error(tmp_path) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"))
    sess = Session(starting_equity=100_000.0)
    hb = agent._maybe_heartbeat(sess, cfg, now_et=datetime(2026, 9, 1, 3, 0, tzinfo=agent.ET),
                                market_open=False, connectivity_ok=False)
    assert hb is not None and hb.connectivity == "Error"


class _BriefConn(_FakeConn):
    def premarket_gaps(self, tickers):
        from trading_agent.offhours import TickerGap
        return [TickerGap("SPY", 600.0, 604.5),    # +0.75% -> alert
                TickerGap("QQQ", 500.0, 500.4)]    # +0.08% -> flat


def test_maybe_morning_brief_only_in_window_and_once_per_day(tmp_path) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    sess = Session(starting_equity=100_000.0)
    conn = _BriefConn()

    # 08:50 ET — before the window
    assert agent._maybe_morning_brief(
        sess, conn, cfg, now_et=datetime(2026, 9, 1, 8, 50, tzinfo=agent.ET)) is None

    # 09:10 ET — fires
    text = agent._maybe_morning_brief(
        sess, conn, cfg, now_et=datetime(2026, 9, 1, 9, 10, tzinfo=agent.ET))
    assert text and "PRE-MARKET ALERT" in text and "TRENDING" in text
    assert sess.last_morning_brief_date == "2026-09-01"

    # 09:20 ET same day — already done
    assert agent._maybe_morning_brief(
        sess, conn, cfg, now_et=datetime(2026, 9, 1, 9, 20, tzinfo=agent.ET)) is None


def test_maybe_post_mortem_after_close_once_per_day(tmp_path) -> None:
    cfg = Config(session_file=str(tmp_path / "s.json"), tickers=("SPY", "QQQ"))
    sess = Session(starting_equity=100_000.0)
    sess.daily_activity = {"2026-09-01": {
        "date": "2026-09-01", "basket_size": 2, "ticker_scans": 12,
        "proposed": 2, "approved": 1, "rm_vetoes": 1, "ro_vetoes": 0,
        "regimes": {"Regime B: Low IV / Range-Bound -> Long Strangle": 10},
    }}
    conn = _FakeConn()

    # 15:30 ET — before the close
    assert agent._maybe_post_mortem(
        sess, conn, cfg, now_et=datetime(2026, 9, 1, 15, 30, tzinfo=agent.ET)) is None

    # 16:05 ET — fires
    text = agent._maybe_post_mortem(
        sess, conn, cfg, now_et=datetime(2026, 9, 1, 16, 5, tzinfo=agent.ET))
    assert text and "Nightly Post-Mortem" in text
    assert "Overall Range-Bound" in text
    assert "Trades approved:          1" in text
    assert sess.last_post_mortem_date == "2026-09-01"

    # later the same day — no repeat
    assert agent._maybe_post_mortem(
        sess, conn, cfg, now_et=datetime(2026, 9, 1, 16, 30, tzinfo=agent.ET)) is None
