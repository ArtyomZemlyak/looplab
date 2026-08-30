"""A triage cut short by its wall says so on the verdict.

`Settings.triage_time_budget_s`'s comment and the settings-table row BOTH said the loop's time exit
tells the operator the investigation was cut short. It did not: `on_budget` is in
`EXPLICIT_ONLY_LOOP_ARGS`, `unified_agent` passed no observer anywhere, and `_note_budget` returns
immediately without one. So a triage that hit the 1200 s wall force-emitted a verdict — failure kind,
`reason_summary`, the repair directive, `NEVER_SALVAGED_REASONS` gating — whose durable rows carried
no truncation mark at all. The same "computed, named, documented, delivered to nobody" shape
`ffdb34e3` closed for the repair session one day before this wall landed one phase over.

STAMPED BY THE ENGINE, NEVER BY THE WIRE. `_finalize` rebuilds the verdict from the schema properties
alone — the property that makes `TRIAGE_TRANSPORT_FAILURE_KEY` unforgeable — so the cutoff is added
AFTER it, from an observer only this module installs. A model cannot emit it, and its ABSENCE means
the sweep reached a conclusion rather than a wall.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

import inspect

from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS, LOOP_OPTION_FIELDS
from looplab.agents.tool_loop import drive_tool_loop
from looplab.engine.triage import TRIAGE_BUDGET_CUTOFF_KEY, TRIAGE_TRANSPORT_FAILURE_KEY


def _stub_agent(ua):
    """The members `triage_crash` reads BEFORE it reaches `_pilot_emit`. Named from the real method
    rather than guessed — the first cut supplied only the wall and failed on `_pilot_client`."""
    agent = ua.UnifiedAgent.__new__(ua.UnifiedAgent)
    agent._pilot_client = object()          # merely non-None: `_pilot_emit` is stubbed out below
    agent._pilot_tools = None
    agent._triage_time_budget_s = 1200.0
    agent._REPAIR_LOOK_TURNS = 3
    return agent


def test_the_emit_helper_can_carry_an_observer_at_all():
    """Mutation: drop `on_budget` from `_pilot_emit` and the cutoff has no route out of the loop."""
    from looplab.agents.unified_agent import UnifiedAgent

    assert "on_budget" in inspect.signature(UnifiedAgent._pilot_emit).parameters


def test_the_observer_is_passed_EXPLICITLY_and_not_through_the_bundle():
    """`on_budget` is in `EXPLICIT_ONLY_LOOP_ARGS`; a name reachable BOTH ways raises a
    duplicate-keyword TypeError that the loop's containment `except` swallows, silently degrading an
    agentic phase to a non-agentic one — the defect the partition exists to prevent.

    Mutation: fold it into `loop_opts` (or add it to `LOOP_OPTION_FIELDS`)."""
    assert "on_budget" in EXPLICIT_ONLY_LOOP_ARGS and "on_budget" not in LOOP_OPTION_FIELDS

    src = inspect.getsource(
        __import__("looplab.agents.unified_agent", fromlist=["x"]).UnifiedAgent._pilot_emit)
    assert "on_budget=on_budget, **loop_opts" in src, (
        "the observer must ride as an explicit keyword beside the spread, never inside it")


def test_triage_crash_installs_an_observer_and_stamps_what_it_reports():
    """The delivery, driven: a loop that reports a cutoff must leave a mark on the verdict.

    Mutation: keep the observer but never write `TRIAGE_BUDGET_CUTOFF_KEY` — the verdict is byte-
    identical to a complete one, which is the whole defect."""
    from looplab.agents import unified_agent as ua

    seen = {}

    def _fake_pilot_emit(self, messages, emit_spec, finalize, fallback, **kw):
        seen["on_budget"] = kw.get("on_budget")
        assert callable(seen["on_budget"]), "triage_crash must install an observer"
        seen["on_budget"]({"kind": "time_budget", "turns": 206, "seconds": 5281.0,
                           "detail": "the 1200s wall ended the sweep"})
        return {"action": "repair", "rationale": "r", "missing_dependency": ""}

    saved = ua.UnifiedAgent._pilot_emit
    ua.UnifiedAgent._pilot_emit = _fake_pilot_emit
    try:
        agent = _stub_agent(ua)
        out = ua.UnifiedAgent.triage_crash(agent, node=None, error="boom", attempt=1)
    finally:
        ua.UnifiedAgent._pilot_emit = saved

    mark = out.get(TRIAGE_BUDGET_CUTOFF_KEY)
    assert mark, f"a truncated diagnosis must say so, got {out}"
    assert mark["kind"] == "time_budget" and mark["turns"] == 206
    assert "1200s wall" in mark["detail"]


def test_a_verdict_that_reached_a_CONCLUSION_carries_no_mark():
    """Absence is the signal. Mutation: stamp the key unconditionally (an empty dict) and every
    complete diagnosis reads as truncated — the same illness pointed the other way."""
    from looplab.agents import unified_agent as ua

    def _no_cutoff(self, messages, emit_spec, finalize, fallback, **kw):
        return {"action": "repair", "rationale": "r", "missing_dependency": ""}

    saved = ua.UnifiedAgent._pilot_emit
    ua.UnifiedAgent._pilot_emit = _no_cutoff
    try:
        agent = _stub_agent(ua)
        out = ua.UnifiedAgent.triage_crash(agent, node=None, error="boom", attempt=1)
    finally:
        ua.UnifiedAgent._pilot_emit = saved

    assert TRIAGE_BUDGET_CUTOFF_KEY not in out


def test_the_two_engine_stamped_keys_are_distinct():
    """They mean different things and must not collapse: one says the endpoint was unreachable (and
    halts the RUN), the other says the sweep was cut short (advisory).

    Mutation: reuse `transport_failure` for the cutoff and a truncated diagnosis pauses the run."""
    assert TRIAGE_BUDGET_CUTOFF_KEY != TRIAGE_TRANSPORT_FAILURE_KEY


def test_the_loop_really_reports_a_time_cutoff_to_its_observer():
    """The other end of the wire, driven through the REAL loop: `_note_budget` must actually call the
    observer on the time exit, or the stamp above can never fire in production.

    Mutation: drop the `on_budget` call from `_note_budget` and nothing is ever reported."""
    got = []

    class _Slow:
        def chat(self, messages, tools, tool_choice="auto"):
            import time
            time.sleep(0.05)
            return {"content": "", "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "update_plan", "arguments": '{"plan": [{"step": "s"}]}'}}]}

        def complete_tool(self, messages, json_schema):
            return {"forced": True}

    drive_tool_loop(_Slow(), None, [{"role": "user", "content": "go"}],
                    {"type": "function", "function": {"name": "emit", "description": "",
                                                      "parameters": {"type": "object",
                                                                     "properties": {}}}},
                    self_plan=True, time_budget_s=0.01,
                    finalize=lambda a: a, fallback=lambda m: None,
                    on_budget=lambda payload: got.append(payload))
    assert got and isinstance(got[0], dict), "the loop must report its cutoff to the observer"
    assert got[0].get("kind"), f"and name WHICH bound ended it, got {got[0]}"
