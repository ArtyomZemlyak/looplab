"""Live MCP transport (optional): connect to an MCP server with the official ``mcp`` Python SDK and
expose a small synchronous handle (`.name`, `.tools()`, `.call()`) for `mcp_tools.McpTools`.

The SDK is async; the assistant's tool loop is synchronous. Each server runs its OWN asyncio event
loop in a daemon thread and keeps the session open for the process lifetime; the sync methods submit
coroutines to that loop with `run_coroutine_threadsafe`. Importing this module fails cleanly when
``mcp`` isn't installed (McpTools.from_config catches that and stays inert).
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import json
import os
import threading
import time
from typing import Optional

from mcp import ClientSession, StdioServerParameters  # noqa: F401  (import-fails -> MCP inert)
from mcp.client.stdio import stdio_client

from looplab.tools._base import ToolResult


class _ServerHandle:
    def __init__(self, name: str, opener):
        self.name = name
        self._opener = opener            # async context-manager factory -> (read, write)
        self._loop = asyncio.new_event_loop()
        self._session: Optional[ClientSession] = None
        self._tools_cache: list = []
        self._ready = threading.Event()
        self._err: Optional[Exception] = None
        self._abandoned = False
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(timeout=30) or self._err:
            # A >30s startup TIMEOUT (or a boot error) makes __init__ raise and the caller discard this
            # handle. Mark it ABANDONED so _serve unwinds a slow-but-successful boot's session/CM in-task
            # instead of run_forever()-leaking the thread/loop/subprocess. `_serve` re-reads this flag
            # AFTER `await self._boot()`, so it sees the write in the common case; at the exact 30s
            # boundary (boot finishing within microseconds of the wait timeout) the observation is
            # best-effort — worst case is the pre-existing run_forever leak, never a crash.
            self._abandoned = True
            raise RuntimeError(f"MCP server {name!r} failed to start: {self._err}")

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            # ONE coroutine drives the whole lifecycle (boot → keep-alive OR abandon-unwind), so the
            # session/CM __aexit__ runs in the SAME asyncio Task that entered them. A second
            # run_until_complete(_shutdown()) wraps _shutdown in a DIFFERENT Task, and the anyio
            # task-group / cancel-scope CMs (stdio_client / streamablehttp_client) then raise
            # "Attempted to exit cancel scope in a different task" — leaving the stdio subprocess
            # un-reaped. run_until_complete still pumps the loop, so call()'s scheduled coroutines run.
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self):
        await self._boot()
        if self._err is not None or self._session is None:
            return                      # boot FAILED — _boot already unwound its CMs in-task; nothing to keep
        if self._abandoned:
            # A >30s startup TIMEOUT made __init__ raise and set _abandoned, but boot then SUCCEEDED —
            # reap the session/subprocess IN THIS task (same one that entered the CMs) instead of
            # run_forever()-leaking the thread/loop/subprocess.
            await self._shutdown()
            return
        # Success: keep this task (and the loop) alive for the session's lifetime so call() can schedule
        # _call coroutines on the loop. Never resolves — the daemon thread lives to process end.
        await asyncio.Event().wait()

    async def _shutdown(self):
        """Unwind the successfully-entered CMs of an ABANDONED handle, in the loop's own task (anyio
        cancel scopes must exit in the task that entered them)."""
        for cm in (self._session_cm, self._cm):
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    async def _boot(self):
        self._cm = None
        self._session_cm = None
        try:
            self._cm = self._opener()
            read, write = await self._cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
            listed = await self._session.list_tools()
            self._tools_cache = [
                {"name": t.name, "description": t.description or "",
                 "input_schema": (getattr(t, "inputSchema", None)
                                  or getattr(t, "input_schema", None)
                                  or {"type": "object", "properties": {}}),
                 "output_schema": (getattr(t, "outputSchema", None)
                                   or getattr(t, "output_schema", None))}
                for t in listed.tools]
        except Exception as e:  # noqa: BLE001
            self._err = e
            # Unwind the partially-entered context managers HERE — inside _boot's own task. The
            # stdio/HTTP CMs are anyio cancel-scope / task-group based, so their __aexit__ MUST run in
            # the SAME task that entered them; exiting from a separate run_until_complete task raises
            # "Attempted to exit cancel scope in a different task" and leaves the subprocess un-reaped.
            for cm in (self._session_cm, self._cm):
                if cm is not None:
                    try:
                        await cm.__aexit__(type(e), e, e.__traceback__)
                    except Exception:  # noqa: BLE001 - best-effort cleanup of a failed boot
                        pass
            self._session = None
        finally:
            self._ready.set()

    def tools(self) -> list:
        return list(self._tools_cache)

    def call(self, tool: str, args: dict, *, cancel_check=None) -> ToolResult:
        fut = asyncio.run_coroutine_threadsafe(self._call(tool, args), self._loop)
        deadline = time.monotonic() + 120.0
        while True:
            if cancel_check is not None:
                try:
                    cancelled = bool(cancel_check())
                except Exception:  # noqa: BLE001 - cancellation hint may not break transport
                    cancelled = False
                if cancelled:
                    # Cancelling the submitted coroutine propagates into ClientSession.call_tool;
                    # the SDK can then emit its protocol cancellation instead of leaving an orphaned
                    # server task behind after the local agent turn has stopped.
                    fut.cancel()
                    raise InterruptedError("MCP call cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fut.cancel()
                raise TimeoutError("MCP call timed out after 120 seconds")
            try:
                return fut.result(timeout=min(0.1, remaining))
            except FutureTimeout:
                continue

    async def _call(self, tool: str, args: dict) -> ToolResult:
        res = await self._session.call_tool(tool, args or {})
        parts = []
        for c in getattr(res, "content", []) or []:
            parts.append(getattr(c, "text", None) or str(c))
        structured = getattr(res, "structuredContent", None)
        if structured is None:
            structured = getattr(res, "structured_content", None)
        meta = getattr(res, "_meta", None)
        if meta is None:
            meta = getattr(res, "meta", None)
        text_content = "\n".join(p for p in parts if p)
        if not text_content and structured is not None:
            # The MCP spec asks servers with an output schema to return structuredContent and a
            # backwards-compatible text block, but preserve usefulness when a server supplies only
            # the structured half: the model-facing legacy view must not collapse to "no content".
            try:
                text_content = json.dumps(structured, ensure_ascii=False, sort_keys=True,
                                          allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError):
                text_content = str(structured)
        return ToolResult(
            content=text_content or "(no content)",
            structured=structured,
            is_error=bool(getattr(res, "isError", False) or getattr(res, "is_error", False)),
            provenance={"source": "mcp", "server": self.name, "tool": tool},
            meta=meta)


def connect_server(name: str, spec: dict):
    """Build a live handle from a server config entry (stdio via command/args, or streamable HTTP via
    url). Returns None for a spec shape we don't support."""
    if spec.get("command"):
        from looplab.runtime.sandbox import is_secret_env
        clean_parent = {key: value for key, value in os.environ.items()
                        if not is_secret_env(key, value)}
        child_env = {**clean_parent, **(spec.get("env") or {})}
        params = StdioServerParameters(command=spec["command"], args=spec.get("args") or [],
                                       env=child_env)
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
