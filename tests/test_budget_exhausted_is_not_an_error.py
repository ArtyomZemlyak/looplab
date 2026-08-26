"""Reaching the operator's spend ceiling is the designed end of a budgeted run, not a crash.

Measured on the 2026-08-24 campaign: ALL ELEVEN `run_finished` rows under `runs-B` carry
`reason: "error"`, and every single one is the spend ceiling -- zero genuine failures. A reader
keying on the class cannot tell a healthy budgeted finish from a crash, and the campaign driver had
to learn the difference from an exit code instead.

The naive repair -- "just use a distinct reason" -- would have broken the finalization protocol,
and that is the second thing these tests pin. `reason == "error"` never meant "crashed": it means
"this terminal event was written by the guarded-abort path rather than by the engine's clean
finish", and six protocol sites key on it. Introducing a new reason without teaching them would
have made a guarded abort look like a clean engine finish.
"""
from __future__ import annotations

import pytest

from looplab.cli.run_cmds import _budget_leaf
from looplab.core.llm import BudgetExceeded
from looplab.events.finalize_scope import GUARDED_ABORT_REASONS, is_guarded_abort


def test_the_ceiling_is_still_the_guarded_abort_class():
    """The protocol half. If this goes red, a budgeted finish is being treated as a CLEAN engine
    finish and finalization will acknowledge a terminal intent that is not its own."""
    assert is_guarded_abort("error")
    assert is_guarded_abort("budget_exhausted")
    assert "budget_exhausted" in GUARDED_ABORT_REASONS
    for clean in ("complete", "converged", "max_nodes", None, ""):
        assert not is_guarded_abort(clean), clean


def test_every_protocol_site_uses_the_predicate_not_a_literal():
    """A seventh site added later with a bare `== "error"` would silently exclude the ceiling."""
    from pathlib import Path

    for rel in ("looplab/engine/finalize.py", "looplab/events/finalize_scope.py"):
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        for banned in ('reason") or "").lower() == "error"', 'reason") or "").lower() != "error"'):
            assert banned not in src, f"{rel} still keys on the literal: {banned}"


def test_a_bare_budget_exception_is_recognised():
    assert _budget_leaf(BudgetExceeded("spend ceiling reached: $1.0007")) is not None
    assert _budget_leaf(RuntimeError("something else")) is None
    assert _budget_leaf(None) is None


def test_a_ceiling_hit_inside_a_task_group_is_still_recognised():
    """The case the old code could not see at all.

    `_run_engine_guarded`'s own docstring records it: anything raised inside the eval task group
    escapes as the GROUP's "unhandled errors in a TaskGroup (1 sub-exception)" and the leaf message
    never reaches the event. So a ceiling hit on the concurrent path was recorded with neither its
    class nor its sentence.
    """
    leaf = BudgetExceeded("spend ceiling reached: $1.0107")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [leaf])
    assert _budget_leaf(group) is leaf

    nested = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [leaf])])
    assert _budget_leaf(nested) is leaf


def test_it_is_found_through_a_cause_chain_but_cannot_hang_on_a_cycle():
    leaf = BudgetExceeded("spend ceiling reached: $1.00")
    wrapper = RuntimeError("wrapped")
    wrapper.__cause__ = leaf
    assert _budget_leaf(wrapper) is leaf

    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert _budget_leaf(a) is None          # bounded depth, not a hang


def test_an_ordinary_crash_keeps_saying_error():
    """The falsifier for a change that relabels every failure as a budget stop."""
    assert _budget_leaf(ValueError("solver blew up")) is None
    grp = BaseExceptionGroup("boom", [ValueError("x"), KeyError("y")])
    assert _budget_leaf(grp) is None
