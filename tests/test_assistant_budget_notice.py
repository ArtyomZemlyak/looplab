"""A cut-short assistant turn must SAY it was cut short.

The operator's report: "the assistant hangs around 40 tool uses and then some tool use comes back as
the answer". The loop was behaving as designed — on budget exhaustion it salvages one forced emit
from everything it gathered, which rescues an answer instead of discarding the investigation. What it
never did was tell anyone, so a truncated answer arrived looking like a finished one.

`agent_time_budget_s` is UNSET by default, and `serve/assistant.py::run_turn` then applies a
five-minute floor — so on a shared, slow endpoint this is the limit most long turns actually hit.
"""
from __future__ import annotations

import time

from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS
from looplab.agents.tool_loop import _note_budget, drive_tool_loop


def test_the_observer_is_registered_as_an_explicit_only_callback():
    """The registry PARTITIONS the loop's keyword-only parameters. A callback reachable both ways
    raises a duplicate-keyword TypeError that the loop's own containment swallows — the exact defect
    `loop_options.py`'s docstring records, which left the agentic Researcher silently dead."""
    from looplab.agents.loop_options import LOOP_OPTION_FIELDS

    assert "on_budget" in EXPLICIT_ONLY_LOOP_ARGS
    assert "on_budget" not in LOOP_OPTION_FIELDS


def test_a_broken_observer_can_never_break_the_loop_it_observes():
    """This fires on the way to a salvage emit. A raising callback here would turn a rescued answer
    into a crash — the opposite of what the salvage exists for."""
    def boom(_payload):
        raise RuntimeError("observer exploded")

    _note_budget(boom, "time", turns=3, seconds=1.0)      # must not raise
    _note_budget(None, "time", turns=3, seconds=1.0)      # …and absent is fine


def test_the_observer_reports_which_budget_ran_out_and_how_far_it_got():
    seen = {}
    _note_budget(seen.update, "time", turns=41, seconds=302.4567)
    assert seen == {"kind": "time", "turns": 41, "seconds": 302.457}

    seen = {}
    _note_budget(seen.update, "turns", turns=30, seconds=12.0)
    assert seen["kind"] == "turns" and seen["turns"] == 30


class _Client:
    """A model that keeps calling a tool and never emits — the shape of a turn that runs out."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.calls += 1
        time.sleep(0.02)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": "look", "arguments": "{}"}}]}

    def complete_text(self, messages):
        return "text"


class _Tools:
    def specs(self):
        return [{"type": "function", "function": {"name": "look", "description": "look",
                                                  "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return "looked"


def test_a_turn_that_runs_out_of_wall_clock_notifies_before_it_salvages():
    seen = {}
    drive_tool_loop(
        _Client(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        time_budget_s=0.05, finalize=lambda args: "done",
        fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert seen.get("kind") == "time", "the caller must learn the wall clock is what stopped it"
    assert seen["turns"] >= 1 and seen["seconds"] >= 0.05


def test_an_ordinary_turn_reports_nothing():
    """Absent, not `{"kind": None}`: the envelope key is what tells a UI to show the notice at all."""
    class _Emitter(_Client):
        def chat(self, messages, tool_specs, tool_choice="auto"):
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "e1", "type": "function",
                 "function": {"name": "emit", "arguments": '{"reply": "hi"}'}}]}

    seen = {}
    result = drive_tool_loop(
        _Emitter(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        time_budget_s=30.0, finalize=lambda args: args.get("reply"),
        fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert result == "hi"
    assert seen == {}


def test_run_turn_puts_the_notice_in_both_the_envelope_and_the_answer():
    """Two surfaces read this differently — the chat renders the reply text, tooling reads the dict —
    so a notice in only one of them is invisible to half the callers."""
    import inspect

    from looplab.serve import assistant

    source = inspect.getsource(assistant.run_turn)
    assert "on_budget=budget_box.update" in source
    assert '"budget_exhausted": dict(budget_box)' in source
    assert "was cut" in source and "not a finished investigation" in source
    assert "agent_time_budget_s" in source, "the notice must name the knob that raises the limit"
