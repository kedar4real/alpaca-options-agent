"""Offline tests for alerts.py — Discord notifications must never touch the
trade path: silent when unconfigured, silent when the POST fails."""

from __future__ import annotations

import pytest

from trading_agent import alerts


class _Poster:
    """Records posts; optionally raises to simulate a network failure."""

    def __init__(self, raises=None):
        self.sent: list[tuple[str, str]] = []
        self.raises = raises

    def __call__(self, url, content):
        if self.raises:
            raise self.raises
        self.sent.append((url, content))
        return True


# --------------------------------------------------------------------------- #
# posting
# --------------------------------------------------------------------------- #
def test_notify_is_silent_without_a_webhook(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_DISCORD_WEBHOOK", raising=False)
    poster = _Poster()
    assert alerts.notify("trade_opened", poster=poster, symbol="SPY") is False
    assert poster.sent == []


def test_notify_posts_when_a_webhook_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DISCORD_WEBHOOK", "https://discord.test/hook")
    poster = _Poster()
    assert alerts.notify("trade_opened", poster=poster, symbol="GLD",
                         structure="long_strangle", detail="8x GLD") is True
    (url, content), = poster.sent
    assert url == "https://discord.test/hook"
    assert "GLD" in content and "long_strangle" in content


def test_notify_swallows_a_failing_post(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DISCORD_WEBHOOK", "https://discord.test/hook")
    poster = _Poster(raises=RuntimeError("discord 500"))
    assert alerts.notify("halt", poster=poster, reason="drawdown") is False


def test_notify_ignores_an_unknown_kind(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DISCORD_WEBHOOK", "https://discord.test/hook")
    poster = _Poster()
    assert alerts.notify("not_a_real_kind", poster=poster) is False
    assert poster.sent == []


# --------------------------------------------------------------------------- #
# message formatting — one per documented event
# --------------------------------------------------------------------------- #
def test_trade_opened_message_names_the_structure_and_detail() -> None:
    msg = alerts.format_message("trade_opened", symbol="IWM",
                                structure="long_strangle", detail="16x IWM exp 2026-09-04")
    assert "IWM" in msg and "long_strangle" in msg and "16x IWM" in msg


def test_trade_closed_message_carries_signed_pnl_and_reason() -> None:
    win = alerts.format_message("trade_closed", symbol="SPY", structure="iron_condor",
                                reason="profit-target", pnl=412.5)
    loss = alerts.format_message("trade_closed", symbol="SPY", structure="iron_condor",
                                 reason="stop-loss", pnl=-233.0)
    assert "+$412" in win and "profit-target" in win
    assert "-$233" in loss and "stop-loss" in loss


def test_halt_and_hard_stop_and_error_messages() -> None:
    assert "HALT" in alerts.format_message("halt", reason="5% floor breached").upper()
    hs = alerts.format_message("hard_stop", legs_closed=4, remaining=0, equity=99_450.0)
    assert "HARD STOP" in hs.upper() and "99,450" in hs
    err = alerts.format_message("cycle_error", error="boom")
    assert "boom" in err


def test_messages_stay_within_discord_length_limit() -> None:
    msg = alerts.format_message("cycle_error", error="x" * 5000)
    assert len(msg) <= alerts.MAX_CONTENT


def test_default_poster_sends_an_explicit_user_agent(monkeypatch) -> None:
    """Discord 403s POSTs carrying the stock ``Python-urllib`` User-Agent."""
    captured = {}

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        captured["ct"] = req.get_header("Content-type")
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert alerts._default_poster("https://discord.test/hook", "hi") is True
    assert captured["ua"] and "urllib" not in captured["ua"].lower()
    assert captured["ct"] == "application/json"
