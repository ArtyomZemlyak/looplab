"""A turn still generating past the session's wall clock is cancelled and the loop salvages.

Measured 2026-09-25 on MiniOneRec inf12: six of nine Developer plan sessions overran their 2400 s
budget by 1.5-27 minutes (~79 min in all) — the budget was read only between turns — one of them on
a single 221k-token turn that ran 30 minutes and returned nothing.
"""
from __future__ import annotations

import time

import looplab.agents.tool_loop as tool_loop
from looplab.core.errors import LLMCancelled
from looplab.core.llm_transient import request_cancelled
from looplab.tools.clock import ClockTools

_EMIT_SPEC = {"type": "function", "function": {
    "name": "answer", "description": "Answer.",
    "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}}


class _RunawayClient:
    """Its first turn 'thinks' until cancelled; any later call (the salvage) answers at once."""

    model = "m"

    def __init__(self):
        self.calls = 0
        self.cancelled_after = None

    def chat(self, messages, tool_specs, tool_choice="auto", **kw):
        self.calls += 1
        if self.calls == 1:
            started = time.monotonic()
            while time.monotonic() - started < 10:
                if request_cancelled():
                    self.cancelled_after = time.monotonic() - started
                    raise LLMCancelled("cancelled")
                time.sleep(0.01)
            raise AssertionError("the runaway turn was never cancelled")
        return {"tool_calls": [{"id": "e", "type": "function",
                                "function": {"name": "answer", "arguments": '{"reply": "salvaged"}'}}]}

    def complete_tool(self, messages, schema):
        """The forced emit the salvage makes — outside the cancelled turn, so it is not cancelled."""
        assert not request_cancelled()
        return {"reply": "salvaged"}


def _drive(client, **kw):
    budgets: list = []
    out = tool_loop.drive_tool_loop(
        client, ClockTools(), [{"role": "user", "content": "go"}], _EMIT_SPEC,
        finalize=lambda args: args.get("reply", ""), fallback=lambda messages: "",
        on_budget=lambda info: budgets.append(info.get("kind")), **kw)
    return out, budgets


def test_a_runaway_turn_is_cancelled_at_the_wall_and_the_session_salvages(monkeypatch):
    monkeypatch.setattr(tool_loop, "_turn_deadline_grace", lambda _budget: 0.2)
    client = _RunawayClient()
    out, budgets = _drive(client, time_budget_s=0.3)
    assert client.cancelled_after is not None and client.cancelled_after < 2.0
    assert out == "salvaged" and "time" in budgets


def test_without_a_wall_nothing_is_cancelled(monkeypatch):
    class _Quick(_RunawayClient):
        def chat(self, messages, tool_specs, tool_choice="auto", **kw):
            assert not request_cancelled()
            return {"tool_calls": [{"id": "e", "type": "function",
                                    "function": {"name": "answer", "arguments": '{"reply": "ok"}'}}]}
    out, _ = _drive(_Quick())
    assert out == "ok"


def test_the_grace_is_a_tenth_of_the_budget_never_under_two_minutes():
    assert tool_loop._turn_deadline_grace(2400) == 240.0
    assert tool_loop._turn_deadline_grace(300) == 120.0
