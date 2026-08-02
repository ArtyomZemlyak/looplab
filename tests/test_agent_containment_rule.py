"""The agents package's containment rule, written down once (doc 25 AG-06).

Every agentic entry point needs the same asymmetry, and it is currently restated at ~15 call sites:
a hard budget stop PROPAGATES and ends the run; anything else DEGRADES to a caller-specific safe
value rather than crashing it.

`tool_loop.resilient` is that rule as code. These tests pin the asymmetry itself, because getting it
backwards is both easy and expensive in opposite directions — swallowing `BudgetExceeded` lets a run
keep billing past the ceiling an operator set to stop it, while propagating a transport blip crashes
a run and loses every node already evaluated.

They deliberately do NOT assert that existing call sites adopt the helper: doc 25's own
recommendation is opportunistic adoption, and the fifteen why-comments at those sites each record
why THAT fallback is safe. A guard demanding uniformity here would trade fifteen small duplications
for fifteen lost explanations.
"""
from __future__ import annotations

import pytest

from looplab.agents.tool_loop import resilient
from looplab.core.llm import BudgetExceeded


def test_the_happy_path_returns_the_attempt_and_never_calls_the_fallback():
    calls = []
    assert resilient(lambda: "real", lambda: calls.append("fallback") or "fb") == "real"
    assert calls == []


def test_a_budget_stop_propagates_and_the_fallback_is_never_reached():
    """The expensive direction. `BudgetExceeded` is not a failure to contain — it is the operator's
    spend ceiling doing its job, and degrading past it keeps billing a run that was told to stop."""
    calls = []

    def spend_ceiling():
        raise BudgetExceeded("llm budget exhausted")

    with pytest.raises(BudgetExceeded):
        resilient(spend_ceiling, lambda: calls.append("fallback"))
    assert calls == [], "a budget stop must not be degraded into a fallback value"


@pytest.mark.parametrize("failure", [
    RuntimeError("transport reset"),
    ValueError("parser gave up"),
    OSError("endpoint unreachable"),
    KeyError("malformed tool payload"),
])
def test_every_other_failure_degrades_instead_of_crashing_the_run(failure):
    """The other direction. A local problem must not lose every node already evaluated."""
    def broken():
        raise failure

    assert resilient(broken, lambda: "safe default") == "safe default"


def test_the_contained_exception_is_offered_to_an_observer():
    seen = []
    assert resilient(lambda: 1 / 0, lambda: "safe", on_error=seen.append) == "safe"
    assert len(seen) == 1 and isinstance(seen[0], ZeroDivisionError)


def test_a_budget_stop_is_not_reported_to_the_observer():
    """`on_error` marks CONTAINED failures. Reporting a propagating budget stop through it would let
    a telemetry consumer count the run's deliberate stop as an agent error."""
    seen = []
    with pytest.raises(BudgetExceeded):
        resilient(lambda: (_ for _ in ()).throw(BudgetExceeded("stop")), lambda: "safe",
                  on_error=seen.append)
    assert seen == []


def test_a_broken_observer_cannot_break_the_agent():
    """Telemetry is not allowed to escalate a contained failure into an uncontained one."""
    def exploding_observer(_exc):
        raise RuntimeError("logging backend down")

    assert resilient(lambda: 1 / 0, lambda: "safe", on_error=exploding_observer) == "safe"


def test_a_failing_fallback_is_not_swallowed():
    """The one thing the helper must NOT contain. If the safe default is itself broken there is no
    safe value to return, and hiding that would hand the caller a silent `None`."""
    def broken_fallback():
        raise RuntimeError("fallback is broken too")

    with pytest.raises(RuntimeError, match="fallback is broken too"):
        resilient(lambda: 1 / 0, broken_fallback)


def test_the_rule_is_documented_where_a_new_caller_will_read_it():
    """AG-06's point is that the rule lives in ~15 copies and a new caller must re-derive it. A
    helper with no statement of the asymmetry would just be a sixteenth copy."""
    doc = resilient.__doc__ or ""
    assert "BudgetExceeded" in doc
    assert "propagat" in doc.lower() and "degrade" in doc.lower()


# --- the pilot emit helper (doc 25 AG-04) ------------------------------------------------------

def _pilot(bind_calls, *, tools=True):
    """A UnifiedAgent stripped to what `_pilot_emit` reads."""
    from looplab.agents.unified_agent import UnifiedAgent

    class _Tools:
        def bind_state(self, state, parent=None):
            bind_calls.append((state, parent))

    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent._pilot_client = object()
    agent._pilot_tools = _Tools() if tools else None
    agent._agent_max_turns = 3
    agent._agent_time_budget_s = 1.0
    agent._loop_opts = {}
    return agent


def test_the_pilot_emit_binds_state_only_when_its_caller_says_so(monkeypatch):
    """`choose_action` binds unconditionally; `triage_crash` binds only when handed a run state.
    Inferring the flag from `state is not None` would silently change which tools are reachable, so
    it stays explicit and this pins both directions."""
    import looplab.agents.agent as agent_module

    monkeypatch.setattr(agent_module, "drive_tool_loop", lambda *a, **k: {"ok": True})

    bound = []
    assert _pilot(bound)._pilot_emit([], {}, None, lambda _m: None,
                                     state="S", bind_state=True) == {"ok": True}
    assert bound == [("S", None)]

    bound = []
    _pilot(bound)._pilot_emit([], {}, None, lambda _m: None, state=None, bind_state=False)
    assert bound == [], "triage without a run state must not bind tools to it"

    # The case that distinguishes the FLAG from an inference: the pilot binds even when the state
    # it was handed is None. Collapsing `bind_state` into `state is not None` passes both cases
    # above and silently changes which tools the pilot can reach.
    bound = []
    _pilot(bound)._pilot_emit([], {}, None, lambda _m: None, state=None, bind_state=True)
    assert bound == [(None, None)], (
        "an explicit bind must happen regardless of the state's value — that is why it is a flag")


def test_the_pilot_emit_propagates_a_budget_stop_and_degrades_everything_else(monkeypatch):
    """The containment rule reaching the site it was extracted for."""
    import looplab.agents.agent as agent_module

    def budget(*_a, **_k):
        raise BudgetExceeded("ceiling reached")

    monkeypatch.setattr(agent_module, "drive_tool_loop", budget)
    with pytest.raises(BudgetExceeded):
        _pilot([])._pilot_emit([], {}, None, lambda _m: "fallback", state=None)

    def transport(*_a, **_k):
        raise RuntimeError("endpoint 503 after retries")

    monkeypatch.setattr(agent_module, "drive_tool_loop", transport)
    assert _pilot([])._pilot_emit([], {}, None, lambda _m: "fallback",
                                  state=None) == "fallback"


def test_the_seam_is_resolved_at_call_time(monkeypatch):
    """The six-line comment this helper inherited exists because a module-level import early-binds
    the function object, so a monkeypatch on the documented seam never reaches the call — and an
    offline test then silently drives the REAL loop against the real client."""
    import looplab.agents.agent as agent_module

    monkeypatch.setattr(agent_module, "drive_tool_loop", lambda *a, **k: "patched")
    assert _pilot([])._pilot_emit([], {}, None, lambda _m: "fb", state=None) == "patched"


def test_triage_without_a_run_state_reaches_the_helper_with_binding_off(monkeypatch):
    """Driving the real entry point, because the wiring is what the structural count cannot see: a
    caller that simply stops passing `bind_state` still shows up as one `_pilot_emit(` call."""
    from looplab.agents.unified_agent import UnifiedAgent

    seen = {}

    def spy(_self, messages, emit_spec, finalize, fallback, *, state=None, bind_state=True):
        seen.update(state=state, bind_state=bind_state)
        return {"action": "repair", "rationale": ""}

    monkeypatch.setattr(UnifiedAgent, "_pilot_emit", spy)
    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent._pilot_client = object()
    agent._pilot_tools = None
    agent.prompts = {}
    node = type("N", (), {"id": 1, "code": ""})()
    agent.triage_crash(node, "boom", 1)
    assert seen == {"state": None, "bind_state": False}, (
        "triage with no run state must not ask the helper to bind tools to it")


def test_both_public_entry_points_go_through_the_helper():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "looplab" / "agents"
              / "unified_agent.py").read_text(encoding="utf-8-sig")
    ast.parse(source)
    assert source.count("self._pilot_emit(") == 2, "choose_action and triage_crash, no more no less"
    assert "except BudgetExceeded:" not in source, (
        "the containment idiom is inlined again; `_pilot_emit` routes through tool_loop.resilient")
