"""One admission fence for every eval-dispatch branch (doc 25 ES-06).

The same eight clauses were spelled at three sites — twice affirmatively and once NEGATED inline,
one of the three using `getattr` defaults the others did not. Three copies of a predicate that
decides whether a node may still be evaluated is a drift surface with no red test: the serial and
parallel branches could disagree about what "still admissible" means while the suite stayed green,
and the failure is a SECOND eval on a node that already terminated — a breach of the
one-terminal-event-per-node invariant, which corrupts the fold rather than raising.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from looplab.core.models import NodeStatus
from looplab.engine.orchestrator import _eval_admission_current, _run_terminal_gate


class _Node:
    def __init__(self, nid=1, attempt=0, status=NodeStatus.pending, tombstoned=False):
        self.id, self.attempt, self.status, self.tombstoned = nid, attempt, status, tombstoned


class _State:
    def __init__(self, **kw):
        self.paused = kw.get("paused", False)
        self.finished = kw.get("finished", False)
        self.stop_requested = kw.get("stop_requested", None)
        self.aborted_nodes = kw.get("aborted_nodes", set())
        self.total_eval_seconds = kw.get("total_eval_seconds", 0.0)


def test_a_current_node_is_admitted():
    assert _eval_admission_current(_State(), _Node(), 0, None) is True


@pytest.mark.parametrize("node,state,gen,max_es,why", [
    (None, _State(), 0, None, "the node was rebuilt while we waited"),
    (_Node(attempt=1), _State(), 0, None, "a different lifecycle owns this reservation now"),
    (_Node(status=NodeStatus.evaluated), _State(), 0, None,
     "already terminal — a second eval breaches one-terminal-event-per-node"),
    (_Node(tombstoned=True), _State(), 0, None, "withdrawn mid-wait"),
    (_Node(), _State(aborted_nodes={1}), 0, None, "operator aborted it mid-wait"),
    (_Node(), _State(paused=True), 0, None, "run paused during the wait"),
    (_Node(), _State(finished=True), 0, None, "run finished during the wait"),
    (_Node(), _State(stop_requested="op"), 0, None, "stop requested during the wait"),
    (_Node(), _State(total_eval_seconds=100.0), 0, 50.0, "eval budget spent during the wait"),
])
def test_every_clause_refuses(node, state, gen, max_es, why):
    assert _eval_admission_current(state, node, gen, max_es) is False, why


def test_the_budget_clause_is_inclusive_and_opt_out():
    assert _eval_admission_current(_State(total_eval_seconds=50.0), _Node(), 0, 50.0) is False
    assert _eval_admission_current(_State(total_eval_seconds=49.9), _Node(), 0, 50.0) is True
    assert _eval_admission_current(_State(total_eval_seconds=1e9), _Node(), 0, None) is True, (
        "max_es=None means no budget bound, not a bound of zero")


def test_the_terminal_gate_tolerates_a_state_missing_the_fields():
    """The `getattr` defaults one of the three copies carried. A folded RunState always has all
    three, so this only keeps a hand-built stub from raising — it cannot change a live decision."""
    assert _run_terminal_gate(object()) is False
    assert _run_terminal_gate(_State(paused=True)) is True


def test_no_dispatch_branch_re_derives_the_fence():
    """The regression that matters is a fourth copy, not a wrong answer here."""
    from looplab.engine import orchestrator

    tree = ast.parse(inspect.getsource(orchestrator))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name in {"_eval_admission_current", "_run_terminal_gate"}:
            continue
        body = ast.unparse(node)
        if "aborted_nodes" in body and "total_eval_seconds" in body and "tombstoned" in body:
            offenders.append(node.name)
    assert not offenders, (
        f"{offenders} re-derive the admission fence inline; call `_eval_admission_current`")
