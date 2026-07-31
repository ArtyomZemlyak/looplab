"""I8 consistent-CV harness + I12 multi-seed top-k confirmation."""
from __future__ import annotations

from looplab.trust.confirm import confirm_top_k
from looplab.trust.cv import (consistent_cv, cv_summary, kfold_indices,
                        purged_walk_forward)
from looplab.core.models import Idea, Node, NodeStatus


# ------------------------------- I8 CV ------------------------------------- #
def test_kfold_partitions_exactly():
    splits = kfold_indices(10, 5)
    assert len(splits) == 5
    seen = []
    for train, test in splits:
        assert set(train).isdisjoint(test)           # train/test disjoint
        assert set(train) | set(test) == set(range(10))  # cover all
        seen += test
    assert sorted(seen) == list(range(10))            # every index tested once


def test_purged_walk_forward_no_lookahead():
    splits = purged_walk_forward(20, n_splits=3, embargo=2)
    assert splits
    for train, test in splits:
        assert max(train) < min(test)                 # train strictly before test
        assert min(test) - max(train) > 2             # embargo gap honored


def test_consistent_cv_same_splits():
    splits = kfold_indices(9, 3)
    scores = consistent_cv(lambda tr, te: float(len(te)), splits)
    summ = cv_summary(scores)
    assert summ["n"] == 3 and summ["mean"] == 3.0


# ------------------------------- I12 confirm ------------------------------- #
def _node(i, metric):
    return Node(id=i, operator="improve",
                idea=Idea(operator="improve", params={"x": float(i)}),
                metric=metric, status=NodeStatus.evaluated)


def test_confirmation_demotes_seed_lucky_leader():
    # node 0 has the best single metric (0.0) but is noisy (mean ~1.0);
    # node 1 is slightly worse single (0.5) but stable -> should win on the mean.
    nodes = [_node(0, 0.0), _node(1, 0.5)]

    def eval_fn(node, seed):
        if node.id == 0:
            return [2.0, 0.0, 2.0, 0.0][seed % 4]  # mean 1.0, high variance
        return 0.5                                  # stable

    out = confirm_top_k(nodes, eval_fn, k=2, seeds=[0, 1, 2, 3], direction="min")
    assert out["best_node_id"] == 1                 # robust winner
    assert out["demoted_single_leader"] is True     # the lucky leader was demoted


# #38 — confirm_top_k does not crown a node that produced zero usable seed scores
def test_confirm_top_k_skips_scoreless_node():
    nodes = [Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
                  status=NodeStatus.evaluated)]
    res = confirm_top_k(nodes, lambda n, s: 0.0, k=1, seeds=[], direction="min")
    assert res["best_node_id"] is None                             # no seeds -> no fabricated 0.0 winner


def test_confirm_top_k_ignores_unscored_nodes_instead_of_raising():
    """`None` is unorderable, so a single unevaluated node in the input made `sorted` raise TypeError
    out of this public helper. An unscored node has no claim on a top-k slot anyway — the same rule
    the zero-usable-seeds guard already applies further down."""
    from looplab.core.models import Idea, Node
    from looplab.trust.confirm import confirm_top_k

    def _node(nid, metric):
        return Node(id=nid, parent_ids=[], operator="draft",
                    idea=Idea(operator="draft", params={}, rationale=""), metric=metric)

    nodes = [_node(0, 0.5), _node(1, None), _node(2, 0.1)]
    out = confirm_top_k(nodes, lambda n, s: n.metric + 0.01 * s, k=2, seeds=[0, 1], direction="min")
    assert out["best_node_id"] == 2
    assert [row["node_id"] for row in out["summaries"]] == [2, 0]     # the unscored node is absent

    # Nothing scored at all is "nothing to confirm", not a crash.
    empty = confirm_top_k([_node(3, None)], lambda n, s: 0.0, k=2, seeds=[0], direction="min")
    assert empty["best_node_id"] is None and empty["summaries"] == []
