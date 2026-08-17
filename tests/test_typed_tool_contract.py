"""Typed tool contracts remain additive, immutable and fail closed for legacy providers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from looplab.agents.tool_loop import CompositeTools, _run_tool_call
from looplab.tools._base import ToolCapability, ToolResult, fn_spec
from looplab.tools.mcp_tools import GatedMcpTools, McpTools


class _Legacy:
    def specs(self):
        return [fn_spec("read_friendly_name", "legacy", {"q": {"type": "string"}})]

    def execute(self, name, args):
        return "legacy bytes"


class _Typed:
    def __init__(self):
        self.cancel_seen = False

    def specs(self):
        return [fn_spec("inspect", "typed", {})]

    def capabilities(self):
        return [ToolCapability(name="inspect", effect="read", risk="low",
                               idempotency="idempotent", concurrency_safe=True,
                               cancellable=True, approval="never", source="test")]

    def execute_result(self, name, args, *, cancel_check=None):
        self.cancel_seen = bool(cancel_check and cancel_check())
        return ToolResult(content="human view", structured={"rows": [1, 2]},
                          provenance={"source": "fixture"}, receipt={"id": "r1"})

    def execute(self, name, args):
        raise AssertionError("typed dispatch must prefer execute_result")


def test_legacy_tools_are_unknown_instead_of_inferred_safe_from_their_name():
    tools = CompositeTools([_Legacy()])
    cap = tools.capability("read_friendly_name")
    assert cap.effect == "unknown" and cap.risk == "unknown" and cap.approval == "unknown"
    assert cap.source == "legacy:_Legacy"
    assert tools.execute("read_friendly_name", {}) == "legacy bytes"
    assert tools.execute_result("read_friendly_name", {}).content == "legacy bytes"


def test_typed_results_retain_structure_and_the_legacy_string_view():
    provider = _Typed()
    tools = CompositeTools([provider])
    result = tools.execute_result("inspect", {}, cancel_check=lambda: True)
    assert result.content == "human view" and str(result) == "human view"
    assert result.structured["rows"] == (1, 2)
    assert dict(result.receipt) == {"id": "r1"}
    assert provider.cancel_seen is True
    assert len(tools.manifest_hash) == 64
    assert tools.manifest()["tools"][0]["capability"]["effect"] == "read"


def test_manifest_bytes_are_detached_from_provider_owned_spec_dicts():
    tools = CompositeTools([_Typed()])
    digest = tools.manifest_hash
    exposed = tools.specs()[0]
    exposed["function"]["description"] = "mutated after manifest construction"
    assert tools.manifest_hash == digest
    assert tools.manifest()["tools"][0]["spec"]["function"]["description"] == "typed"


def test_contract_and_result_nested_data_are_immutable_copies():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    structured = {"items": [1]}
    cap = ToolCapability(name="x", input_schema=schema)
    result = ToolResult(content="x", structured=structured)
    schema["type"] = "array"
    structured["items"].append(2)
    assert cap.input_schema["type"] == "object"
    assert result.structured["items"] == (1,)
    with pytest.raises(TypeError):
        cap.input_schema["type"] = "array"


def test_the_tool_loop_passes_its_live_cancellation_seam_to_typed_tools():
    provider = _Typed()
    tools = CompositeTools([provider])
    result, note = _run_tool_call(tools, "inspect", {}, repeat_state={},
                                  cancel_check=lambda: True)
    assert result == "human view" and note == "" and provider.cancel_seen is True


class _StructuredMcp:
    name = "fixture"

    def tools(self):
        return [{"name": "lookup", "description": "lookup", "input_schema": {
            "type": "object", "properties": {}}, "output_schema": {
            "type": "object", "properties": {"value": {"type": "integer"}}}}]

    def call(self, tool, args, *, cancel_check=None):
        return ToolResult(content="value=7", structured={"value": 7}, is_error=False,
                          meta={"request_id": "m1"})


def test_mcp_structure_survives_and_the_permission_wrapper_stays_fail_closed():
    inner = McpTools([_StructuredMcp()])
    name = inner.specs()[0]["function"]["name"]
    result = inner.execute_result(name, {})
    assert result.structured["value"] == 7 and result.meta["request_id"] == "m1"
    assert inner.capabilities()[0].effect == "external"
    gated = GatedMcpTools(inner, mode="default", approver=lambda _action: "deny")
    denied = gated.execute_result(name, {})
    assert denied.is_error is True and denied.receipt["approved"] is False
    assert gated.capabilities()[0].approval == "policy"


def test_mcp_structured_only_result_keeps_a_model_facing_json_view():
    pytest.importorskip("mcp")
    from looplab.tools._mcp_transport import _ServerHandle

    class _Session:
        async def call_tool(self, _tool, _args):
            return SimpleNamespace(content=[], structuredContent={"value": 9}, isError=False)

    handle = object.__new__(_ServerHandle)
    handle.name = "fixture"
    handle._session = _Session()
    result = asyncio.run(handle._call("lookup", {}))
    assert result.structured["value"] == 9 and result.content == '{"value":9}'
