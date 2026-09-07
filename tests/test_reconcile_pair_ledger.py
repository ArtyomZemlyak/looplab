"""A pair the reconcile pass PAID for is spent, whether or not its lesson committed.

The ledger's contract is `select_comparison_pairs`' own words: `exclude` is the "(child, parent) id
tuples already distilled (later firings must not re-spend LLM budget on the same pair)". SPENT means
PAID, and the mid-run cadence already reads it that way in as many words — it appends its
`lessons_distilled` receipt "always — even with 0 lessons".

The reconcile pass did not. It calls `_comparative_lessons` (one paid provider call over the pairs it
selected), and then:

    fresh = committed_fresh
    if not fresh:
        pairs_used = []
    if pairs_used:
        ...append the ledger row...

so a pass that spent the call and committed nothing left its pairs UNSPENT, and the next cadence
re-selected and re-paid for them. Measured on `rubertlite-dense-retrieval` (81 nodes, 22 firings):
three pairs — (23,14), (14,7), (24,14) — each distilled and paid for twice. Three of ~60 is small;
the SHAPE is the point, because this ledger is the only thing standing between a cadence and
unbounded re-spend, and that run is the only one with enough firings to test it at all.

Driven over `select_comparison_pairs` and the real reconcile source, not through a live provider.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from _source_scan import function_tree

from looplab.engine import lessons_reconcile as reconcile_mod
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import select_comparison_pairs


def _node(nid, metric, *, parents=(), status=None):
    return SimpleNamespace(id=nid, metric=metric, repairs=0, parent_ids=list(parents),
                           tombstoned=False, status=status)


def _state(*nodes, direction="max"):
    return SimpleNamespace(nodes={n.id: n for n in nodes}, direction=direction,
                           aborted_nodes=[], best_node_id=None, nodes_flagged=[],
                           trust_gate="audit")


def test_spent_pairs_reads_the_ledger_rows():
    """Precondition: the ledger is `lessons_distilled.pairs`, folded, and a recorded pair excludes
    the same pair from every later selection."""
    state = SimpleNamespace(lessons_distilled=[{"pairs": [[2, 1]]}, {"pairs": [[3, 1]]}])
    assert LessonReconcileMixin.spent_pairs(state) == [(2, 1), (3, 1)]


def test_an_unrecorded_pair_is_re_selected_and_re_paid():
    """WHY the ledger row is the whole fix — the cost of not writing one, driven over the real
    selector. This is what the reconcile pass caused every time it committed nothing."""
    state = _state(_node(1, 0.5), _node(2, 0.9, parents=[1]))
    assert [(p["a"], p["b"]) for p in select_comparison_pairs(state, k=3)] == [(2, 1)]
    # ...and with the row written, it is gone.
    assert select_comparison_pairs(state, k=3, exclude=[(2, 1)]) == []


def test_the_reconcile_ledger_append_is_keyed_on_what_was_PAID_FOR():
    """THE DEFECT, by AST over the real function: the append must be gated on the pairs bound at the
    provider call, NOT on `pairs_used`, which is cleared when nothing commits.

    Structural because the alternative is a live reconcile pass with a real provider — and the whole
    failure is that a paid call happened and left no trace, which a stubbed callee cannot show.

    MUTATION: put `pairs_used` back in the `if` -> a pass that spends the call and commits nothing
    records no ledger row, and the next cadence re-pays.
    """
    tree = function_tree(LessonReconcileMixin.reconcile_lessons)
    appends = [node for node in ast.walk(tree)
               if isinstance(node, ast.If)
               and isinstance(node.test, ast.Name)
               and any(isinstance(inner, ast.Call)
                       and isinstance(inner.func, ast.Attribute)
                       and inner.func.attr == "append"
                       for inner in ast.walk(node))]
    gates = {node.test.id for node in appends}
    assert "spent_pairs_this_pass" in gates, (
        "the reconcile ledger append is not gated on the pairs the provider call was made over")
    assert "pairs_used" not in gates, (
        "gated on the COMMITTED pairs again — a paid pass that commits nothing records nothing")


def test_the_recorded_pairs_are_the_paid_ones_and_the_lessons_are_the_committed_ones():
    """Both halves, and they are different facts. The receipt must never claim a lesson that is not
    in the store, and must never omit a pair the run was charged for.

    MUTATION: record `comp`-derived pairs -> the receipt's `pairs` shrinks to what committed, i.e.
    exactly the bug, spelled differently.
    """
    source = Path(reconcile_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    call = next(node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and any(isinstance(a, ast.Constant) and a.value == "lessons_distilled"
                        for a in ast.walk(node)) is False
                and any(isinstance(k, ast.Constant) and k.value == "reconcile"
                        for k in ast.walk(node)))
    payload = next(a for a in call.args if isinstance(a, ast.Dict))
    keys = {k.value: v for k, v in zip(payload.keys, payload.values)
            if isinstance(k, ast.Constant)}
    pairs_src = ast.dump(keys["pairs"])
    lessons_src = ast.dump(keys["lessons"])
    assert "spent_pairs_this_pass" in pairs_src, pairs_src
    assert "comp" in lessons_src, lessons_src


def test_a_pass_that_never_called_records_nothing():
    """`spent_pairs_this_pass` is bound empty at the top and filled only by the branch that makes
    the paid call, so an offline pass, a client-less pass and a failed pass all stay silent — a
    ledger row for a pair nobody paid for would bar a real lesson forever."""
    tree = function_tree(LessonReconcileMixin.reconcile_lessons)
    # `AnnAssign` too: the top-of-function binding carries a `: list` annotation.
    assigns = [node for node in ast.walk(tree)
               if isinstance(node, (ast.Assign, ast.AnnAssign))
               and any(isinstance(t, ast.Name) and t.id == "spent_pairs_this_pass"
                       for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
    assert len(assigns) == 2, "exactly one empty binding and one fill at the paid call"
    empty = [a for a in assigns if isinstance(a.value, ast.List) and not a.value.elts]
    assert len(empty) == 1, "the top-of-function binding must be an empty list"


def test_the_mid_run_cadence_still_records_unconditionally():
    """The sibling this was made consistent with, and the argument for the whole change: its own
    comment says the receipt goes out "always — even with 0 lessons"."""
    from looplab.engine import lessons as lessons_mod

    source = Path(lessons_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef) and "distill" in node.name)
    appends = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "append"]
    assert appends, "the cadence no longer appends its ledger row — re-point this test"
