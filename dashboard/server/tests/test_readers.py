"""Tests for the dashboard's log/state parsers.

Every parser here is pure: text in, plain data out. The agent's own files are
the only input, and nothing in this package ever writes to them.
"""

import readers


# --------------------------------------------------------------------------- #
# parse_decisions — the per-ticker DECISION SUMMARY lines in agent.log
# --------------------------------------------------------------------------- #
DECISION_LOG = """\
2026-09-03 00:42:33 INFO    agent: cycle priority order: XLE > XLF > QQQ
2026-09-03 00:42:33 INFO    agent: [IWM] DECISION SUMMARY - Skipped at [precheck]: already holds a position or working order
2026-09-03 00:42:33 INFO    agent: [XLK] DECISION SUMMARY - Blocked at [risk_manager]: risk_manager rejected - long-vol concentration: total long-vol debit $5,688 > $3,981 (4% of current equity)
2026-09-03 00:42:33 INFO    agent: [HYG] DECISION SUMMARY - Skipped at [strategy]: strategy did not propose a trade - no tradeable regime
2026-09-02 22:13:41 INFO    agent: SUBMITTED IWM f058567f-6b43-4055-a2e8-e738044572c9 [long_strangle] - pending fill - 16x IWM long_strangle exp 2026-09-04 debit $1.19 width $0.00 [BP:IWM260904P00290000 BC:IWM260904C00296000]
2026-09-03 18:39:35 INFO    agent: market closed - next open 2026-09-03 09:30:00-04:00
"""


def test_parse_decisions_extracts_symbol_outcome_stage_and_reason():
    rows = readers.parse_decisions(DECISION_LOG)

    assert [r["symbol"] for r in rows] == ["IWM", "XLK", "HYG"]
    assert rows[0]["outcome"] == "Skipped"
    assert rows[0]["stage"] == "precheck"
    assert rows[0]["reason"].startswith("already holds a position")
    assert rows[0]["ts"] == "2026-09-03 00:42:33"


def test_parse_decisions_keeps_the_full_reason_including_dollar_figures():
    rows = readers.parse_decisions(DECISION_LOG)
    blocked = next(r for r in rows if r["symbol"] == "XLK")

    assert "$5,688 > $3,981" in blocked["reason"]
    assert blocked["stage"] == "risk_manager"


def test_parse_decisions_ignores_non_decision_lines():
    rows = readers.parse_decisions(DECISION_LOG)

    assert all("market closed" not in r["reason"] for r in rows)
    assert all(r["symbol"] != "SUBMITTED" for r in rows)


def test_parse_decisions_returns_empty_for_blank_input():
    assert readers.parse_decisions("") == []


def test_parse_decisions_newest_first_when_requested():
    rows = readers.parse_decisions(DECISION_LOG, newest_first=True)
    assert [r["symbol"] for r in rows] == ["HYG", "XLK", "IWM"]


# --------------------------------------------------------------------------- #
# parse_orders — OPENED / SUBMITTED lines
# --------------------------------------------------------------------------- #
def test_parse_orders_reads_submitted_and_opened_events():
    log = (
        "2026-09-02 21:24:54 INFO    agent: OPENED TLT 1ba139b6-27a1-4b83-93a2-9e52f7a211c4 "
        "[long_strangle] - 76x TLT long_strangle exp 2026-09-04 debit $0.26 width $0.00 "
        "[BP:TLT260904P00081500 BC:TLT260904C00082500]\n"
        "2026-09-02 22:13:41 INFO    agent: SUBMITTED IWM f058567f-6b43-4055-a2e8-e738044572c9 "
        "[long_strangle] - pending fill - 16x IWM long_strangle exp 2026-09-04 debit $1.19\n"
    )
    rows = readers.parse_orders(log)

    assert [r["event"] for r in rows] == ["OPENED", "SUBMITTED"]
    assert rows[0]["symbol"] == "TLT"
    assert rows[0]["structure"] == "long_strangle"
    assert rows[0]["order_id"].startswith("1ba139b6")
    assert rows[1]["symbol"] == "IWM"


# --------------------------------------------------------------------------- #
# parse_scan_table — the basket scan block in agent_activity.log
# --------------------------------------------------------------------------- #
SCAN_BLOCK = """\
============================================================
SCAN TABLE  (IV-RV>=+0.015  ER<0.45  floor>0.08  c/w>=20%)
----------------------------------------------------------
  SPY   px  764.78  IV 0.132  RV 0.079  IVRV +0.052 ok    ER 0.13 ok    floor ok    c/w   --   -> skipped [precheck] already holds a position in SPY
  GLD   px  401.80  IV 0.212  RV 0.279  IVRV -0.067 FAIL  ER 0.21 ok    floor ok    c/w   --   -> blocked [risk_manager] long-vol concentration
  HYG   px   79.12  IV 0.041  RV 0.050  IVRV -0.010 FAIL  ER 0.40 ok    floor FAIL  c/w   --   -> skipped [strategy] no tradeable regime
============================================================
"""


def test_parse_scan_table_reads_every_row():
    scan = readers.parse_scan_table(SCAN_BLOCK)

    assert [r["symbol"] for r in scan["rows"]] == ["SPY", "GLD", "HYG"]
    assert scan["thresholds"].startswith("IV-RV>=")


def test_parse_scan_table_converts_numeric_columns():
    scan = readers.parse_scan_table(SCAN_BLOCK)
    spy = scan["rows"][0]

    assert spy["price"] == 764.78
    assert spy["iv"] == 0.132
    assert spy["rv"] == 0.079
    assert spy["iv_rv"] == 0.052


def test_parse_scan_table_records_per_gate_pass_fail():
    scan = readers.parse_scan_table(SCAN_BLOCK)
    gld = next(r for r in scan["rows"] if r["symbol"] == "GLD")
    hyg = next(r for r in scan["rows"] if r["symbol"] == "HYG")

    assert gld["iv_rv_ok"] is False
    assert gld["er_ok"] is True
    assert gld["floor_ok"] is True
    assert hyg["floor_ok"] is False


def test_parse_scan_table_splits_outcome_stage_and_reason():
    scan = readers.parse_scan_table(SCAN_BLOCK)
    gld = next(r for r in scan["rows"] if r["symbol"] == "GLD")

    assert gld["outcome"] == "blocked"
    assert gld["stage"] == "risk_manager"
    assert gld["reason"] == "long-vol concentration"


def test_parse_scan_table_uses_the_last_block_when_several_are_present():
    older = SCAN_BLOCK.replace("SPY", "AAA")
    scan = readers.parse_scan_table(older + "\n" + SCAN_BLOCK)

    assert scan["rows"][0]["symbol"] == "SPY"


def test_parse_scan_table_returns_empty_when_no_block_present():
    scan = readers.parse_scan_table("nothing to see here")
    assert scan["rows"] == []


# --------------------------------------------------------------------------- #
# parse_signals — the macro / VIX / RSI / ADX context line
# --------------------------------------------------------------------------- #
SIGNAL_LINE = (
    "Macro: Employment Situation (NFP) (2026-09-04) | "
    "VIX: VIX 15.25 / VXV 17.76 (ratio 0.86, contango) | "
    "REGIME SIGNALS: MACRO_DANGER | "
    "CORRELATED (>0.8, 10d): {DIA,IWM}; {GLD,SLV}; {QQQ,SPY} | "
    "News SPY: First headline; Second headline | "
    "RSI SPY: 56.6 (neutral) | ADX SPY: 12.8 (range, up) | "
    "RSI QQQ: 50.6 (neutral) | ADX QQQ: 10.5 (range, down)"
)


def test_parse_signals_reads_the_macro_event():
    sig = readers.parse_signals(SIGNAL_LINE)

    assert sig["macro_event"] == "Employment Situation (NFP)"
    assert sig["macro_date"] == "2026-09-04"


def test_parse_signals_reads_the_vix_term_structure():
    sig = readers.parse_signals(SIGNAL_LINE)

    assert sig["vix"] == 15.25
    assert sig["vix3m"] == 17.76
    assert sig["vix_ratio"] == 0.86
    assert sig["vix_state"] == "contango"


def test_parse_signals_reads_regime_flags_and_correlation_clusters():
    sig = readers.parse_signals(SIGNAL_LINE)

    assert sig["regime_signals"] == ["MACRO_DANGER"]
    assert ["DIA", "IWM"] in sig["correlated"]
    assert ["QQQ", "SPY"] in sig["correlated"]


def test_parse_signals_reads_per_symbol_rsi_adx_and_news():
    sig = readers.parse_signals(SIGNAL_LINE)

    assert sig["symbols"]["SPY"]["rsi"] == 56.6
    assert sig["symbols"]["SPY"]["adx"] == 12.8
    assert sig["symbols"]["SPY"]["adx_label"] == "range, up"
    assert sig["symbols"]["SPY"]["news"] == ["First headline", "Second headline"]
    assert sig["symbols"]["QQQ"]["rsi"] == 50.6


def test_parse_signals_tolerates_a_missing_line():
    sig = readers.parse_signals("")

    assert sig["vix"] is None
    assert sig["regime_signals"] == []
    assert sig["symbols"] == {}


# --------------------------------------------------------------------------- #
# long_vol_exposure — the 4% concentration cap, as the risk manager computes it
# --------------------------------------------------------------------------- #
POSITIONS = [
    {"symbol": "IWM", "structure": "long_strangle", "quantity": 16, "entry_credit": -1.185},
    {"symbol": "SPY", "structure": "long_strangle", "quantity": 8, "entry_credit": -2.25},
]


def test_long_vol_exposure_totals_debit_across_positions():
    exp = readers.long_vol_exposure(POSITIONS, equity=99282.08)

    # 16 x 1.185 x 100 = 1896 ; 8 x 2.25 x 100 = 1800
    assert exp["debit"] == 3696.0
    assert exp["count"] == 2


def test_long_vol_exposure_reports_the_cap_and_headroom():
    exp = readers.long_vol_exposure(POSITIONS, equity=100000.0, max_pct=0.04)

    assert exp["cap"] == 4000.0
    assert exp["headroom"] == 304.0
    assert exp["breached"] is False


def test_long_vol_exposure_flags_a_breach():
    heavy = POSITIONS + [
        {"symbol": "GLD", "structure": "long_strangle", "quantity": 8, "entry_credit": -2.45}
    ]
    exp = readers.long_vol_exposure(heavy, equity=100000.0, max_pct=0.04)

    assert exp["debit"] == 5656.0
    assert exp["breached"] is True
    assert exp["headroom"] == 0.0


def test_long_vol_exposure_counts_only_debit_structures():
    mixed = POSITIONS + [
        {"symbol": "QQQ", "structure": "iron_condor", "quantity": 5, "entry_credit": 1.10}
    ]
    exp = readers.long_vol_exposure(mixed, equity=100000.0)

    assert exp["count"] == 2
    assert exp["debit"] == 3696.0


def test_long_vol_exposure_handles_an_empty_book():
    exp = readers.long_vol_exposure([], equity=100000.0)

    assert exp["debit"] == 0.0
    assert exp["count"] == 0
    assert exp["breached"] is False


# --------------------------------------------------------------------------- #
# decision_funnel — how many tickers died at each pipeline stage
# --------------------------------------------------------------------------- #
def test_decision_funnel_counts_by_stage_preserving_pipeline_order():
    decisions = [
        {"symbol": "A", "outcome": "Skipped", "stage": "precheck", "reason": ""},
        {"symbol": "B", "outcome": "Skipped", "stage": "strategy", "reason": ""},
        {"symbol": "C", "outcome": "Blocked", "stage": "risk_manager", "reason": ""},
        {"symbol": "D", "outcome": "Blocked", "stage": "risk_manager", "reason": ""},
        {"symbol": "E", "outcome": "Executed", "stage": "executor", "reason": ""},
    ]
    funnel = readers.decision_funnel(decisions)

    assert [s["stage"] for s in funnel] == [
        "precheck", "strategy", "risk_manager", "risk_officer", "executor",
    ]
    assert {s["stage"]: s["count"] for s in funnel}["risk_manager"] == 2
    assert {s["stage"]: s["count"] for s in funnel}["risk_officer"] == 0


def test_decision_funnel_on_no_decisions_is_all_zero():
    funnel = readers.decision_funnel([])
    assert all(s["count"] == 0 for s in funnel)


# --------------------------------------------------------------------------- #
# iv_series — the per-symbol IV/RV history behind the chart
# --------------------------------------------------------------------------- #
IV_CSV = """\
timestamp,symbol,iv,rv,spread
2026-09-03T01:30:01,SPY,0.1337,0.0790,0.0547
2026-09-03T01:30:05,GLD,0.2120,0.2790,-0.0670
2026-09-03T02:30:01,SPY,0.1300,0.0800,0.0500
2026-09-03T03:30:01,SPY,,,
"""


def test_iv_series_groups_points_by_symbol():
    series = readers.iv_series(IV_CSV)

    assert set(series) == {"SPY", "GLD"}
    assert len(series["SPY"]) == 2


def test_iv_series_keeps_numeric_values_and_drops_blank_rows():
    series = readers.iv_series(IV_CSV)

    assert series["SPY"][0]["iv"] == 0.1337
    assert series["SPY"][0]["rv"] == 0.079
    assert series["GLD"][0]["spread"] == -0.067
    assert all(p["iv"] is not None for p in series["SPY"])


def test_iv_series_can_limit_to_requested_symbols():
    series = readers.iv_series(IV_CSV, symbols=["SPY"])

    assert set(series) == {"SPY"}


def test_iv_series_on_empty_input():
    assert readers.iv_series("") == {}
