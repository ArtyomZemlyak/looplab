"""`RunState.halted`: ONE spelling of "the run has stopped accepting new work" (review 2026-09-22,
ENG1-11).

`paused or finished or stop_requested` was written out at thirteen sites — admission and reservation
fences, the confirm phase's lifecycle checks, the Card session's terminal intent, the containment
pause, the CLI's resume handoff — beside `eval_dispatch.py::_run_terminal_gate`, which asks the same
question of a possibly hand-built state. Thirteen copies of a gate are thirteen chances for one of
them to drift (a `stop_requested` read by truthiness at one site and by `is not None` at another is a
run that stops in one place and keeps building in the next). The copies now read the property; this
file pins its truth table — byte-identical to the spelling it replaced, including the empty-reason
case — its agreement with the stub-tolerant gate, and that no module spells the trio out again.

The name is `halted` and not `stopping` on purpose: `engine/speculation.py::CardSessionGates.stopping`
is read in the same module and is WIDER (this, or an exhausted eval budget, or a pending outer
rebuild), and `tests/test_card_speculation_engine.py` counts its readers by that attribute name.
"""
from __future__ import annotations

import ast
import itertools

import pytest

from looplab.core.models import RunState
from looplab.engine.eval_dispatch import _run_terminal_gate
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests._source_scan import PKG, iter_trees

_TRIO = {"paused", "finished", "stop_requested"}


@pytest.mark.parametrize("paused,finished,stop_requested", list(itertools.product(
    (False, True), (False, True), (None, "", "operator abort"))))
def test_the_property_is_the_disjunction_it_replaced(paused, finished, stop_requested):
    state = RunState(paused=paused, finished=finished, stop_requested=stop_requested)
    replaced = bool(paused or finished or stop_requested)     # the spelling at every old site
    assert state.halted is replaced
    assert _run_terminal_gate(state) is replaced, (
        "the stub-tolerant dispatch gate disagrees with the property on a real state")


def test_an_empty_stop_reason_does_not_halt_the_run():
    """TRUTHINESS, as every replaced site read it: the reason string is what the control carried,
    and an empty one never stopped anything. `is not None` here would be a behaviour change."""
    assert RunState(stop_requested="").halted is False
    assert RunState(stop_requested="x").halted is True


def test_it_reads_the_folded_run(tmp_path):
    """Driven through the real fold: a pause halts the run, its resume reopens it, an abort halts
    it again."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    assert fold(store.read_all()).halted is False
    store.append("pause", {"reason": "operator"})
    assert fold(store.read_all()).halted is True
    store.append("resume", {})
    assert fold(store.read_all()).halted is False
    store.append("run_abort", {"reason": "operator"})
    assert fold(store.read_all()).halted is True


def test_it_is_not_part_of_any_dump():
    """A plain property: no snapshot, `/state` payload or SSE frame changes shape."""
    dumped = RunState(paused=True).model_dump(mode="json")
    assert "halted" not in dumped


def _spells_the_trio(node: ast.BoolOp) -> bool:
    """`x.paused or x.finished or x.stop_requested` (or its negated `and not` form) on ONE object."""
    negated = isinstance(node.op, ast.And)
    seen: dict[str, set] = {}
    for value in node.values:
        if negated:
            if not (isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not)):
                continue
            value = value.operand
        if isinstance(value, ast.Attribute) and value.attr in _TRIO:
            seen.setdefault(ast.unparse(value.value), set()).add(value.attr)
        elif (isinstance(value, ast.Call) and getattr(value.func, "id", "") == "getattr"
              and len(value.args) >= 2 and isinstance(value.args[1], ast.Constant)
              and value.args[1].value in _TRIO):
            seen.setdefault(ast.unparse(value.args[0]), set()).add(value.args[1].value)
    return any(attrs == _TRIO for attrs in seen.values())


def test_no_module_spells_the_disjunction_out_again():
    """By AST (a comment cannot satisfy it): the property's own body and the stub-tolerant gate are
    the only two spellings in the package."""
    allowed = {("core/models.py", "RunState.halted"),
               ("engine/eval_dispatch.py", "_run_terminal_gate")}
    found = set()
    for path, tree in iter_trees(PKG):
        rel = path.relative_to(PKG).as_posix()
        parents = {child: parent for parent in ast.walk(tree)
                   for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BoolOp) and _spells_the_trio(node)):
                continue
            owner, cursor = [], parents.get(node)
            while cursor is not None:
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    owner.append(cursor.name)
                cursor = parents.get(cursor)
            found.add((rel, ".".join(reversed(owner))))
    assert found == allowed, (
        f"`paused or finished or stop_requested` is spelled out again; read `RunState.halted`: "
        f"{sorted(found - allowed)}")
