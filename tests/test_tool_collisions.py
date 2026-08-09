"""Composite tool-name collisions are deterministic and observable."""
from __future__ import annotations

import logging

import pytest

from looplab.agents.tool_loop import CompositeTools, drive_tool_loop
from looplab.tools._base import fn_spec
from looplab.tools.mcp_tools import McpTools, _prefixed


class _First:
    def specs(self):
        return [fn_spec("same_name", "first", {})]

    def execute(self, name, args):
        return "first"


class _Second:
    def specs(self):
        return [fn_spec("same_name", "second", {})]

    def execute(self, name, args):
        return "second"


def test_collision_keeps_legacy_first_provider_but_is_visible(caplog):
    with caplog.at_level(logging.WARNING, logger="looplab.agents.tool_loop"):
        tools = CompositeTools([_First(), _Second()])

    assert tools.execute("same_name", {}) == "first"
    assert tools.collisions == [("same_name", "_First", "_Second")]
    assert "duplicate tool name 'same_name'" in caplog.text
    assert len(tools.specs()) == 1


def test_strict_collision_mode_fails_at_composition_boundary():
    with pytest.raises(ValueError, match="duplicate tool name 'same_name'"):
        CompositeTools([_First(), _Second()], strict_collisions=True)


class _ReservedProvider:
    def __init__(self):
        self.executed = []

    def specs(self):
        return [
            fn_spec("finish", "provider must not replace emit", {}),
            fn_spec("update_plan", "provider must not replace self-plan", {}),
            fn_spec("read", "ordinary provider tool", {}),
        ]

    def execute(self, name, args):
        self.executed.append(name)
        return "provider result"


class _EmitClient:
    def __init__(self):
        self.specs = []

    def chat(self, messages, specs, tool_choice="auto"):
        self.specs = specs
        return {"tool_calls": [{
            "id": "done",
            "function": {"name": "finish", "arguments": '{"answer":"ok"}'},
        }]}


def test_loop_owned_names_shadow_provider_specs_visibly(caplog):
    provider = _ReservedProvider()
    client = _EmitClient()
    emit = fn_spec("finish", "internal final answer", {
        "answer": {"type": "string"},
    }, ["answer"])

    with caplog.at_level(logging.WARNING, logger="looplab.agents.tool_loop"):
        result = drive_tool_loop(
            client, provider, [{"role": "user", "content": "go"}], emit,
            self_plan=True, max_turns=1, finalize=lambda args: args["answer"],
            fallback=lambda _messages: "fallback",
        )

    names = [(spec.get("function") or {}).get("name") for spec in client.specs]
    assert names == ["read", "finish", "update_plan"]
    assert len(names) == len(set(names))
    assert result == "ok" and provider.executed == []
    assert "collides with loop-owned control tool" in caplog.text


def test_provider_update_plan_remains_available_when_self_plan_is_off():
    provider = _ReservedProvider()
    client = _EmitClient()
    emit = fn_spec("finish", "internal final answer", {})

    drive_tool_loop(
        client, provider, [], emit, self_plan=False, max_turns=1,
        finalize=lambda _args: "ok", fallback=lambda _messages: "fallback",
    )

    names = [(spec.get("function") or {}).get("name") for spec in client.specs]
    assert names == ["update_plan", "read", "finish"]


class _McpCollisionServer:
    def __init__(self, name, tool, description, result):
        self.name = name
        self.tool = tool
        self.description = description
        self.result = result

    def tools(self):
        return [{
            "name": self.tool,
            "description": self.description,
            "input_schema": {"type": "object", "properties": {}},
        }]

    def call(self, tool, args):
        assert tool == self.tool
        return self.result


def test_mcp_internal_prefix_aliases_receive_distinct_schema_route_names(caplog):
    """Delimiter aliases are encoded injectively, so both MCP effects remain reachable."""
    first = _McpCollisionServer("a__b", "c", "first safe schema", "first result")
    second = _McpCollisionServer("a", "b__c", "second effect", "second result")

    with caplog.at_level(logging.WARNING, logger="looplab.tools.mcp_tools"):
        mcp = McpTools([first, second])

    first_name = _prefixed("a__b", "c")
    second_name = _prefixed("a", "b__c")
    assert first_name != second_name
    assert [spec["function"]["description"] for spec in mcp.specs()] == [
        "first safe schema", "second effect"]
    assert mcp.execute(first_name, {}) == "first result"
    assert mcp.execute(second_name, {}) == "second result"
    assert mcp.collisions == []

    # The outer composition boundary sees two coherent, provider-safe schemas.
    composite = CompositeTools([mcp])
    assert composite.collisions == []
    assert composite.execute(first_name, {}) == "first result"
    assert composite.execute(second_name, {}) == "second result"


def test_mcp_malformed_spec_cannot_reserve_a_route_and_shadow_valid_alias():
    """Spec conversion fails before either contract index is published."""
    malformed = _McpCollisionServer("a__b", "c", 7, "must stay unreachable")
    valid = _McpCollisionServer("a", "b__c", "valid schema", "valid result")

    mcp = McpTools([malformed, valid])

    name = _prefixed("a", "b__c")
    assert [spec["function"]["description"] for spec in mcp.specs()] == ["valid schema"]
    assert mcp.execute(name, {}) == "valid result"
    assert mcp.collisions == []


class _McpInventoryServer:
    name = "unsafe server.with spaces"

    def tools(self):
        return [
            {"name": "bad-schema", "description": "bad", "input_schema": "not-an-object"},
            {"name": "tool name." + ("x" * 100), "description": "safe effect",
             "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}},
        ]

    def call(self, tool, args):
        return f"{tool}:{args.get('x')}"


def test_mcp_rejects_only_malformed_schema_and_encodes_unsafe_long_name(caplog):
    server = _McpInventoryServer()
    with caplog.at_level(logging.WARNING, logger="looplab.tools.mcp_tools"):
        mcp = McpTools([server])

    assert len(mcp.specs()) == 1
    name = mcp.specs()[0]["function"]["name"]
    assert len(name) <= 64
    assert name.startswith("mcp__")
    assert name.replace("_", "").replace("-", "").isalnum()
    assert mcp.execute(name, {"x": "ok"}).endswith(":ok")
    assert len(mcp.rejections) == 1
    assert "input_schema is not a JSON Schema object" in mcp.rejections[0][1]
    assert "rejecting malformed MCP tool" in caplog.text
