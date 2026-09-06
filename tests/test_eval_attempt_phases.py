"""The EvalAttempt split (doc 52 row 21): a driver, nine phases, one record, a closed signal vocabulary.

`_evaluate` was a 2,016-line method carrying fifty locals across the regions its own comments named.
It is now a DRIVER over `EvalAttempt` and the `_eval_*` phases, cut with every append, `_write_lock`
block, fold and branch in place (each moved region was proven AST-equivalent to the stated rewrite at
the cut, and four deterministic offline scenarios — a smoke run, crash -> repair -> success, crash ->
repair -> crash -> floor stop, and no-repair — produced byte-identical normalized event logs before
and after). What this file guards is the SHAPE that split created, in the same three ways
`tests/test_orchestrator_internals.py` guards the run loop's:

  * a phase reports the loop control it cannot execute as a `PHASE_*` signal, and that vocabulary is
    CLOSED — resolved from real `ast.Return` constants, because a misspelt signal does not fail: it
    reads as "next phase" and turns a stop into another attempt;
  * the driver dispatches on signal IDENTITY and runs the phases in the one order the old method did;
  * the record declares every attribute the phases touch, and refuses one they do not (`slots`).

The last test DRIVES a phase on its own against a real engine — the thing the split bought.
"""
from __future__ import annotations

import ast
import dataclasses

import anyio
import pytest

from looplab.engine import evaluate as ev
from looplab.engine.evaluate import (PHASE_NEXT, PHASE_RETRY, PHASE_RETURN, PHASE_SETTLED, PHASE_SIGNALS,
                                     EvalAttempt, EvaluateMixin)
from tests._source_scan import EVAL_PHASES, called_names, function_tree
from tests.factories import make_engine

_SIGNAL_NAMES = {"PHASE_NEXT", "PHASE_RETRY", "PHASE_SETTLED", "PHASE_RETURN"}
_SYNC_PHASES = {"_eval_prepare_workdir", "_eval_seed_ledgers", "_eval_write_terminal"}


def _own_returns(fn) -> list[ast.Return]:
    """Every `return` that belongs to *fn* itself — a nested def's returns are its own."""
    tree = function_tree(fn).body[0]
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                out.append(child)
            walk(child)
    walk(tree)
    return out


def test_the_signal_vocabulary_is_closed_and_every_phase_speaks_only_it():
    assert PHASE_SIGNALS == {PHASE_NEXT, PHASE_RETRY, PHASE_SETTLED, PHASE_RETURN}
    assert len(PHASE_SIGNALS) == 4, "four distinct words, or two signals collapse into one branch"
    for name in EVAL_PHASES:
        fn = getattr(EvaluateMixin, name)
        returns = _own_returns(fn)
        if name in _SYNC_PHASES:
            assert all(r.value is None for r in returns), f"{name} reports no signal and must return nothing"
            continue
        assert returns, f"{name} must report a signal"
        for r in returns:
            assert isinstance(r.value, ast.Name) and r.value.id in _SIGNAL_NAMES, (
                f"{name} returns {ast.unparse(r)!r}: a phase may return ONLY a registered signal name — "
                "a bare `return` reads as None, which is not a signal, and the driver would fall through")
            assert getattr(ev, r.value.id) in PHASE_SIGNALS
        # the phase's last statement is an unconditional signal, so falling off the end is impossible
        last = function_tree(fn).body[0].body[-1]
        assert isinstance(last, ast.Return) and isinstance(last.value, ast.Name), name
    # …and the driver's terminal transitions are the ones the old loop had: a `break` became SETTLED,
    # a `continue` RETRY, a bare `return` RETURN — no phase may `break`/`continue` a loop it is not in.
    for name in EVAL_PHASES:
        for node in ast.walk(function_tree(getattr(EvaluateMixin, name))):
            assert not isinstance(node, (ast.Break, ast.Continue)) or _inside_loop(node, name), name


def _inside_loop(node, name) -> bool:
    """A `break`/`continue` inside a phase must belong to a loop INSIDE that phase."""
    tree = function_tree(getattr(EvaluateMixin, name))
    for loop in ast.walk(tree):
        if isinstance(loop, (ast.For, ast.AsyncFor, ast.While)) and any(n is node for n in ast.walk(loop)):
            return True
    return False


def test_the_driver_runs_the_phases_in_the_one_order_and_dispatches_on_identity():
    phases = [c for c in called_names(EvaluateMixin._evaluate) if c.startswith("self._eval_")]
    assert phases == [
        "self._eval_admit", "self._eval_prepare_workdir", "self._eval_seed_ledgers",
        "self._eval_run_attempt", "self._eval_settle_outcome", "self._eval_salvage",
        "self._eval_decide_repair", "self._eval_apply_repair", "self._eval_write_terminal"], phases
    assert tuple(p.removeprefix("self.") for p in phases) == EVAL_PHASES, "the registry lists the driver's order"
    tree = function_tree(EvaluateMixin._evaluate)
    compared = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            names = [c.id for c in node.comparators if isinstance(c, ast.Name)]
            if any(n in _SIGNAL_NAMES for n in names):
                assert all(isinstance(op, ast.Is) for op in node.ops), ast.unparse(node)
                compared.update(names)
    assert compared == {"PHASE_RETURN", "PHASE_SETTLED", "PHASE_RETRY"}, (
        "the driver decides on RETURN/SETTLED/RETRY; NEXT is the fall-through and is never compared")
    # the containment handler names the lifecycle through the record, never through a re-fold
    contain = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_contain_eval_crash"]
    assert [[ast.unparse(a) for a in c.args] for c in contain] == [["node_id", "a.generation", "exc"]]
    # the record is built BEFORE the try, so the handler can always read it
    body = tree.body[0].body
    first_stmt = next(s for s in body if not isinstance(s, ast.Expr))
    assert isinstance(first_stmt, ast.Assign) and ast.unparse(first_stmt.targets[0]) == "a", ast.unparse(first_stmt)


def test_the_record_declares_every_attribute_the_phases_touch_and_refuses_the_rest():
    fields = {f.name for f in dataclasses.fields(EvalAttempt)}
    methods = {"mark_superseded_workdir", "stamp_workdir", "workdir_matches"}
    touched = set()
    for name in EVAL_PHASES + ("_eval_record_superseded",):
        for node in ast.walk(function_tree(getattr(EvaluateMixin, name))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "a":
                touched.add(node.attr)
    assert touched - methods <= fields, f"phases touch undeclared record attributes: {touched - methods - fields}"
    assert fields <= touched, f"declared but never touched by any phase: {fields - touched}"
    assert methods <= touched
    a = EvalAttempt(node_id=7)
    assert a.generation == -1, "unset until ADMIT binds the lifecycle — what the containment handler reads"
    with pytest.raises(AttributeError):
        a.no_such_attribute = 1          # slots: a typo raises instead of minting silently


class _Span:
    def set(self, *_a, **_k):
        pass

    def set_many(self, **_k):
        pass


def test_admit_refuses_a_closed_run_without_writing_or_binding_a_lifecycle(tmp_path):
    """DRIVEN: one phase, on its own, against a real engine — the thing the split bought.

    A finished run's node is not `pending`, so ADMIT must answer `PHASE_RETURN` without appending a
    row and without binding `a.generation` (the containment handler would otherwise close a
    lifecycle that was never opened)."""
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1)
    anyio.run(eng.run)
    before = len(eng.store.read_all())
    a = EvalAttempt(node_id=0)
    a.sp = _Span()
    assert anyio.run(eng._eval_admit, a) is PHASE_RETURN
    assert len(eng.store.read_all()) == before and a.generation == -1 and a.state is not None
