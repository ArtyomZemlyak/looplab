"""Live MCP transport (optional): connect to an MCP server with the official ``mcp`` Python SDK and
expose a small synchronous handle (`.name`, `.tools()`, `.call()`) for `mcp_tools.McpTools`.

The SDK is async; the assistant's tool loop is synchronous. Each server runs its OWN asyncio event
loop in a daemon thread and keeps the session open for the process lifetime; the sync methods submit
coroutines to that loop with `run_coroutine_threadsafe`. Importing this module fails cleanly when
``mcp`` isn't installed (McpTools.from_config catches that and stays inert).
"""
from __future__ import annotations

import asyncio
import threading
from typing import Optional

from mcp import ClientSession, StdioServerParameters  # noqa: F401  (import-fails -> MCP inert)
from mcp.client.stdio import stdio_client


class _ServerHandle:
    def __init__(self, name: str, opener):
        self.name = name
        self._opener = opener            # async context-manager factory -> (read, write)
        self._loop = asyncio.new_event_loop()
        self._session: Optional[ClientSession] = None
        self._tools_cache: list = []
        self._ready = threading.Event()
        self._err: Optional[Exception] = None
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(timeout=30) or self._err:
            raise RuntimeError(f"MCP server {name!r} failed to start: {self._err}")

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._boot())
        self._loop.run_forever()

    async def _boot(self):
        try:
            self._cm = self._opener()
            read, write = await self._cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
            listed = await self._session.list_tools()
            self._tools_cache = [
                {"name": t.name, "description": t.description or "",
                 "input_schema": getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}}
                for t in listed.tools]
        except Exception as e:  # noqa: BLE001
            self._err = e
        finally:
            self._ready.set()

    def tools(self) -> list:
        return list(self._tools_cache)

    def call(self, tool: str, args: dict) -> str:
        fut = asyncio.run_coroutine_threadsafe(self._call(tool, args), self._loop)
        return fut.result(timeout=120)

    async def _call(self, tool: str, args: dict) -> str:
        res = await self._session.call_tool(tool, args or {})
        parts = []
        for c in getattr(res, "content", []) or []:
            parts.append(getattr(c, "text", None) or str(c))
        return "\n".join(p for p in parts if p) or "(no content)"


def connect_server(name: str, spec: dict):
    """Build a live handle from a server config entry (stdio via command/args, or streamable HTTP via
    url). Returns None for a spec shape we don't support."""
    if spec.get("command"):
        params = StdioServerParameters(command=spec["command"], args=spec.get("args") or [],
                                       env=spec.get("env") or None)
        return _ServerHandle(name, lambda: stdio_client(params))
    if spec.get("url"):
        from mcp.client.streamable_http import streamablehttp_client
        url = spec["url"]

        def opener():
            # streamablehttp_client yields (read, write, _get_session_id); adapt to (read, write).
            class _Adapt:
                async def __aenter__(self):
                    self._cm = streamablehttp_client(url, headers=spec.get("headers") or None)
                    r, w, _ = await self._cm.__aenter__()
                    return r, w
                async def __aexit__(self, *a):
                    return await self._cm.__aexit__(*a)
            return _Adapt()
        return _ServerHandle(name, opener)
    return None
