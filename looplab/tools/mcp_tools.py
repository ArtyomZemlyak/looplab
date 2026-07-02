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

import json
import os
from pathlib import Path
from typing import Optional

from looplab.serve.assistant import REPO_ROOT


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
            return str(server.call(tool, args or {}))[:8000]
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
        thread + event loop + subprocess), not on every assistant turn. build_tools calls this."""
        global _CACHED
        if _CACHED is None:
            _CACHED = cls.from_config()
        return _CACHED


_CACHED: Optional["McpTools"] = None
