"""One live concurrency-width settling rule for all four control paths (doc 25 ES-09 + EC-11).

Two loops in `_apply_control_overrides` (operator `budget_extend` controls) and two in
`_apply_strategy` (Strategist decisions) had written out the same validator, differing only in the
upper bound and the target attribute. Each of the four rules inside it fails in a DIFFERENT direction
when a copy drifts, and none of them fails loudly — a rejected width simply leaves the running
envelope alone, which looks exactly like a control that was never sent.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.engine.widths import EVAL_WIDTH_MAX, LLM_WIDTH_MAX, settle_width


# ------------------------------------------------------------------ the four rules

@pytest.mark.parametrize("raw,expected", [
    (1, 1), (4, 4), (1024, 1024), (4.0, 4), ("8", 8),
    (0, 1),                      # a LIVE zero is serial, not AUTO
    (-1, None), (1025, None),    # out of range: refuse, do not clamp
    (True, None), (False, None), (2.5, None), (float("inf"), None), (float("nan"), None),
    (None, None), ("x", None), ([], None), ({}, None),
])
def test_the_settling_rule_answers_each_value_once(raw, expected):
    assert settle_width(raw, EVAL_WIDTH_MAX) == expected


def test_a_bool_is_not_a_width():
    """`True` is an `int` subclass, so a JSON `true` from a UI/API type slip would set the width to
    ONE. The run then looks configured and simply serializes, with nothing in the log to say why."""
    assert settle_width(True, EVAL_WIDTH_MAX) is None
    assert settle_width(False, EVAL_WIDTH_MAX) is None


def test_a_non_integral_float_is_refused_rather_than_truncated():
    """Truncating 2.5 to 2 is a guess about what the caller meant. Refusing keeps the previous
    width — the value the operator last actually chose."""
    assert settle_width(2.5, EVAL_WIDTH_MAX) is None
    assert settle_width(2.0, EVAL_WIDTH_MAX) == 2


def test_the_bound_is_a_REFUSAL_not_a_clamp():
    """A 100_000 clamped to the ceiling would look accepted and quietly reshape the run. Skipping
    leaves the current width, and the next control can correct it."""
    assert settle_width(100_000, EVAL_WIDTH_MAX) is None
    assert settle_width(65, LLM_WIDTH_MAX) is None
    assert settle_width(64, LLM_WIDTH_MAX) == 64


def test_a_live_zero_settles_to_serial_and_never_to_AUTO():
    """AUTO belongs to launch-time `Settings`, where it can read the hardware and the settled eval
    width. A live 0 arriving mid-run has no such context; treating it as AUTO would re-derive a width
    the operator never asked for."""
    assert settle_width(0, EVAL_WIDTH_MAX) == 1
    assert settle_width(0, LLM_WIDTH_MAX) == 1


def test_the_two_axes_keep_their_own_ceilings():
    """They bound different things: evals are bounded by the box, provider calls by the shared LLM
    broker. Collapsing them to one number would let a Strategist open 1024 concurrent provider
    calls."""
    assert (EVAL_WIDTH_MAX, LLM_WIDTH_MAX) == (1024, 64)
    assert settle_width(200, EVAL_WIDTH_MAX) == 200
    assert settle_width(200, LLM_WIDTH_MAX) is None


# ------------------------------------------------------------------ all four call sites use it

@pytest.mark.parametrize("module,holder,function", [
    ("looplab.engine.orchestrator", "Engine", "_apply_control_overrides"),
    ("looplab.engine.strategy", "StrategyCadenceMixin", "_apply_strategy"),
])
def test_no_control_path_re_derives_the_rule(module, holder, function):
    import importlib

    source = inspect.getsource(getattr(getattr(importlib.import_module(module), holder), function))
    assert source.count("settle_width(") == 2, f"{function} does not settle BOTH width axes"
    # Scoped to the two WIDTH loops. `_apply_strategy` also validates the per-lane allocation map,
    # and that rule is deliberately different — strict `int` only, and all-or-nothing across the map
    # (one bad lane rejects the whole allocation) — so folding it in here would loosen it.
    for axis in ('("max_parallel", "eval_parallel")', '("parallel_build", "llm_parallel")'):
        body = source[source.index(axis):source.index(axis) + 400]
        assert "settle_width(" in body, f"{function}'s {axis} loop bypasses the shared rule"
        assert "is_integer()" not in body, f"{function} re-derives the non-integral-float rule"
        assert "<= 1024" not in body and "<= 64" not in body, f"{function} re-derives a bound"


def test_the_per_lane_allocation_keeps_its_own_STRICTER_rule():
    """Not an oversight. A lane map is an allocation, so one bad lane rejects the WHOLE map rather
    than silently allocating the rest; and it accepts strict `int` only, where `settle_width` also
    takes an integral float or a numeric string. Folding it into the shared rule would loosen a
    validator whose job is to be all-or-nothing."""
    from looplab.engine import strategy

    source = inspect.getsource(strategy.StrategyCadenceMixin._apply_strategy)
    lanes = source[source.index("raw_lanes ="):]
    assert "lane_values_valid = False" in lanes and "break" in lanes
    assert "not isinstance(value, int)" in lanes
    assert "settle_width(" not in lanes


def test_the_broker_is_reconfigured_with_the_SETTLED_width():
    """`_reconfigure_llm_broker` applies the identical rule internally, so passing the settled value
    is equivalent to passing the raw one — but only the settled value is guaranteed to match what
    `_llm_parallel` now holds. Passing the raw value left the two spellings free to diverge if either
    rule were ever edited alone."""
    from looplab.engine import strategy

    source = inspect.getsource(strategy.StrategyCadenceMixin._apply_strategy)
    assert "self._reconfigure_llm_broker(_settled)" in source


# ------------------------------------------------------------------ end to end through the engine

def test_an_operator_control_settles_through_the_shared_rule(tmp_path):
    from looplab.core.models import RunState
    from looplab.engine.orchestrator import Engine

    engine = Engine.__new__(Engine)
    engine._speculation_gate_calibration = False
    engine.max_seconds = None
    engine.max_eval_seconds = None
    engine.timeout = 60.0
    engine._eval_parallel = 3
    engine._llm_parallel = 2
    engine._llm_broker = None
    state = RunState()

    state.budget_overrides = {"eval_parallel": 0, "llm_parallel": True}
    engine._apply_control_overrides(state)
    assert engine._eval_parallel == 1, "a live zero must settle to serial width"
    assert engine._llm_parallel == 2, "a bool must leave the running width alone"

    state.budget_overrides = {"eval_parallel": 9999}
    engine._apply_control_overrides(state)
    assert engine._eval_parallel == 1, "an out-of-range width must be refused, not clamped"
