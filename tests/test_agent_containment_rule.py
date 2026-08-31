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

    def spy(_self, messages, emit_spec, finalize, fallback, *, state=None, bind_state=True,
            transport_fallback=None, extra_tools=None, extra_turns=0, wall_when_unbounded=0.0,
            on_budget=None):
        # `on_budget=` arrived with the triage budget-cutoff stamp (`45f87d34`) and this double was
        # not re-pointed with it, so the real call raised TypeError here. A spy whose signature the
        # production call site cannot satisfy tests nothing; it is listed rather than swallowed with
        # `**_` so the next added argument is a decision instead of a silent hole.
        seen.update(state=state, bind_state=bind_state, extra_tools=extra_tools,
                    extra_turns=extra_turns, wall=wall_when_unbounded)
        # The two degradations must arrive as two DIFFERENT callables: the loop's no-emit fallback
        # says `unreadable` (the endpoint answered), the transport one says `unanswerable` and
        # carries the marker. One callable for both is how a prose-answering live endpoint paused a
        # whole run — see `test_repair_stop_decision.py::test_a_live_endpoint_that_never_emits_...`.
        seen.update(no_emit=fallback(messages), transport=transport_fallback(messages))
        return {"action": "repair", "rationale": ""}

    monkeypatch.setattr(UnifiedAgent, "_pilot_emit", spy)
    agent = UnifiedAgent.__new__(UnifiedAgent)
    agent._pilot_client = object()
    agent._pilot_tools = None
    agent.prompts = {}
    # Set explicitly rather than reached through a `getattr(..., 0.0)` at the call site: this object
    # skips `__init__` on purpose, and a defensive default there would let a genuinely unwired
    # `triage_time_budget_s` pass as "no wall configured" instead of failing loudly.
    agent._triage_time_budget_s = 0.0
    node = type("N", (), {"id": 1, "code": ""})()
    agent.triage_crash(node, "boom", 1)
    assert (seen["state"], seen["bind_state"]) == (None, False), (
        "triage with no run state must not ask the helper to bind tools to it")
    # ...and a caller that was given no log tools asks for no per-call provider, so the loop is handed
    # `self._pilot_tools` itself and `self._loop_opts` unchanged — the historical request byte for
    # byte (`engine/train_monitor.py::repair_log_tools`, `Settings.repair_log_tools`).
    assert seen["extra_tools"] is None
    # ...and the triage wall is FORWARDED, not defaulted inside the helper: an agent configured with
    # no wall must reach it with 0, so `Settings.triage_time_budget_s = 0` really means unlimited.
    assert seen["wall"] == 0.0
    from looplab.engine.triage import (TRIAGE_TRANSPORT_FAILURE_KEY, UNANSWERABLE_TRIAGE_ACTION,
                                       UNREADABLE_TRIAGE_ACTION, is_transport_failure_verdict)
    assert seen["no_emit"]["action"] == UNREADABLE_TRIAGE_ACTION
    assert TRIAGE_TRANSPORT_FAILURE_KEY not in seen["no_emit"]
    assert not is_transport_failure_verdict(seen["no_emit"])
    assert seen["transport"]["action"] == UNANSWERABLE_TRIAGE_ACTION
    assert is_transport_failure_verdict(seen["transport"])


def test_every_public_entry_point_goes_through_the_helper():
    """THREE since F8 added `repair_critic` (2026-08-13), and the count is the point rather than the
    number: `_pilot_emit` owns the containment boundary — the budget-vs-transport split, the
    bind_state decision, the call-time seam import — so a new emit path that hand-rolls its own
    `try`/`except` around `drive_tool_loop` is how a transport failure starts being read as a
    verdict again. Raise this only alongside a new pilot method that routes through the helper."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "looplab" / "agents"
              / "unified_agent.py").read_text(encoding="utf-8-sig")
    ast.parse(source)
    assert source.count("self._pilot_emit(") == 3, (
        "choose_action, triage_crash and repair_critic, no more no less")
    assert "except BudgetExceeded:" not in source, (
        "the containment idiom is inlined again; `_pilot_emit` routes through tool_loop.resilient")
