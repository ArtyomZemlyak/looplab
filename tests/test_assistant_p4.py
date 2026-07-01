"""P4: subagents (the `task` tool) and the MCP client provider."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.assistant import run_turn  # noqa: E402
from looplab.mcp_tools import McpTools, load_config, _prefixed  # noqa: E402


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _final(reply):
    return _call("final_answer", {"reply": reply})


class _SubagentFake:
    """Outer: call task(...) then answer. Inner (detected by a marker in the user msg): answer at once."""
    def __init__(self):
        self.n = 0

    def chat(self, messages, tools, tool_choice="auto"):
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        if "SUBTASK-MARKER" in (last_user.get("content") or ""):
            return _final("subagent found: 42")
        self.n += 1
        if self.n == 1:
            return _call("task", {"prompt": "SUBTASK-MARKER research the answer"})
        return _final("outer used the subagent")


def test_subagent_task_delegation(tmp_path):
    client = _SubagentFake()
    res = run_turn(client, tmp_path, [], "find the answer via a subagent", "plan")
    # The outer agent delegated to a subagent (task tool ran an isolated inner turn) and answered.
    assert res["ok"] and res["reply"] == "outer used the subagent"
    assert client.n >= 2      # outer made its task call then its final answer (inner ran in between)


# --- MCP ---------------------------------------------------------------------
class _FakeServer:
    name = "fs"

    def tools(self):
        return [{"name": "echo", "description": "echo x",
                 "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}]

    def call(self, tool, args):
        if tool == "echo":
            return "echoed:" + str(args.get("x", ""))
        raise ValueError("no such tool")


def test_mcp_specs_and_routing():
    m = McpTools([_FakeServer()])
    names = [s["function"]["name"] for s in m.specs()]
    assert names == [_prefixed("fs", "echo")] == ["mcp__fs__echo"]
    assert m.execute("mcp__fs__echo", {"x": "hi"}) == "echoed:hi"
    assert "unknown tool" in m.execute("mcp__fs__missing", {})


def test_mcp_tool_error_is_returned_not_raised():
    class _Boom:
        name = "b"
        def tools(self):
            return [{"name": "t", "description": "", "input_schema": {}}]
        def call(self, tool, args):
            raise RuntimeError("kaboom")
    m = McpTools([_Boom()])
    assert "mcp error" in m.execute("mcp__b__t", {})


def test_mcp_load_config_from_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_MCP_SERVERS", json.dumps({"mcpServers": {"web": {"url": "https://h/mcp"}}}))
    cfg = load_config()
    assert cfg == {"web": {"url": "https://h/mcp"}}


def test_mcp_from_config_inert_without_sdk():
    # No `mcp` SDK installed here -> from_config degrades to no tools (never raises).
    m = McpTools.from_config()
    assert m.specs() == []
