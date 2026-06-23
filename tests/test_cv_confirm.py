"""I8 consistent-CV harness + I12 multi-seed top-k confirmation."""
from __future__ import annotations

from looplab.confirm import confirm_top_k
from looplab.cv import (consistent_cv, cv_summary, kfold_indices,
                        purged_walk_forward)
from looplab.models import Idea, Node, NodeStatus


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
