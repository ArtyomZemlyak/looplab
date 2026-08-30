"""A repair self-pair is spent once, like every other pair.

`select_comparison_pairs`' contract says `exclude` is the "(child, parent) id tuples already
distilled (later firings must not re-spend LLM budget on the same pair)", and the parent loop tests
it. The IN-NODE repair self-pair — `{"kind": "debug", "a": n.id, "b": n.id}` — did not, while
`lessons_reconcile.spent_pairs` DOES return a self-pair once one is distilled, because the lesson's
`pairs` row round-trips through `lessons_distilled`.

THE COST IS STARVATION, NOT ONLY BUDGET. `debug` sorts FIRST and `k` defaults to 3, so every repaired
node with a metric re-occupied a top slot on every distillation cadence AND at run-end reflection —
the same pair re-sent to the paid comparative-lesson call each firing. Once a run held three such
nodes (`runs/e5small-dr-unified-v4` held four) NO NEW SOLUTION PAIR COULD EVER REACH THE JUDGE.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.engine.memory import select_comparison_pairs


class _Node(SimpleNamespace):
    pass


def _node(nid, metric, *, repairs=0, parents=(), status=None):
    return _Node(id=nid, metric=metric, repairs=repairs, parent_ids=list(parents),
                 tombstoned=False, status=status)


def _state(*nodes, direction="max"):
    return SimpleNamespace(nodes={n.id: n for n in nodes}, direction=direction,
                           aborted_nodes=[], best_node_id=None, nodes_flagged=[],
                           trust_gate="audit")


def test_a_repaired_node_offers_a_self_pair_the_FIRST_time():
    """Precondition, and the behaviour the self-pair exists for: 'what code change fixed a crash'
    mostly happens INSIDE one node.

    Mutation: drop the self-pair append entirely and the developer half of the lesson store goes
    back to being empty from the day it shipped."""
    pairs = select_comparison_pairs(_state(_node(0, 0.7, repairs=1)), k=3)
    assert {"kind": "debug", "a": 0, "b": 0, "delta": None} in pairs


def test_the_SAME_self_pair_is_not_re_selected_once_distilled():
    """THE DEFECT. Mutation: drop the `(n.id, n.id) not in excl` test and this pair returns on every
    later cadence and at run-end reflection, re-spending the paid call each time."""
    state = _state(_node(0, 0.7, repairs=1))
    pairs = select_comparison_pairs(state, k=3, exclude=[(0, 0)])
    assert not [p for p in pairs if p["a"] == 0 and p["b"] == 0], (
        f"an already-distilled self-pair must not be re-selected, got {pairs}")


def test_a_SPENT_self_pair_stops_starving_new_solution_pairs():
    """The consequence that makes this correctness and not cost. `debug` sorts FIRST and k=3, so
    three spent self-pairs used to occupy every slot and no NEW solution pair could reach the judge.

    Mutation: drop the gate and the three debug rows fill k, leaving the real improvement out.
    """
    state = _state(
        _node(0, 0.70, repairs=1), _node(1, 0.71, repairs=1), _node(2, 0.72, repairs=1),
        _node(3, 0.60), _node(4, 0.90, parents=(3,)),          # a genuine, unspent improvement
    )
    spent = [(0, 0), (1, 1), (2, 2)]
    pairs = select_comparison_pairs(state, k=3, exclude=spent)
    assert any(p["kind"] == "solution" and p["a"] == 4 and p["b"] == 3 for p in pairs), (
        f"a new solution pair must be reachable once the self-pairs are spent, got {pairs}")


def test_an_UNSPENT_self_pair_of_a_DIFFERENT_node_is_untouched():
    """The gate is keyed on the node, not on 'any self-pair was spent'.

    Mutation: exclude every self-pair as soon as one is spent, and a newly repaired node never
    contributes its lesson at all."""
    state = _state(_node(0, 0.7, repairs=1), _node(1, 0.8, repairs=1))
    pairs = select_comparison_pairs(state, k=3, exclude=[(0, 0)])
    assert any(p["a"] == 1 and p["b"] == 1 for p in pairs)
    assert not [p for p in pairs if p["a"] == 0 and p["b"] == 0]


def test_the_PARENT_pair_exclusion_still_works():
    """The rule this fix copied, pinned so the two stay one behaviour.

    Mutation: drop the parent loop's `(n.id, pid) in excl` test and ordinary solution pairs
    re-spend too."""
    state = _state(_node(0, 0.60), _node(1, 0.90, parents=(0,)))
    assert select_comparison_pairs(state, k=3, exclude=[(1, 0)]) == []
