"""
mcp_client.py — optional MCP runtime path for Alpaca reads.

The agent can serve two per-cycle reads — the account snapshot and per-symbol
news — either through Alpaca's official MCP server or through ``alpaca-py``
directly. MCP is tried first when a session is available; ``alpaca-py`` is the
always-present fallback. Every call logs which path served it, so the runtime
route is visible in the trail rather than assumed.

Startup (see ``main.startup``) launches::

    uv run --directory C:/alpaca-hackathon/alpaca-mcp-server alpaca-mcp-server

over stdio. If the SDK is missing, the server will not start, or the handshake
times out, ``connect_session`` returns ``None`` and the agent runs entirely on
``alpaca-py`` — the MCP path is an enhancement, never a dependency. Nothing in
the trade path (orders, risk gates, position management) goes through MCP.

The ``mcp`` SDK is asyncio-only while the agent loop is synchronous, so
``_StdioSession`` owns a private event loop on a daemon thread and exposes
blocking wrappers with timeouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

log = logging.getLogger("mcp")

ACCOUNT_TOOL = "get_account_info"
NEWS_TOOL = "get_news"
DEFAULT_CALL_TIMEOUT_S = 20.0
DEFAULT_CONNECT_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------- #
# Parsing helpers (pure)
# --------------------------------------------------------------------------- #
def extract_text(result) -> str:
    """Flatten an MCP tool result into plain text.

    Handles the SDK's ``CallToolResult.content`` list of blocks, a bare string,
    and ``None``. Unknown shapes stringify to ``""`` rather than raising.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if content is None:
        return ""
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def headlines_from_text(text: str) -> list[str]:
    """Pull ``headline`` strings out of an MCP news payload. ``[]`` if the text
    is not JSON or carries no headlines — the caller then falls back."""
    data = _loads(text)
    if isinstance(data, dict):
        data = data.get("news") or data.get("data") or []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict):
            headline = (item.get("headline") or "").strip()
            if headline:
                out.append(headline)
    return out


def account_from_text(text: str) -> dict | None:
    data = _loads(text)
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# stdio session (asyncio SDK behind a blocking facade)
# --------------------------------------------------------------------------- #
def _import_mcp():
    """Isolated so tests can monkeypatch the import failure path."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    return ClientSession, StdioServerParameters, stdio_client


class _StdioSession:
    """Blocking facade over an asyncio MCP ClientSession on a daemon thread."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-loop", daemon=True
        )
        self._thread.start()
        self._session = None
        self._stack = None
        self._tools: list[str] = []
        self.connected = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def open(self, *, command, args, cwd, env=None, timeout) -> bool:
        ClientSession, StdioServerParameters, stdio_client = _import_mcp()
        from contextlib import AsyncExitStack

        async def _open():
            stack = AsyncExitStack()
            params = StdioServerParameters(command=command, args=list(args),
                                           cwd=cwd, env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            return stack, session, [t.name for t in listed.tools]

        self._stack, self._session, self._tools = self._submit(_open(), timeout)
        self.connected = True
        return True

    def list_tools(self) -> list[str]:
        return list(self._tools)

    def call_tool(self, name, args=None, timeout=DEFAULT_CALL_TIMEOUT_S):
        if self._session is None:
            raise RuntimeError("MCP session is not open")
        return self._submit(self._session.call_tool(name, args or {}), timeout)

    def close(self) -> None:
        try:
            if self._stack is not None:
                self._submit(self._stack.aclose(), 10.0)
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
            log.warning("MCP session close failed: %s", exc)
        finally:
            self.connected = False
            self._loop.call_soon_threadsafe(self._loop.stop)


def connect_session(*, command: str, args, cwd: str, env=None,
                    timeout: float = DEFAULT_CONNECT_TIMEOUT_S):
    """Open a stdio MCP session, or return ``None`` if it cannot be had.

    Never raises: a missing SDK, a server that will not start, and a handshake
    timeout all degrade to ``None`` so the caller uses ``alpaca-py``.
    """
    try:
        session = _StdioSession()
    except Exception as exc:  # noqa: BLE001
        log.warning("MCP session could not be created: %s", exc)
        return None
    try:
        session.open(command=command, args=args, cwd=cwd, env=env, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - MCP is optional by design
        log.warning("MCP session unavailable (%s) — falling back to alpaca-py", exc)
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
        return None
    log.info("MCP session connected — %d tool(s) available", len(session.list_tools()))
    return session


# --------------------------------------------------------------------------- #
# Bridge — chooses the path per call and records which one served it
# --------------------------------------------------------------------------- #
class MCPBridge:
    """Serves the account snapshot and news over MCP when possible, else
    ``alpaca-py``. Both fallbacks are injected so this stays offline-testable."""

    def __init__(self, *, session=None, account_fallback, news_fallback,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT_S):
        self.session = session
        self._account_fallback = account_fallback
        self._news_fallback = news_fallback
        self._call_timeout = call_timeout
        self._paths: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self.session is not None and getattr(self.session, "connected", True)

    def describe(self) -> str:
        if self.enabled:
            tools = ", ".join(self.session.list_tools()[:6]) or "none listed"
            return f"MCP session active (tools: {tools}); alpaca-py is the fallback"
        return "MCP session unavailable — every call served by alpaca-py"

    def last_path(self, call: str) -> str:
        """``"mcp"`` or ``"alpaca-py"`` — which route served ``call`` last."""
        return self._paths.get(call, "")

    def _record(self, call: str, path: str) -> None:
        self._paths[call] = path
        log.info("%s served by %s", call, path)

    def _try_tool(self, name, args):
        if not self.enabled:
            return None
        try:
            return extract_text(
                self.session.call_tool(name, args, timeout=self._call_timeout)
            )
        except Exception as exc:  # noqa: BLE001 - any MCP problem -> fallback
            log.warning("MCP %s failed (%s) — falling back to alpaca-py", name, exc)
            return None

    def account_info(self) -> dict:
        text = self._try_tool(ACCOUNT_TOOL, {})
        parsed = account_from_text(text) if text else None
        if parsed is not None:
            self._record("account", "mcp")
            return parsed
        self._record("account", "alpaca-py")
        return self._account_fallback()

    def news(self, symbol: str, limit: int = 5) -> list[str]:
        text = self._try_tool(NEWS_TOOL, {"symbols": symbol, "limit": limit})
        headlines = headlines_from_text(text) if text else []
        if headlines:
            self._record("news", "mcp")
            return headlines[:limit]
        self._record("news", "alpaca-py")
        return list(self._news_fallback(symbol, limit))[:limit]

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass
