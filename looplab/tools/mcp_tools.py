"""MCP client tool provider: expose tools from configured Model Context Protocol servers to the
assistant as ordinary OpenAI functions (named ``mcp__<server>__<tool>``), so the shared tool loop can
call them with no special-casing — provider-neutral by construction.

Config (first found wins): env ``LOOPLAB_MCP_CONFIG`` (path to JSON), env ``LOOPLAB_MCP_SERVERS``
(inline JSON), or ``<repo>/.mcp.json``. Shape mirrors the common ``.mcp.json``::

    {"mcpServers": {"name": {"command": "npx", "args": ["-y", "pkg"]},        # stdio
                    "web":  {"url": "https://host/mcp"}}}                      # streamable HTTP

Degrades gracefully: no config, no ``mcp`` SDK, or a server that won't connect → that server simply
contributes no tools (never raises into the loop). The spec-conversion and call-routing are separated
from the live transport so they are unit-testable with a fake server handle.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Optional

# The LoopLab repo root (…/looplab, two levels above this file) — where the default `.mcp.json`
# lives. Computed locally instead of importing `looplab.serve.assistant.REPO_ROOT` (same value):
# the tools layer must not depend on the serve layer.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _prefixed(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


def load_config() -> dict:
    """Return {server_name: config} from the first configured source, else {}."""
    raw = None
    p = os.environ.get("LOOPLAB_MCP_CONFIG")
    if p and Path(p).is_file():
        raw = Path(p).read_text(encoding="utf-8")
    elif os.environ.get("LOOPLAB_MCP_SERVERS"):
        raw = os.environ["LOOPLAB_MCP_SERVERS"]
    else:
        default = REPO_ROOT / ".mcp.json"
        if default.is_file():
            raw = default.read_text(encoding="utf-8")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    return servers if isinstance(servers, dict) else {}


class McpTools:
    """Aggregate provider over one-or-more connected MCP servers. `servers` is a list of handles, each
    exposing `.name`, `.tools()` -> [{name, description, input_schema}], and `.call(tool, args) -> str`.
    Use `from_config()` for the live path; inject fakes in tests."""

    def __init__(self, servers: Optional[list] = None):
        self.servers = servers or []
        self._route: dict = {}       # prefixed tool name -> (server, tool_name)
        self._specs: list[dict] = []
        for s in self.servers:
            try:
                for t in s.tools():
                    full = _prefixed(s.name, t["name"])
                    self._route[full] = (s, t["name"])
                    self._specs.append({"type": "function", "function": {
                        "name": full, "description": t.get("description", "")[:400],
                        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}})
            except Exception:  # noqa: BLE001 - a flaky server contributes no tools
                continue

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return list(self._specs)

    def execute(self, name: str, args: dict) -> str:
        target = self._route.get(name)
        if not target:
            return f"(unknown tool: {name})"
        server, tool = target
        try:
            # Cap at the loop's RESULT_CAP (the ToolProvider convention: derive budgets FROM it, not a
            # free-standing 8000 that the loop's own 4000 tail-cut always dominates anyway).
            from looplab.tools._base import RESULT_CAP
            return str(server.call(tool, args or {}))[:RESULT_CAP]
        except Exception as e:  # noqa: BLE001 - a tool error is data for the model, never a crash
            return f"(mcp error calling {name}: {e})"

    @classmethod
    def from_config(cls) -> "McpTools":
        cfg = load_config()
        if not cfg:
            return cls([])
        try:
            from looplab.tools._mcp_transport import connect_server   # live SDK path (optional dependency)
        except Exception:  # noqa: BLE001 - no mcp SDK installed -> inert
            return cls([])
        servers = []
        for name, spec in cfg.items():
            try:
                servers.append(connect_server(name, spec))
            except Exception:  # noqa: BLE001 - a server that won't connect is skipped
                continue
        return cls([s for s in servers if s is not None])

    @classmethod
    def cached(cls) -> "McpTools":
        """Process-global instance: connect to each MCP server ONCE (a live server owns a background
        thread + event loop + subprocess), not on every assistant turn. build_tools calls this.

        Double-checked under a lock: two concurrent first turns (two tabs/sessions — the workers are
        plain threads) would otherwise both see `_CACHED is None`, both `from_config()`, and each spawn
        a full set of server handles (thread + loop + subprocess); the loser's set orphans and leaks
        for the process lifetime."""
        global _CACHED
        if _CACHED is None:
            with _CACHE_LOCK:
                if _CACHED is None:
                    _CACHED = cls.from_config()
        return _CACHED


class GatedMcpTools:
    """Wrap `McpTools` so every MCP call passes the assistant's permission policy. An MCP tool is an
    arbitrary EXTERNAL side effect, so CompositeTools dispatching it with NO gate was a bypass
    (arch-review §3 P0-6): a `default`-mode session could fire an MCP mutation with no confirm-card.
    Here each call is treated as a mutating `shell`-class effect — ASK in `default`/`acceptEdits`,
    run inline only in `auto` (the user opted in). Read tools (specs) pass through unchanged; plan
    mode never even builds this wrapper (build_tools drops MCP there, so no stdio server is started
    in a read-only session)."""

    def __init__(self, inner: "McpTools", mode: str, approver=None):
        self._inner = inner
        self._mode = mode
        from looplab.tools.perm_modes import default_approver
        self._approver = approver or default_approver

    def specs(self) -> list[dict]:
        return self._inner.specs()

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def execute(self, name: str, args: dict) -> str:
        from looplab.tools.perm_modes import approval_allows, decide_action
        try:
            args_json = json.dumps(args or {}, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            args_json = "<invalid arguments>"
        from looplab.trust.redact import redact_secrets
        args_digest = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
        action = {"tool": name, "tool_kind": "mcp", "label": f"MCP tool {name}",
                  "verb": f"call MCP tool `{name}`",
                  "preview": redact_secrets(args_json)[:2000], "cwd": "",
                  "scope": {"tool": name, "arguments_digest": args_digest}}
        d = decide_action(self._mode, action)
        if d == "deny":
            return f"(MCP tool {name} is disabled in plan mode. Switch to default/acceptEdits/auto.)"
        if d == "ask":
            if not approval_allows(self._approver(action) or "deny"):
                return f"(declined by the user: MCP tool {name})"
        return self._inner.execute(name, args)


_CACHED: Optional["McpTools"] = None
_CACHE_LOCK = threading.Lock()
