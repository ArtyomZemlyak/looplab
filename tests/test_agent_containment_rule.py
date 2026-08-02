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
