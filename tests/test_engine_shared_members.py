"""A home for the Engine members that belong to every cluster (doc 25 ES-14).

The seventeen-file split separates the big concerns well. What it lacked was a home for a helper
several clusters call, so those defaulted back into `orchestrator.py` — the module the split exists
to shrink — or landed in whichever mixin needed them first. `_agent_may`, the agent-governance gate
consulted at the strategist / boss / researcher seams, sat in `EvalDispatchMixin` while its own
sibling `effective_researcher_eval_timeout` lived in a separate 33-line module; `_op_span` stayed in
the god-module under a comment explaining that it is shared. A comment saying "this belongs to no
cluster" is the symptom, not the resolution.

These tests pin the home AND the bar for entering it — a shared module that accretes single-caller
helpers is just a second god-module.
"""
from __future__ import annotations

import ast
import contextlib
import inspect

import pytest

from _source_scan import PKG
from looplab.engine import eval_dispatch, orchestrator, shared
from looplab.engine.cadence import cadence_due
from looplab.engine.orchestrator import Engine
from looplab.engine.shared import SharedEngineMixin, effective_researcher_eval_timeout

SHARED_MEMBERS = ("_agent_may", "_op_span", "_cadence_due")


@pytest.mark.parametrize("name", SHARED_MEMBERS)
def test_each_cross_cluster_member_lives_in_the_shared_home(name):
    assert name in vars(SharedEngineMixin), f"{name} is not owned by SharedEngineMixin"
    assert getattr(Engine, name) is not None, "and it must still resolve on the Engine"


@pytest.mark.parametrize("name", SHARED_MEMBERS)
def test_no_other_engine_module_re_declares_it(name):
    """The point of the home is that there is only one. A second definition in a concern mixin would
    win or lose by MRO position, which is not a thing a reader can see at the call site."""
    owners = []
    for path in sorted((PKG / "engine").glob("*.py")):
        if path.name == "shared.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                owners.append(path.name)
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in node.targets):
                owners.append(path.name)
    assert owners == [], f"{name} is also defined in {owners}"


def test_the_governance_gate_sits_with_its_own_sibling():
    """They are one rule seen twice: `effective_researcher_eval_timeout` calls `_agent_may` to decide
    whether a researcher timeout override is even an action. Splitting them across two modules is how
    a reader ends up believing there are two governance systems."""
    assert "_agent_may" in inspect.getsource(effective_researcher_eval_timeout)
    assert effective_researcher_eval_timeout.__module__ == SharedEngineMixin.__module__


def test_the_bar_for_adding_to_the_shared_home_is_written_down():
    doc = shared.__doc__ or ""
    assert "more than one cluster" in doc
    assert "second god-module" in doc, "the failure mode this module can become must be named"


def test_the_shared_mixin_is_last_in_the_mro_so_a_cluster_can_still_specialize():
    bases = Engine.__bases__
    assert bases[-1] is SharedEngineMixin
    assert Engine.__mro__[-2] is SharedEngineMixin, "only `object` may follow it"


# ------------------------------------------------------------------ the members still work

def test_the_governance_gate_still_locks_an_unlisted_setting():
    engine = Engine.__new__(Engine)
    engine._agent_control = {"eval_parallel": ("strategist",)}
    assert engine._agent_may("strategist", "eval_parallel") is True
    assert engine._agent_may("boss", "eval_parallel") is False
    assert engine._agent_may("strategist", "never_registered") is False, (
        "a setting absent from the map is LOCKED for everyone")


def test_the_op_span_is_a_null_context_without_a_tracer():
    """Tests build Engine via `__new__` and skip `__init__`, so the op must still run untraced rather
    than raising on a missing tracer."""
    engine = Engine.__new__(Engine)
    span = engine._op_span("probe")
    assert isinstance(span, contextlib.nullcontext)
    with span:
        pass


def test_the_op_span_opens_a_NEW_trace_when_one_is_wired():
    """The whole reason it exists: the event appended inside is stamped with THIS op's trace_id, so
    the UI can scope it to the operation rather than the whole iteration."""
    seen = {}

    class _Tracer:
        def span(self, name, **kwargs):
            seen.update(name=name, kwargs=kwargs)
            return contextlib.nullcontext()

    engine = Engine.__new__(Engine)
    engine.tracer = _Tracer()
    with engine._op_span("hypothesis_merge", n=3):
        pass
    assert seen["name"] == "hypothesis_merge"
    assert seen["kwargs"]["new_trace"] is True and seen["kwargs"]["n"] == 3


def test_the_cadence_gate_is_the_shared_rule_not_a_copy():
    assert Engine._cadence_due is cadence_due
    # since-last, not `n % every == 0`: a run that skips a node must not skip the whole cadence.
    assert Engine._cadence_due(10, 5, 5) is True
    assert Engine._cadence_due(9, 5, 5) is False
    assert Engine._cadence_due(10, 5, 0) is False, "a non-positive cadence is off, not always-due"


def test_eval_dispatch_reaches_the_gate_through_the_engine():
    """It still CALLS `_agent_may` — the move was about ownership, not about removing the consult."""
    source = inspect.getsource(eval_dispatch)
    assert "effective_researcher_eval_timeout" in source
    assert "from looplab.engine.shared import" in source


def test_the_orchestrator_no_longer_carries_them():
    source = inspect.getsource(orchestrator)
    assert "def _op_span" not in source
    assert "_cadence_due = staticmethod" not in source
    assert "SharedEngineMixin" in source, "but it must still compose the mixin"
