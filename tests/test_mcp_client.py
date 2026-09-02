"""Offline tests for mcp_client.py.

The MCP path is an *optional* runtime route for the account snapshot and news.
The invariants under test: when a session is available the bridge uses it and
says so; when it is missing or a call fails, the bridge silently falls back to
alpaca-py and says so. Nothing here touches a real subprocess or the network.
"""

from __future__ import annotations

import pytest

from trading_agent import mcp_client as mc


class _FakeSession:
    """Stands in for a connected stdio MCP ClientSession."""

    def __init__(self, results=None, raises=None, tools=("get_account_info", "get_news")):
        self._results = results or {}
        self._raises = raises
        self._tools = list(tools)
        self.calls: list[tuple[str, dict]] = []
        self.connected = True

    def list_tools(self):
        return list(self._tools)

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, dict(args or {})))
        if self._raises:
            raise self._raises
        if name not in self._tools:
            raise KeyError(f"no such tool: {name}")
        return self._results.get(name, "")


def _fallbacks(account=None, news=None, spy_news=("fallback headline",)):
    return dict(
        account_fallback=lambda: account or {"equity": 100_000.0, "source": "alpaca-py"},
        news_fallback=lambda sym, limit: list(news if news is not None else spy_news),
    )


# --------------------------------------------------------------------------- #
# account snapshot
# --------------------------------------------------------------------------- #
def test_account_uses_the_mcp_session_when_connected(caplog) -> None:
    session = _FakeSession(results={"get_account_info": '{"equity": "99450.80"}'})
    bridge = mc.MCPBridge(session=session, **_fallbacks())
    with caplog.at_level("INFO", logger="mcp"):
        out = bridge.account_info()
    assert session.calls[0][0] == "get_account_info"
    assert out["equity"] == "99450.80"
    assert bridge.last_path("account") == "mcp"
    assert "mcp" in caplog.text.lower()


def test_account_falls_back_when_there_is_no_session() -> None:
    bridge = mc.MCPBridge(session=None, **_fallbacks())
    out = bridge.account_info()
    assert out["source"] == "alpaca-py"
    assert bridge.last_path("account") == "alpaca-py"


def test_account_falls_back_when_the_mcp_call_raises() -> None:
    session = _FakeSession(raises=RuntimeError("stdio closed"))
    bridge = mc.MCPBridge(session=session, **_fallbacks())
    out = bridge.account_info()
    assert out["source"] == "alpaca-py"
    assert bridge.last_path("account") == "alpaca-py"


# --------------------------------------------------------------------------- #
# news
# --------------------------------------------------------------------------- #
def test_news_uses_the_mcp_session_and_parses_headlines() -> None:
    payload = '{"news": [{"headline": "Gold rips"}, {"headline": "Fed speaks"}]}'
    session = _FakeSession(results={"get_news": payload})
    bridge = mc.MCPBridge(session=session, **_fallbacks())
    assert bridge.news("GLD", limit=5) == ["Gold rips", "Fed speaks"]
    assert session.calls[0][0] == "get_news"
    assert session.calls[0][1]["symbols"] == "GLD"
    assert bridge.last_path("news") == "mcp"


def test_news_falls_back_when_mcp_returns_nothing_useful() -> None:
    session = _FakeSession(results={"get_news": "not json at all"})
    bridge = mc.MCPBridge(session=session, **_fallbacks(spy_news=("fallback headline",)))
    assert bridge.news("SPY", limit=5) == ["fallback headline"]
    assert bridge.last_path("news") == "alpaca-py"


def test_news_respects_the_limit() -> None:
    payload = '{"news": [{"headline": "h%d"}]}' % 1
    many = '{"news": [' + ",".join(f'{{"headline": "h{i}"}}' for i in range(9)) + ']}'
    session = _FakeSession(results={"get_news": many})
    bridge = mc.MCPBridge(session=session, **_fallbacks())
    assert len(bridge.news("SPY", limit=3)) == 3
    assert payload  # keep the simple payload referenced for readability


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def test_extract_text_handles_the_mcp_content_shapes() -> None:
    class _Block:
        def __init__(self, text):
            self.text = text

    class _Result:
        def __init__(self, blocks):
            self.content = blocks

    assert mc.extract_text(_Result([_Block("hello"), _Block(" world")])) == "hello world"
    assert mc.extract_text("plain string") == "plain string"
    assert mc.extract_text(None) == ""


def test_headlines_from_text_reads_json_or_returns_empty() -> None:
    assert mc.headlines_from_text('{"news":[{"headline":"a"},{"headline":"b"}]}') == ["a", "b"]
    assert mc.headlines_from_text('[{"headline":"a"}]') == ["a"]
    assert mc.headlines_from_text("garbage") == []
    assert mc.headlines_from_text("") == []


# --------------------------------------------------------------------------- #
# connection is optional and never fatal
# --------------------------------------------------------------------------- #
def test_connect_session_returns_none_when_the_sdk_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_import_mcp", lambda: (_ for _ in ()).throw(ImportError("no mcp")))
    assert mc.connect_session(command="uv", args=["run"], cwd=".", timeout=1.0) is None


def test_connect_session_returns_none_on_a_launch_failure(monkeypatch) -> None:
    def boom():
        raise RuntimeError("server would not start")

    monkeypatch.setattr(mc, "_import_mcp", boom)
    assert mc.connect_session(command="uv", args=["run"], cwd=".", timeout=1.0) is None


def test_bridge_reports_disabled_when_constructed_without_a_session() -> None:
    bridge = mc.MCPBridge(session=None, **_fallbacks())
    assert bridge.enabled is False
    assert "alpaca-py" in bridge.describe().lower()


def test_bridge_reports_enabled_with_a_session() -> None:
    bridge = mc.MCPBridge(session=_FakeSession(), **_fallbacks())
    assert bridge.enabled is True
    assert "mcp" in bridge.describe().lower()
