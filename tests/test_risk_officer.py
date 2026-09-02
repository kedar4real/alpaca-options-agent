"""Offline tests for risk_officer.py.

Two providers: Featherless AI (primary, OpenAI-compatible) and Ollama (fallback).
Both are mocked here — no network. Covers each provider on its own, the
Featherless -> Ollama fallback path, and the both-failed fail-safe VETO.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
import requests

from trading_agent import risk_officer as ro
from trading_agent.risk_manager import AccountState, OrderLeg, ProposedOrder

CONDOR = (
    OrderLeg("sell", "put", 3, "SPY260901P00762000"),
    OrderLeg("buy", "put", 3, "SPY260901P00758000"),
    OrderLeg("sell", "call", 3, "SPY260901C00770000"),
    OrderLeg("buy", "call", 3, "SPY260901C00772000"),
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Never touch a real .env or a real Featherless key. Tests opt into the
    Featherless path explicitly via ``featherless_client=`` / ``featherless_api_key=``."""
    monkeypatch.setattr(ro, "_ensure_env_loaded", lambda: None)
    monkeypatch.delenv("FEATHERLESS_API_KEY", raising=False)
    monkeypatch.delenv("FEATHERLESS_MODEL", raising=False)
    monkeypatch.delenv("FEATHERLESS_BASE_URL", raising=False)


def order():
    # risk_dollars = (4.0 - 1.2) * 100 * 3 = 840
    return ProposedOrder(4.0, 1.2, 3, CONDOR)


def account(current=99_000.0):
    return AccountState(100_000.0, current, 99_400.0, open_positions=())


def snapshot(**overrides):
    base = dict(
        current_price=766.0,
        atm_iv=0.22,
        realized_vol=0.14,
        iv_rv_spread=0.08,
        iv_regime=SimpleNamespace(
            mode="hackathon_static", trade_eligible=True,
            reason="ATM IV 0.220 > 0.15", atm_iv=0.22, iv_percentile=None,
        ),
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Fakes: Ollama HTTP session
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, *, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, *, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.resp


def reply(text):
    return FakeResp(payload={"response": text})


# --------------------------------------------------------------------------- #
# Fakes: Featherless (OpenAI-compatible) client
# --------------------------------------------------------------------------- #
class FakeCompletions:
    def __init__(self, *, content=None, exc=None):
        self._content = content
        self._exc = exc
        self.calls = []

    def create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class FakeFeatherless:
    """Stands in for ``openai.OpenAI`` — exposes ``.chat.completions.create``."""

    def __init__(self, *, content=None, exc=None):
        self._completions = FakeCompletions(content=content, exc=exc)
        self.chat = SimpleNamespace(completions=self._completions)

    @property
    def calls(self):
        return self._completions.calls


APPROVE = (
    "VERDICT: APPROVE\n"
    "THESIS: IV sits well above realized vol so premium is rich. The book is "
    "empty and max loss is under 1% of equity. Reasonable to open."
)
VETO = (
    "VERDICT: VETO\n"
    "THESIS: The IV-RV spread is too thin to pay for the risk and the regime is "
    "only the static fallback. Wait for richer vol."
)


# --------------------------------------------------------------------------- #
# parse_review — pure, provider-agnostic
# --------------------------------------------------------------------------- #
def test_thesis_falls_back_when_no_thesis_label() -> None:
    r = ro.parse_review(
        "VERDICT: APPROVE\nThe regime supports selling premium and exposure is light.",
        "m",
    )
    assert r.approved is True and r.ok is True
    assert "regime supports selling premium" in r.thesis


@pytest.mark.parametrize(
    ("text", "approved"),
    [
        ("VERDICT: APPROVE\nTHESIS: yes.", True),
        ("verdict: veto\nthesis: no.", False),
        ("VERDICT:APPROVE THESIS:tight spread", True),
        ("preamble\nVERDICT - VETO\nTHESIS - not now", False),
        ("VERDICT: APPROVE  \nTHESIS: trailing spaces are fine.", True),  # Featherless style
    ],
)
def test_parse_review_verdict_forms(text, approved) -> None:
    r = ro.parse_review(text, "m")
    assert r.ok is True and r.approved is approved


def test_empty_response_is_unparseable() -> None:
    r = ro.parse_review("", "m")
    assert r.approved is False and r.ok is False


# --------------------------------------------------------------------------- #
# Featherless (primary) — happy path
# --------------------------------------------------------------------------- #
def test_featherless_approve_is_used_ollama_not_called() -> None:
    fl = FakeFeatherless(content=APPROVE)
    sess = FakeSession(exc=AssertionError("ollama must not be called"))
    r = ro.review_trade(
        order(), snapshot(), account(),
        featherless_client=fl, featherless_model="Qwen/Qwen2.5-7B-Instruct",
        session=sess,
    )
    assert r.approved is True and r.ok is True
    assert r.provider == "featherless"
    assert r.model == "Qwen/Qwen2.5-7B-Instruct"
    assert "premium is rich" in r.thesis
    assert sess.calls == []                       # fallback never touched
    assert fl.calls[0]["model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert fl.calls[0]["messages"][0]["role"] == "user"
    assert "PROPOSED TRADE" in fl.calls[0]["messages"][0]["content"]


def test_featherless_call_caps_max_tokens() -> None:
    fl = FakeFeatherless(content=APPROVE)
    sess = FakeSession(exc=AssertionError("ollama must not be called"))
    ro.review_trade(
        order(), snapshot(), account(),
        featherless_client=fl, featherless_model="meta-llama/Llama-3.3-70B-Instruct",
        session=sess,
    )
    assert fl.calls[0]["max_tokens"] == ro.OFFICER_MAX_TOKENS


def test_featherless_veto_is_used() -> None:
    fl = FakeFeatherless(content=VETO)
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl)
    assert r.approved is False and r.ok is True
    assert r.provider == "featherless"
    assert "too thin" in r.thesis


def test_featherless_key_builds_client(monkeypatch) -> None:
    """No explicit client, but a key -> _build_featherless_client is used."""
    built = {}

    def fake_build(api_key, base_url):
        built["api_key"] = api_key
        built["base_url"] = base_url
        return FakeFeatherless(content=APPROVE)

    monkeypatch.setattr(ro, "_build_featherless_client", fake_build)
    r = ro.review_trade(
        order(), snapshot(), account(),
        featherless_api_key="rc_testkey", featherless_base_url="https://api.featherless.ai/v1",
    )
    assert r.provider == "featherless" and r.approved is True
    assert built == {"api_key": "rc_testkey", "base_url": "https://api.featherless.ai/v1"}


# --------------------------------------------------------------------------- #
# Fallback: Featherless fails -> Ollama succeeds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fl_exc",
    [
        requests.ConnectionError("connection refused"),
        requests.Timeout("read timed out"),
        RuntimeError("401 Unauthorized: bad api key"),   # auth error
        ValueError("malformed response"),
    ],
    ids=["connection", "timeout", "auth", "malformed"],
)
def test_featherless_fails_ollama_succeeds(fl_exc) -> None:
    fl = FakeFeatherless(exc=fl_exc)
    sess = FakeSession(resp=reply(APPROVE))
    r = ro.review_trade(
        order(), snapshot(), account(),
        featherless_client=fl, session=sess, model="llama3.2",
    )
    assert r.approved is True and r.ok is True
    assert r.provider == "ollama"
    assert r.model == "llama3.2"
    assert len(sess.calls) == 1                   # fallback actually ran


def test_featherless_unparseable_falls_back_to_ollama() -> None:
    fl = FakeFeatherless(content="Hmm, hard to say, let me think about it.")
    sess = FakeSession(resp=reply(VETO))
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.ok is True and r.provider == "ollama"
    assert r.approved is False


def test_featherless_empty_content_falls_back_to_ollama() -> None:
    fl = FakeFeatherless(content=None)            # choices[0].message.content is None
    sess = FakeSession(resp=reply(APPROVE))
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.ok is True and r.provider == "ollama" and r.approved is True


def test_featherless_no_choices_falls_back_to_ollama() -> None:
    fl = FakeFeatherless(content=APPROVE)
    fl._completions.create = lambda **kw: SimpleNamespace(choices=[])  # malformed
    sess = FakeSession(resp=reply(VETO))
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.ok is True and r.provider == "ollama" and r.approved is False


# --------------------------------------------------------------------------- #
# No Featherless key -> Ollama is the primary path
# --------------------------------------------------------------------------- #
def test_no_featherless_key_uses_ollama_directly(caplog) -> None:
    sess = FakeSession(resp=reply(APPROVE))
    with caplog.at_level(logging.INFO, logger="risk_officer"):
        r = ro.review_trade(order(), snapshot(), account(), session=sess)
    assert r.ok is True and r.provider == "ollama"
    assert "no FEATHERLESS_API_KEY configured" in caplog.text
    assert len(sess.calls) == 1


def test_ollama_only_verdict_forms() -> None:
    for text, expected in ((APPROVE, True), (VETO, False)):
        r = ro.review_trade(
            order(), snapshot(), account(), session=FakeSession(resp=reply(text))
        )
        assert r.ok is True and r.approved is expected and r.provider == "ollama"


# --------------------------------------------------------------------------- #
# Both providers fail -> fail-safe VETO
# --------------------------------------------------------------------------- #
def test_both_providers_fail_vetoes() -> None:
    fl = FakeFeatherless(exc=RuntimeError("featherless 503"))
    sess = FakeSession(exc=requests.ConnectionError("connection refused"))
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.approved is False and r.ok is False
    assert r.provider == "none"
    assert "featherless 503" in r.error
    assert "ConnectionError" in r.error and "connection refused" in r.error


def test_featherless_fails_ollama_unparseable_vetoes() -> None:
    fl = FakeFeatherless(exc=requests.Timeout("timed out"))
    sess = FakeSession(resp=reply("no verdict here, just noise"))
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.approved is False and r.ok is False and r.provider == "none"
    assert "unparseable" in r.error
    assert r.raw_response.startswith("no verdict here")   # last raw kept for evidence


def test_no_key_and_ollama_down_vetoes() -> None:
    sess = FakeSession(exc=requests.ConnectionError("refused"))
    r = ro.review_trade(order(), snapshot(), account(), session=sess)
    assert r.approved is False and r.ok is False
    assert "no FEATHERLESS_API_KEY configured" in r.error
    assert "ConnectionError" in r.error


def test_ollama_http_error_after_featherless_fails_vetoes() -> None:
    fl = FakeFeatherless(exc=RuntimeError("boom"))
    sess = FakeSession(resp=FakeResp(status=500, payload={"error": "model not found"}))
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.approved is False and r.ok is False
    assert "HTTPError" in r.error


def test_ollama_bad_json_after_featherless_fails_vetoes() -> None:
    class BadJson(FakeResp):
        def json(self):
            raise ValueError("no json could be decoded")

    fl = FakeFeatherless(exc=RuntimeError("boom"))
    sess = FakeSession(resp=BadJson())
    r = ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.approved is False and r.ok is False


# --------------------------------------------------------------------------- #
# Prompt carries the required context (same prompt for both providers)
# --------------------------------------------------------------------------- #
def test_prompt_contains_regime_spread_and_exposure_featherless() -> None:
    fl = FakeFeatherless(content="VERDICT: APPROVE\nTHESIS: fine.")
    ro.review_trade(
        order(), snapshot(iv_rv_spread=0.077), account(current=97_500.0),
        featherless_client=fl,
    )
    prompt = fl.calls[0]["messages"][0]["content"]
    assert "IV - RV spread  0.077" in prompt
    assert "IV regime       hackathon_static -> ATM IV 0.220 > 0.15" in prompt
    assert "max loss        $840" in prompt        # (4.0-1.2)*100*3
    assert "% of equity" in prompt
    assert "open positions  0" in prompt
    assert "total drawdown  $2,500" in prompt      # 100k start - 97.5k now


def test_prompt_identical_across_providers() -> None:
    fl = FakeFeatherless(exc=RuntimeError("down"))
    sess = FakeSession(resp=reply(APPROVE))
    ro.review_trade(order(), snapshot(), account(), featherless_client=fl, session=sess)
    fl_prompt = fl.calls[0]["messages"][0]["content"]
    ollama_prompt = sess.calls[0]["json"]["prompt"]
    assert fl_prompt == ollama_prompt
    assert sess.calls[0]["json"]["stream"] is False


def test_prompt_handles_missing_snapshot_fields() -> None:
    fl = FakeFeatherless(content="VERDICT: VETO\nTHESIS: no data.")
    thin = {"iv_rv_spread": None}  # no iv_regime, no atm_iv, etc.
    r = ro.review_trade(order(), thin, account(), featherless_client=fl)
    assert r.approved is False
    assert "IV regime       unknown -> n/a" in fl.calls[0]["messages"][0]["content"]


# --------------------------------------------------------------------------- #
# Prompt carries the MACRO CONTEXT section
# --------------------------------------------------------------------------- #
def test_prompt_has_macro_context_section_with_the_supplied_string() -> None:
    fl = FakeFeatherless(content="VERDICT: APPROVE\nTHESIS: ok.")
    ctx = "Macro: HIGH-IMPACT EVENT TODAY -> FOMC rate decision (2026-09-16) | VIX: 22.1 (VIXY proxy, +14.0% 5d; elevated / possibly spiking) | News SPY: rout | RSI SPY: 28.0 (oversold)"
    ro.review_trade(order(), snapshot(market_context=ctx), account(), featherless_client=fl)
    prompt = fl.calls[0]["messages"][0]["content"]
    assert "### MACRO CONTEXT" in prompt
    assert ctx in prompt
    # the standing instruction about VIX spikes / Red-Folder events / RSI
    assert "VETO" in prompt and "Red Folder" in prompt or "Red-Folder" in prompt
    assert "overbought" in prompt.lower() and "oversold" in prompt.lower()


def test_prompt_macro_context_fails_safe_to_no_context_available() -> None:
    fl = FakeFeatherless(content="VERDICT: VETO\nTHESIS: no ctx.")
    ro.review_trade(order(), snapshot(), account(), featherless_client=fl)  # no market_context key
    prompt = fl.calls[0]["messages"][0]["content"]
    assert "### MACRO CONTEXT" in prompt
    assert "No Context Available" in prompt


def test_prompt_has_quant_clarification_on_contango_and_neutral_rsi() -> None:
    prompt = ro.build_prompt(order(), snapshot(), account())
    assert "### QUANT CLARIFICATION" in prompt
    # contango = normal, not a veto trigger; only backwardation panics
    assert "CONTANGO" in prompt and "BACKWARDATION" in prompt
    assert "NOT a reason to veto" in prompt
    # neutral RSI 40-60 is good for range-bound premium selling
    assert "40-60" in prompt
    assert "IDEAL for range-bound" in prompt


def test_quant_clarification_reaches_the_judge_prompt() -> None:
    prompt = ro.build_prompt(
        order(), snapshot(), account(),
        bull_case="VERDICT: APPROVE\nTHESIS: rich premium.",
        bear_case="VERDICT: VETO\nTHESIS: contango is stress.",
    )
    assert "### QUANT CLARIFICATION" in prompt and "### DEBATE" in prompt


# --------------------------------------------------------------------------- #
# Logging = evidence trail
# --------------------------------------------------------------------------- #
def test_logs_prompt_and_featherless_raw_response(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="risk_officer"):
        ro.review_trade(
            order(), snapshot(), account(),
            featherless_client=FakeFeatherless(
                content="VERDICT: APPROVE\nTHESIS: Rich IV, light book, go."
            ),
        )
    assert "--- prompt ---" in caplog.text
    assert "featherless" in caplog.text
    assert "verdict=APPROVE" in caplog.text
    assert "Rich IV, light book, go." in caplog.text  # raw response logged verbatim


def test_logs_fallback_from_featherless_to_ollama(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="risk_officer"):
        ro.review_trade(
            order(), snapshot(), account(),
            featherless_client=FakeFeatherless(exc=requests.ConnectionError("refused")),
            session=FakeSession(resp=reply(APPROVE)),
        )
    assert "featherless FAILED" in caplog.text
    assert "falling back to ollama" in caplog.text


def test_logs_both_failed_and_veto(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="risk_officer"):
        ro.review_trade(
            order(), snapshot(), account(),
            featherless_client=FakeFeatherless(exc=RuntimeError("503")),
            session=FakeSession(exc=requests.ConnectionError("refused")),
        )
    assert "NO PROVIDER SUCCEEDED" in caplog.text
    assert "VETO" in caplog.text


def test_logs_unparseable_at_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="risk_officer"):
        ro.review_trade(
            order(), snapshot(), account(),
            featherless_client=FakeFeatherless(exc=RuntimeError("down")),
            session=FakeSession(resp=reply("maybe? not sure")),
        )
    assert "UNPARSEABLE" in caplog.text
    assert "maybe? not sure" in caplog.text


# --------------------------------------------------------------------------- #
# OfficerReview.describe
# --------------------------------------------------------------------------- #
def test_describe_marks_fail_safe() -> None:
    ok = ro.OfficerReview(True, "looks good", "m", ok=True, provider="featherless")
    bad = ro.OfficerReview(False, "broke", "m", ok=False, error="boom", provider="none")
    assert "APPROVE" in ok.describe() and "FAIL-SAFE" not in ok.describe()
    assert "via featherless" in ok.describe()
    assert "VETO" in bad.describe() and "FAIL-SAFE" in bad.describe()


# --------------------------------------------------------------------------- #
# warm_up (Ollama fallback only)
# --------------------------------------------------------------------------- #
def test_warm_up_sends_one_token_request_and_returns_true() -> None:
    sess = FakeSession(resp=reply("ready"))
    assert ro.warm_up(session=sess, model="testmodel") is True

    body = sess.calls[0]["json"]
    assert body["model"] == "testmodel"
    assert body["stream"] is False
    assert body["options"] == {"num_predict": 1}
    assert sess.calls[0]["url"].endswith("/api/generate")


def test_warm_up_returns_false_on_failure_without_raising() -> None:
    sess = FakeSession(exc=requests.ConnectionError("connection refused"))
    assert ro.warm_up(session=sess) is False


# --------------------------------------------------------------------------- #
# Multi-agent debate (Bull / Bear / Judge)
# --------------------------------------------------------------------------- #
class SeqCompletions:
    """Returns the next content in a list on each .create() call."""

    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = []

    def create(self, *, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages})
        c = self._contents[len(self.calls) - 1] if len(self.calls) <= len(self._contents) else ""
        if isinstance(c, Exception):
            raise c
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c))])


class SeqFeatherless:
    def __init__(self, contents):
        self._completions = SeqCompletions(contents)
        self.chat = SimpleNamespace(completions=self._completions)

    @property
    def calls(self):
        return self._completions.calls


def test_debate_runs_bull_bear_judge_and_the_judge_decides() -> None:
    fl = SeqFeatherless([
        "The premium is rich and the book is empty — strong risk/reward.",   # bull
        "A CPI print lands in 36h and RSI is stretched — tail risk is real.",  # bear
        "VERDICT: APPROVE\nTHESIS: On balance the edge outweighs the event risk.",  # judge
    ])
    r = ro.debate_review(order(), snapshot(), account(), featherless_client=fl)
    assert len(fl.calls) == 3
    assert r.approved is True and r.ok is True
    assert r.provider.startswith("debate")
    assert r.debate["bull"].startswith("The premium is rich")
    assert r.debate["bear"].startswith("A CPI print")
    assert "APPROVE" in r.debate["judge_raw"]
    t = r.transcript()
    assert "BULL" in t and "BEAR" in t and "JUDGE" in t


def test_debate_judge_veto_is_respected() -> None:
    fl = SeqFeatherless([
        "bull case", "bear case",
        "VERDICT: VETO\nTHESIS: Macro danger dominates; stand aside.",
    ])
    r = ro.debate_review(order(), snapshot(), account(), featherless_client=fl)
    assert r.approved is False and r.ok is True


def test_debate_tolerates_a_failed_advocate() -> None:
    fl = SeqFeatherless([
        RuntimeError("bull call 500"),                       # bull fails
        "bear says stand aside",
        "VERDICT: APPROVE\nTHESIS: bear concerns look overstated.",
    ])
    # ollama is the per-call fallback; make it fail too so the bull truly degrades
    sess = FakeSession(exc=requests.ConnectionError("no ollama"))
    r = ro.debate_review(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.ok is True and r.approved is True
    assert "unavailable" in r.debate["bull"].lower()


def test_debate_judge_failure_on_both_providers_is_fail_safe_veto() -> None:
    fl = SeqFeatherless([
        "bull case", "bear case", RuntimeError("judge call 503"),
    ])
    sess = FakeSession(exc=requests.ConnectionError("no ollama"))
    r = ro.debate_review(order(), snapshot(), account(), featherless_client=fl, session=sess)
    assert r.approved is False and r.ok is False
    assert r.provider == "none"
    assert r.debate is not None                    # transcript still captured


def test_judge_prompt_carries_the_debate_and_lessons() -> None:
    fl = SeqFeatherless([
        "bull", "bear", "VERDICT: VETO\nTHESIS: no.",
    ])
    ro.debate_review(
        order(), snapshot(), account(), featherless_client=fl,
        lessons=["Sold too close to a CPI print; avoid event weeks."],
    )
    judge_prompt = fl.calls[2]["messages"][0]["content"]
    assert "### DEBATE" in judge_prompt
    assert "bull" in judge_prompt and "bear" in judge_prompt
    assert "### LESSONS LEARNED" in judge_prompt
    assert "CPI print" in judge_prompt


def test_load_lessons_returns_empty_list_when_file_missing(tmp_path) -> None:
    assert ro.load_lessons(path=str(tmp_path / "nope.json")) == []


# --------------------------------------------------------------------------- #
# Self-correction loop — post_trade_analysis + lessons_learned.json
# --------------------------------------------------------------------------- #
_CLOSED = {
    "kind": "closed", "at": "2026-09-01T15:55:00-04:00", "id": "QQQ-1",
    "symbol": "QQQ", "structure": "iron_condor", "reason": "stop-loss",
    "pnl": -412.0, "quantity": 3,
}


def test_save_lesson_appends_caps_and_round_trips(tmp_path) -> None:
    p = str(tmp_path / "lessons_learned.json")
    for i in range(5):
        ro.save_lesson(f"lesson number {i}", closed_event=_CLOSED, path=p, max_keep=3)
    lessons = ro.load_lessons(path=p)
    assert lessons == ["lesson number 2", "lesson number 3", "lesson number 4"]  # capped to last 3


def test_post_trade_analysis_writes_a_lesson_from_the_llm(tmp_path) -> None:
    p = str(tmp_path / "lessons_learned.json")
    fl = FakeFeatherless(content="LESSON: Sold premium into a stop-out; tighten the "
                         "delta when RSI is mid-range and IV is only borderline rich.")
    out = ro.post_trade_analysis(_CLOSED, path=p, featherless_client=fl)
    assert out and "tighten the delta" in out
    assert ro.load_lessons(path=p)[-1] == out
    prompt = fl.calls[0]["messages"][0]["content"]
    assert "QQQ" in prompt and "stop-loss" in prompt and "-412" in prompt


def test_post_trade_analysis_is_non_fatal_when_the_llm_is_down(tmp_path) -> None:
    p = str(tmp_path / "lessons_learned.json")
    fl = FakeFeatherless(exc=RuntimeError("503"))
    sess = FakeSession(exc=requests.ConnectionError("no ollama"))
    out = ro.post_trade_analysis(_CLOSED, path=p, featherless_client=fl, session=sess)
    assert out is None
    assert ro.load_lessons(path=p) == []            # nothing written


def test_warm_up_returns_false_on_http_error() -> None:
    sess = FakeSession(resp=FakeResp(status=404, payload={"error": "model not found"}))
    assert ro.warm_up(session=sess) is False


def test_warm_up_logs_outcome(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="risk_officer"):
        ro.warm_up(session=FakeSession(resp=reply("ready")))
    assert "warm-up" in caplog.text and "resident" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="risk_officer"):
        ro.warm_up(session=FakeSession(exc=requests.Timeout("timed out")))
    assert "warm-up FAILED" in caplog.text


# =========================================================================== #
# Step 5 — recent headlines + intraday realized vol reach the judge
# =========================================================================== #
def test_prompt_lists_recent_headlines_and_asks_for_veto_worthy_ones() -> None:
    prompt = ro.build_prompt(
        order(),
        snapshot(symbol="GLD", recent_headlines=[
            "Gold rips to a record as the dollar slides",
            "Miner halts output after regulatory action",
        ]),
        account(),
    )
    assert "RECENT HEADLINES" in prompt
    assert "Gold rips to a record as the dollar slides" in prompt
    assert "Miner halts output after regulatory action" in prompt
    assert "veto" in prompt.lower()


def test_prompt_handles_no_headlines_without_pretending_there_are_some() -> None:
    prompt = ro.build_prompt(order(), snapshot(recent_headlines=[]), account())
    assert "RECENT HEADLINES" in prompt
    assert "none retrieved" in prompt.lower()


def test_prompt_reports_intraday_realized_vol_as_ungated_context() -> None:
    prompt = ro.build_prompt(order(), snapshot(intraday_rv=0.1834), account())
    assert "0.1834" in prompt
    assert "not a gate" in prompt.lower()


def test_prompt_intraday_rv_absent_is_labelled_unavailable() -> None:
    prompt = ro.build_prompt(order(), snapshot(intraday_rv=None), account())
    assert "intraday" in prompt.lower() and "n/a" in prompt.lower()
