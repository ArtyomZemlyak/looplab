"""R1-a — SearchFitness, the single owner of the run's ordering / eligibility decision.

Locks the byte-identical contract the fold's `_select_best`, the policies' `rank_by_metric`, and
`holdout_topk` now delegate to: one direction comparator, one raw-metric ranking, the `(robust_metric,
id)` / `(holdout_metric, id)` promotion keys, and the one eligibility predicate. (End-to-end
byte-identity of selection is separately locked by test_golden_replay + test_events_replay.)"""
from __future__ import annotations

from looplab.core.fitness import SearchFitness, is_better
from looplab.core.models import Idea, Node, NodeStatus, RunState


def _node(nid, metric=None, *, feasible=True, confirmed_mean=None, holdout_metric=None,
          status=NodeStatus.evaluated):
    return Node(id=nid, operator="draft", idea=Idea(operator="draft"), metric=metric, status=status,
                feasible=feasible, confirmed_mean=confirmed_mean, holdout_metric=holdout_metric)


# --------------------------------------------------------------------------- #
# Comparator
# --------------------------------------------------------------------------- #

def test_is_better_both_directions():
    assert is_better("max", 2.0, 1.0) and not is_better("max", 1.0, 2.0)
    assert is_better("min", 1.0, 2.0) and not is_better("min", 2.0, 1.0)
    assert SearchFitness("max").is_better(2.0, 1.0) and SearchFitness("min").is_better(1.0, 2.0)


def test_runstate_is_better_delegates():
    # RunState.is_better must be byte-identical to the owner (it now delegates to it)
    for d in ("max", "min"):
        rs, f = RunState(direction=d), SearchFitness(d)
        assert rs.is_better(0.6, 0.5) == f.is_better(0.6, 0.5)
        assert rs.is_better(0.5, 0.6) == f.is_better(0.5, 0.6)


# --------------------------------------------------------------------------- #
# best() argmin/argmax + deterministic tie-break
# --------------------------------------------------------------------------- #

def test_best_picks_extreme_and_breaks_ties_by_id():
    nodes = [_node(0, 0.5), _node(1, 0.9), _node(2, 0.9)]
    # max: highest metric; tie 0.9 between #1 and #2 -> highest id wins (max over (metric, id))
    assert SearchFitness("max").best(nodes, key=SearchFitness.selection_key).id == 2
    # min: lowest metric -> #0
    assert SearchFitness("min").best(nodes, key=SearchFitness.selection_key).id == 0


# --------------------------------------------------------------------------- #
# rank() — the raw-metric ordering the policies share
# --------------------------------------------------------------------------- #

def test_rank_is_best_first_with_id_tiebreak():
    nodes = [_node(0, 0.5), _node(1, 0.9), _node(2, 0.7), _node(3, 0.9)]
    ids_max = [n.id for n in SearchFitness("max").rank(nodes)]
    assert ids_max == [3, 1, 2, 0]                 # 0.9(id3),0.9(id1) desc → 0.7 → 0.5
    ids_min = [n.id for n in SearchFitness("min").rank(nodes)]
    assert ids_min == [0, 2, 1, 3]                 # ascending; 0.9 tie keeps ascending id


def test_rank_matches_rank_by_metric_delegation():
    from looplab.search.policy import rank_by_metric
    st = RunState(direction="max")
    nodes = [_node(i, m) for i, m in enumerate([0.3, 0.8, 0.8, 0.1])]
    assert [n.id for n in rank_by_metric(st, nodes)] == [n.id for n in SearchFitness("max").rank(nodes)]


# --------------------------------------------------------------------------- #
# promotion keys
# --------------------------------------------------------------------------- #

def test_selection_key_uses_robust_metric():
    # robust_metric = confirmed_mean when present, else raw metric
    assert SearchFitness.selection_key(_node(4, 0.5)) == (0.5, 4)
    assert SearchFitness.selection_key(_node(4, 0.5, confirmed_mean=0.7)) == (0.7, 4)


def test_holdout_key_uses_holdout_metric():
    assert SearchFitness.holdout_key(_node(3, 0.5, holdout_metric=0.42)) == (0.42, 3)


def test_promotion_key_plain_when_tiebreak_off():
    # tie-break off -> plain (robust_metric, id), byte-identical to selection_key
    f = SearchFitness("max", verifier_tiebreak=False)
    n = _node(2, 0.5)
    assert f.promotion_key(n) == (0.5, 2) == SearchFitness.selection_key(n)


def test_promotion_key_inserts_direction_oriented_verifier_slot():
    n = _node(2, 0.5)
    n.verifier_score = 0.8
    # max: +score between metric and id; min: -score (so a HIGHER score always wins the metric-tie)
    assert SearchFitness("max", verifier_tiebreak=True).promotion_key(n) == (0.5, 0.8, 2)
    assert SearchFitness("min", verifier_tiebreak=True).promotion_key(n) == (0.5, -0.8, 2)
    # an unscored node contributes the neutral midpoint (0.5), oriented by direction
    u = _node(3, 0.5)
    assert SearchFitness("max", verifier_tiebreak=True).promotion_key(u) == (0.5, 0.5, 3)
    assert SearchFitness("min", verifier_tiebreak=True).promotion_key(u) == (0.5, -0.5, 3)


# --------------------------------------------------------------------------- #
# eligibility predicate
# --------------------------------------------------------------------------- #

def test_eligible_predicate():
    ok = _node(0, 0.5)
    assert SearchFitness.eligible(ok, flagged=set(), aborted=set())
    assert not SearchFitness.eligible(_node(1, 0.5, feasible=False), set(), set())   # infeasible
    assert not SearchFitness.eligible(ok, flagged={0}, aborted=set())                # trust-flagged
    assert not SearchFitness.eligible(ok, flagged=set(), aborted={0})                # aborted
    assert not SearchFitness.eligible(_node(2, None), set(), set())                  # no robust metric
