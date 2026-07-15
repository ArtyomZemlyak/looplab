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


def _cn(nid, mean, *, std=None, seeds=None, score=None):
    """A confirmed node with optional confirm-noise + verifier score, for the R1-d CI-tie tests."""
    n = Node(id=nid, operator="draft", idea=Idea(operator="draft"), metric=mean,
             status=NodeStatus.evaluated, feasible=True, confirmed_mean=mean)
    n.confirmed_std, n.confirmed_seeds, n.verifier_score = std, seeds, score
    return n


# --------------------------------------------------------------------------- #
# R1-d CI-tie (§21.19) — statistical tie-break, §21.7-safe
# --------------------------------------------------------------------------- #

def test_ci_tie_off_equals_exact_promotion_pick():
    f_off = SearchFitness("max", verifier_tiebreak=True, ci_tie=False)
    nodes = [_cn(0, 0.90, std=0.05, seeds=3, score=0.9), _cn(1, 0.905, std=0.05, seeds=3, score=0.1)]
    # ci_tie off -> exact-tie: distinct means -> the metric leader (id 1, 0.905) wins regardless of score
    assert f_off.best_ci(nodes).id == 1


def test_ci_tie_never_overrides_a_significant_difference():
    """§21.7: a mean better by MORE than the confirm noise is NOT tied — a higher soundness cannot steal it."""
    f = SearchFitness("max", verifier_tiebreak=True, ci_tie=True)
    # A: mean 0.90, TIGHT noise (std 0.01, 4 seeds -> SE 0.005), LOW soundness 0.1
    # B: mean 0.80, tight noise, HIGH soundness 0.9. |Δ|=0.10 >> 1.96·√(2·0.005²)=0.014 -> NOT tied.
    a = _cn(0, 0.90, std=0.01, seeds=4, score=0.1)
    b = _cn(1, 0.80, std=0.01, seeds=4, score=0.9)
    assert f.best_ci([a, b]).id == 0            # the significantly-better mean wins; soundness can't override


def test_ci_tie_breaks_a_statistical_tie_by_soundness():
    """Within the confirm noise, the sounder node wins — even if it is NOT the raw metric leader."""
    f = SearchFitness("max", verifier_tiebreak=True, ci_tie=True)
    # A is the raw leader (0.905) but LOW soundness; B is 0.90 with HIGH soundness. SE=0.05/√3=0.029,
    # CI=1.96·√(2·0.029²)=0.080; |Δ|=0.005 <= 0.080 -> statistically TIED -> the sounder B wins.
    a = _cn(0, 0.905, std=0.05, seeds=3, score=0.2)
    b = _cn(1, 0.90, std=0.05, seeds=3, score=0.95)
    assert f.best_ci([a, b]).id == 1
    # min direction: symmetric — leader is the LOWEST mean, tie broken by soundness
    fmin = SearchFitness("min", verifier_tiebreak=True, ci_tie=True)
    assert fmin.best_ci([_cn(0, 0.10, std=0.05, seeds=3, score=0.2),
                         _cn(1, 0.105, std=0.05, seeds=3, score=0.95)]).id == 1


def test_ci_tie_unscored_tieset_falls_back_to_metric_leader():
    """Review finding #1: an all-UNSCORED (or equal-soundness) statistical tie-set must resolve to the
    METRIC LEADER, never an arbitrary id — the verifier gives no signal, so it must not move the champion."""
    f = SearchFitness("max", verifier_tiebreak=True, ci_tie=True)
    # both within the leader's noise, NEITHER scored -> the metric leader (0.905, id 0) must win, not id 1
    a = _cn(0, 0.905, std=0.05, seeds=3, score=None)
    b = _cn(1, 0.90, std=0.05, seeds=3, score=None)
    assert f.best_ci([a, b]).id == 0
    # equal EXPLICIT scores -> still the metric leader
    assert f.best_ci([_cn(0, 0.905, std=0.05, seeds=3, score=0.7),
                      _cn(1, 0.90, std=0.05, seeds=3, score=0.7)]).id == 0


def test_ci_tie_candidate_variance_cannot_widen_the_band():
    """Review finding #2: a candidate's OWN inflated confirmed_std must NOT drag a genuinely-better tight
    leader into a tie — the band is anchored on the LEADER's precision only."""
    f = SearchFitness("max", verifier_tiebreak=True, ci_tie=True)
    # leader tight (std 0.001), candidate 0.20 worse but wildly noisy (std 5.0) + high soundness.
    # band = 1.96·SE_leader (tiny) -> |Δ|=0.20 NOT tied -> the leader wins; C's variance can't steal it.
    leader = _cn(0, 0.90, std=0.001, seeds=4, score=0.1)
    noisy = _cn(1, 0.70, std=5.0, seeds=3, score=0.9)
    assert f.best_ci([leader, noisy]).id == 0


def test_ci_tie_rejects_boolean_confirmed_std_no_21_7_violation():
    """Workflow finding: a foreign/hand-edited `confirmed_std: true` (bool is an int/float subclass) must
    NOT become std=1.0 and inflate the band to swallow a genuinely-better metric — §21.7. `_se` rejects it,
    so the pair falls back to exact-tie: the metric leader wins despite a lower-metric node's high soundness."""
    f = SearchFitness("max", verifier_tiebreak=True, ci_tie=True)
    leader = _cn(0, 0.90, std=0.01, seeds=4, score=0.1)
    bogus = _cn(1, 0.70, std=True, seeds=True, score=0.9)      # boolean noise data
    assert f.best_ci([leader, bogus]).id == 0                  # leader wins; bool std can't widen the band


def test_ci_tie_falls_back_to_exact_without_noise_data():
    """No confirmed_std/seeds -> no SE -> only EXACT-metric ties are broken (never a fabricated band)."""
    f = SearchFitness("max", verifier_tiebreak=True, ci_tie=True)
    # near but distinct means, NO noise data -> not tied -> metric leader (id 1) wins despite lower score
    assert f.best_ci([_cn(0, 0.90, score=0.9), _cn(1, 0.905, score=0.1)]).id == 1
    # EXACT tie with no noise data -> still broken by soundness
    assert f.best_ci([_cn(0, 0.90, score=0.2), _cn(1, 0.90, score=0.9)]).id == 1


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


def test_holdout_key_plain_when_tiebreak_off():
    assert SearchFitness("max").holdout_key(_node(3, 0.5, holdout_metric=0.42)) == (0.42, 3)


def test_holdout_key_inserts_verifier_slot_when_on():
    n = _node(3, 0.5, holdout_metric=0.42)
    n.verifier_score = 0.7
    assert SearchFitness("max", verifier_tiebreak=True).holdout_key(n) == (0.42, 0.7, 3)
    assert SearchFitness("min", verifier_tiebreak=True).holdout_key(n) == (0.42, -0.7, 3)
    # unscored -> neutral midpoint, direction-oriented
    u = _node(4, 0.5, holdout_metric=0.42)
    assert SearchFitness("max", verifier_tiebreak=True).holdout_key(u) == (0.42, 0.5, 4)


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
