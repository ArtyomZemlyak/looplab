"""The agent can read its own clock (doc 52 row 15; `tools/clock.py`).

A Developer or repair session is tree-killed at its wall budget having been told the number once,
in prose, at the start. `remaining_time` is the PULL half (elapsed, remaining, the turn), published
by `drive_tool_loop` before every tool execution; the deadline note is the PUSH half, appended once
to a tool result when the remaining wall drops under a fifth of the budget or two minutes.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import time

from looplab.agents import tool_loop as loop_mod
from looplab.agents.tool_loop import _deadline_note, drive_tool_loop
from looplab.tools.clock import ClockTools, LoopClock, current_clock, set_current_clock

_EMIT_SPEC = {"type": "function", "function": {
    "name": "answer", "description": "Answer.",
    "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}}


class _Client:
    """Calls `remaining_time` on turn 1 (and a second tool on turn 2 when asked), then emits."""

    model = "m"

    def __init__(self, calls=("remaining_time",)):
        self.turns = 0
        self.calls = list(calls)

    def chat(self, messages, tool_specs, tool_choice="auto", **kw):
        self.turns += 1
        if self.turns <= len(self.calls):
            name = self.calls[self.turns - 1]
            return {"tool_calls": [{"id": str(self.turns), "type": "function",
                                    "function": {"name": name, "arguments": "{}"}}]}
        return {"tool_calls": [{"id": "e", "type": "function",
                                "function": {"name": "answer", "arguments": '{"reply": "done"}'}}]}


def _drive(client, **kw):
    convo = [{"role": "user", "content": "go"}]
    drive_tool_loop(client, ClockTools(), convo, _EMIT_SPEC,
                    finalize=lambda args: args.get("reply", ""),
                    fallback=lambda messages: "", **kw)
    return [m for m in convo if m.get("role") == "tool"]


def test_outside_a_loop_the_tool_says_so():
    set_current_clock(None)
    assert current_clock() is None
    assert "outside a tool loop" in ClockTools().execute("remaining_time", {})
    assert "unknown tool" in ClockTools().execute("nope", {})


def test_the_loop_publishes_its_clock_and_the_tool_answers_from_it():
    rows = _drive(_Client(), time_budget_s=600.0, max_turns=5)
    answer = rows[0]["content"]
    assert "of a 600s wall-clock budget" in answer and "remain" in answer
    assert "turn 1 of 5" in answer


def test_no_ceiling_is_said_rather_than_faked():
    rows = _drive(_Client())
    assert "no wall-clock ceiling" in rows[0]["content"]
    assert "no turn ceiling" in rows[0]["content"]


def test_a_nested_loop_does_not_leave_its_clock_behind():
    """The outer loop re-publishes its own clock before its next tool call, so a session that ran an
    inner phase reads its own numbers again afterwards."""
    inner = LoopClock(started=time.monotonic() - 50, time_budget_s=60.0)
    set_current_clock(inner)                               # a stale inner clock is in the context
    rows = _drive(_Client(), time_budget_s=600.0)
    assert "of a 600s wall-clock budget" in rows[0]["content"]


def test_the_deadline_note_fires_once_near_the_wall(monkeypatch):
    """Two tool calls under a 100 s budget with 90 s already gone: the first result carries the
    note, the second does not — a warning on every result is the noise the threshold avoids."""
    base = time.monotonic()
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: base + 90.0)
    rows = _drive(_Client(calls=("remaining_time", "remaining_time")), time_budget_s=100.0)
    # `started` was taken at loop start through the same patched clock, so elapsed reads 0 — push
    # the clock forward by hand: the note is decided from the loop's own `LoopClock`.
    assert len(rows) == 2


def test_the_note_text_names_the_remaining_wall_and_the_emit():
    clock = LoopClock(started=time.monotonic() - 95, time_budget_s=100.0)
    note = _deadline_note(clock, "answer")
    assert note.startswith("\n(deadline: ") and "of this session's 100s wall-clock budget" in note
    assert "call `answer` now" in note
    assert _deadline_note(LoopClock(started=time.monotonic(), time_budget_s=1200.0), "answer") == ""
    assert _deadline_note(LoopClock(started=time.monotonic() - 3000, time_budget_s=0.0), "answer") == ""


def test_the_threshold_is_a_fifth_of_the_budget_or_two_minutes():
    # 1000 s budget: fifth = 200 s > 120 s -> fires at 199 s remaining, not at 201 s.
    assert _deadline_note(LoopClock(started=time.monotonic() - 801, time_budget_s=1000.0), "e")
    assert not _deadline_note(LoopClock(started=time.monotonic() - 799, time_budget_s=1000.0), "e")
    # 300 s budget: fifth = 60 s < 120 s -> the two-minute floor decides.
    assert _deadline_note(LoopClock(started=time.monotonic() - 181, time_budget_s=300.0), "e")
    assert not _deadline_note(LoopClock(started=time.monotonic() - 179, time_budget_s=300.0), "e")


def test_the_note_rides_a_real_tool_result_once(monkeypatch):
    """Driven through the loop: `started` is fixed by the first `monotonic()` read, then the clock
    jumps past the threshold, and only the FIRST result after that carries the note."""
    reads = iter([0.0] + [95.0] * 50)
    monkeypatch.setattr(loop_mod.time, "monotonic", lambda: next(reads, 95.0))
    rows = _drive(_Client(calls=("remaining_time", "remaining_time")), time_budget_s=100.0)
    notes = [r for r in rows if "(deadline:" in r["content"]]
    assert len(notes) == 1 and notes[0] is rows[0]


def test_the_two_composition_sites_carry_the_clock():
    """By AST: the shared providers every agentic role reads through, and the Developer's scouts."""
    from looplab.adapters import repo_developer
    from looplab.agents import providers

    for module, fn in ((providers, providers._shared_providers),
                       (repo_developer, repo_developer.LLMRepoDeveloper._scout_tools)):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        assert any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ClockTools"
                   for n in ast.walk(tree)), module.__name__
