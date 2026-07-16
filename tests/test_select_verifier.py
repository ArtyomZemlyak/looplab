"""R1-c — persisted calibrated-verifier tie treatment in best-selection (Part IV unblock).

Locks: new producers commit one evidence-bound ``verifier_group_scored`` event for the complete
selector-reachable tie; replay validates it all-or-none. Legacy ``node_verified`` rows remain readable but a
torn group fails closed. Soundness never overrides a strictly-better metric (§21.7), and the cadence is a
no-op when the recorded run contract is off / has no tie / has no client.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT, verifier_evidence_digest
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _run(tmp_path, direction="max", *, select_verifier=False, samples=3,
         holdout_select=False, verifier_ci_tie=False) -> EventStore:
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": direction,
                             "select_verifier": select_verifier,
                             "select_verifier_samples": samples,
                             "holdout_select": holdout_select,
                             "verifier_ci_tie": verifier_ci_tie})
    return s


def _add(s: EventStore, nid: int, metric: float) -> None:
    s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": f"exp {nid}"}})
    s.append("node_evaluated", {"node_id": nid, "metric": metric})


# --------------------------------------------------------------------------- #
# Fold: node_verified -> Node.verifier_score
# --------------------------------------------------------------------------- #

def test_node_verified_folds_into_score(tmp_path):
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.73})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.73


def test_node_verified_without_generation_is_dropped(tmp_path):
    # a BRAND-NEW selection-affecting event has no legacy logs, so a missing generation stamp is REJECTED
    # (not accepted-as-current) — a forged/hand-edited unscoped score can't bias selection.
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "score": 0.8})     # no generation
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_node_verified_stale_generation_is_dropped(tmp_path):
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})           # bumps attempt -> 1
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.9})
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})   # stale gen 0 -> dropped
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_node_verified_out_of_range_ignored(tmp_path):
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 1.5})   # not in [0,1]
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": True})  # bool is not a score
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_verifier_score_cleared_on_reset(tmp_path):
    # verify-then-reset: a soundness score judged the OLD attempt's result, so a reset must scrub it
    # (like proxy_scores / holdout_metric) — else it survives onto a re-evaluated, different result.
    s = _run(tmp_path)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.88})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.88
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})    # attempt -> 1
    assert fold(s.read_all()).nodes[0].verifier_score is None


def test_stale_verifier_score_does_not_bias_the_new_attempt(tmp_path):
    # the reviewer's scenario: an attempt-0 soundness score must NOT decide the tie for the DIFFERENT
    # attempt-1 result. After the reset-clear, node 0's re-eval is unscored (neutral) like node 1.
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.88})   # attempt-0 result
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})           # discard attempt 0
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.5})  # new, different result
    _add(s, 1, 0.5)                                                             # node 1 ties at 0.5
    # node 0's stale 0.88 was cleared -> both neutral 0.5 -> id tie-break -> max picks #1, NOT the
    # stale-score-boosted #0 (which is what happened before the reset-clear fix)
    assert fold(s.read_all()).best_node_id == 1


def test_verifier_score_tracks_confirm_and_holdout_evidence_revision(tmp_path):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    initial = fold(s.read_all())
    old_digest = verifier_evidence_digest(initial.direction, initial.nodes[0])
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9,
                               "evidence_digest": old_digest})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.9

    # Confirmation changes the evidence: the prior score is cleared, and an in-flight verdict carrying
    # the pre-confirm digest is rejected even if it lands after the confirmation event.
    s.append("node_confirmed", {"node_id": 0, "generation": 0,
                                "mean": 0.88, "std": 0.01, "seeds": 3})
    assert fold(s.read_all()).nodes[0].verifier_score is None
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.99,
                               "evidence_digest": old_digest})
    assert fold(s.read_all()).nodes[0].verifier_score is None

    confirmed = fold(s.read_all())
    confirmed_digest = verifier_evidence_digest(confirmed.direction, confirmed.nodes[0])
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.8,
                               "evidence_digest": confirmed_digest})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.8
    # Variance is part of CI tie membership. Even with the same mean/seed count, a new std is a different
    # selector evidence revision and must invalidate the old treatment.
    s.append("node_confirmed", {"node_id": 0, "generation": 0,
                                "mean": 0.88, "std": 0.02, "seeds": 3})
    revised = fold(s.read_all())
    assert revised.nodes[0].verifier_score is None
    assert verifier_evidence_digest(revised.direction, revised.nodes[0]) != confirmed_digest
    revised_digest = verifier_evidence_digest(revised.direction, revised.nodes[0])
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.8,
                               "evidence_digest": revised_digest})
    _holdout(s, 0, 0.84)
    assert fold(s.read_all()).nodes[0].verifier_score is None


@pytest.mark.parametrize("new_evidence", ["confirmation", "holdout"])
def test_digestless_legacy_score_cannot_return_after_revisioned_evidence(tmp_path, new_evidence):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    # Digestless legacy rows remain readable while the score refers only to the raw metric.
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.7})
    assert fold(s.read_all()).nodes[0].verifier_score == 0.7

    if new_evidence == "confirmation":
        s.append("node_confirmed", {"node_id": 0, "generation": 0,
                                    "mean": 0.88, "std": 0.01, "seeds": 3})
    else:
        s.append("holdout_evaluated", {"node_id": 0, "generation": 0,
                                        "search_epoch": 0, "metric": 0.84})
    assert fold(s.read_all()).nodes[0].verifier_score is None

    # A late pre-revision legacy result must not restore the invalidated score.  Modern producers can
    # still publish a score by binding it to the current evidence digest.
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.99})
    assert fold(s.read_all()).nodes[0].verifier_score is None


@pytest.mark.parametrize("invalid_field", [
    {"mean": float("nan")},
    {"std": float("inf")},
    {"std": -0.01},
    {"seeds": 0},
    {"seeds": True},
    {"seeds": "3"},
])
def test_malformed_confirmation_cannot_overwrite_valid_evidence(tmp_path, invalid_field):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    s.append("node_confirmed", {"node_id": 0, "generation": 0,
                                "mean": 0.88, "std": 0.02, "seeds": 3})
    confirmed = fold(s.read_all())
    digest = verifier_evidence_digest(confirmed.direction, confirmed.nodes[0])
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.81,
                               "evidence_digest": digest})

    malformed = {"node_id": 0, "generation": 0, "mean": 0.70, "std": 0.01, "seeds": 5}
    malformed.update(invalid_field)
    s.append("node_confirmed", malformed)
    node = fold(s.read_all()).nodes[0]
    assert (node.confirmed_mean, node.confirmed_std, node.confirmed_seeds) == (0.88, 0.02, 3)
    assert node.verifier_score == 0.81


def test_old_logs_default_off(tmp_path):
    s = _run(tmp_path)                                                     # no select_verifier key
    _add(s, 0, 0.9)
    st = fold(s.read_all())
    assert st.nodes[0].verifier_score is None and st.select_verifier_tiebreak is False


# --------------------------------------------------------------------------- #
# Fold: the tie-break in best-selection
# --------------------------------------------------------------------------- #

def test_tiebreak_off_ignores_score_and_uses_id(tmp_path):
    s = _run(tmp_path, "max", select_verifier=False)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                                                        # exact tie
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})
    assert fold(s.read_all()).best_node_id == 1     # flag off -> score ignored -> max picks highest id


def test_tiebreak_on_prefers_higher_soundness(tmp_path):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                                                        # exact tie; max would pick #1
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9})
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.2})
    assert fold(s.read_all()).best_node_id == 0     # #0 more sound -> wins the tie despite the lower id


def test_tiebreak_never_overrides_a_better_metric(tmp_path):
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.95)                                                       # strictly better metric
    _add(s, 1, 0.90)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.1})    # ...but low soundness
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.99})   # #1 high soundness
    assert fold(s.read_all()).best_node_id == 0     # the metric wins; the advisory score can't override it


def test_tiebreak_min_direction(tmp_path):
    s = _run(tmp_path, "min", select_verifier=True)
    _add(s, 0, 0.1)
    _add(s, 1, 0.1)                                                        # exact tie; min would pick #0
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.2})
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.9})
    assert fold(s.read_all()).best_node_id == 1     # #1 more sound -> wins despite the higher id


def test_torn_legacy_treatment_fails_closed_to_metric_id_order(tmp_path):
    # A legacy per-node prefix can exist if an old process crashed between appends. It remains readable,
    # but may not decide the tie until every contender has a score from one complete treatment.
    s = _run(tmp_path, "max", select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.8})   # #1 stays unscored (neutral)
    assert fold(s.read_all()).best_node_id == 1


# --------------------------------------------------------------------------- #
# Fold: the tie-break on the HOLDOUT path (holdout_select on)
# --------------------------------------------------------------------------- #

def _holdout(s, nid, m):
    s.append("holdout_evaluated", {"node_id": nid, "generation": 0, "search_epoch": 0, "metric": m})


def _group_record(state, node_ids, scores=None):
    scores = scores or [0.9 - 0.1 * i for i in range(len(node_ids))]
    return {
        "v": 1,
        "contract": VERIFIER_SELECTION_CONTRACT,
        "requested_samples": state.select_verifier_samples,
        "members": [{
            "node_id": nid,
            "generation": state.nodes[nid].attempt,
            "score": score,
            "n_samples": state.select_verifier_samples,
            "agreement": 1.0,
            "method": "llm",
            "evidence_digest": verifier_evidence_digest(state.direction, state.nodes[nid]),
        } for nid, score in zip(node_ids, scores)],
    }


def test_holdout_override_breaks_tie_by_verifier(tmp_path):
    # holdout_select on: the final champion is the holdout pick. A tie on the UNSEEN-signal metric must
    # be broken by soundness too — so select_verifier actually influences the champion on this path.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max",
                             "select_verifier": True, "holdout_select": True})
    _add(s, 0, 0.85)
    _add(s, 1, 0.90)                     # the mean pick alone would prefer #1 (higher search metric)
    _holdout(s, 0, 0.80)
    _holdout(s, 1, 0.80)                 # ...but they TIE on the holdout metric
    evidence = fold(s.read_all())
    s.append("node_verified", {"node_id": 0, "generation": 0, "score": 0.9,
                               "evidence_digest": verifier_evidence_digest(
                                   evidence.direction, evidence.nodes[0])})   # #0 sounder
    s.append("node_verified", {"node_id": 1, "generation": 0, "score": 0.2,
                               "evidence_digest": verifier_evidence_digest(
                                   evidence.direction, evidence.nodes[1])})
    assert fold(s.read_all()).best_node_id == 0     # holdout override breaks the 0.80 tie by soundness


def test_holdout_override_tie_falls_to_id_when_verifier_off(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max", "holdout_select": True})
    _add(s, 0, 0.85)
    _add(s, 1, 0.90)
    _holdout(s, 0, 0.80)
    _holdout(s, 1, 0.80)
    assert fold(s.read_all()).best_node_id == 1     # flag off -> legacy max-id holdout tie-break


def test_atomic_group_event_publishes_all_scores_together(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=1)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    before = fold(s.read_all())
    s.append("verifier_group_scored", _group_record(before, [0, 1], [0.95, 0.1]))
    after = fold(s.read_all())
    assert [after.nodes[nid].verifier_score for nid in (0, 1)] == [0.95, 0.1]
    assert after.best_node_id == 0


def test_atomic_group_rejects_one_invalid_member_without_a_prefix(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=1)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    data = _group_record(fold(s.read_all()), [0, 1])
    data["members"][1]["evidence_digest"] = "0" * 64
    s.append("verifier_group_scored", data)
    state = fold(s.read_all())
    assert state.nodes[0].verifier_score is None
    assert state.nodes[1].verifier_score is None


def test_atomic_group_rejects_invalid_contract_generation_sampling_and_ids(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=3)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    state = fold(s.read_all())

    bad = _group_record(state, [0, 1])
    bad["contract"] = "selection-criteria:v999"
    s.append("verifier_group_scored", bad)
    bad = _group_record(state, [0, 1])
    bad["members"][0]["generation"] = 99
    s.append("verifier_group_scored", bad)
    bad = _group_record(state, [0, 1])
    bad["members"][0]["n_samples"] = 1  # not a strict majority of the three requested samples
    s.append("verifier_group_scored", bad)
    bad = _group_record(state, [0, 1])
    bad["members"][0]["agreement"] = 0.5
    s.append("verifier_group_scored", bad)
    bad = _group_record(state, [0, 1])
    bad["members"][0]["node_id"] = []  # malformed/unhashable foreign JSON must be an inert event
    s.append("verifier_group_scored", bad)

    after = fold(s.read_all())
    assert after.nodes[0].verifier_score is None
    assert after.nodes[1].verifier_score is None


def test_atomic_group_requires_the_complete_champion_tie(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=1)
    for nid in range(3):
        _add(s, nid, 0.9)
    data = _group_record(fold(s.read_all()), [0, 1])  # valid rows, torn/incomplete tie membership
    s.append("verifier_group_scored", data)
    state = fold(s.read_all())
    assert all(node.verifier_score is None for node in state.nodes.values())


def test_atomic_group_rejects_a_well_formed_losing_tie(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=1)
    for nid, metric in enumerate((0.9, 0.9, 0.5, 0.5)):
        _add(s, nid, metric)
    data = _group_record(fold(s.read_all()), [2, 3])
    s.append("verifier_group_scored", data)
    state = fold(s.read_all())
    assert state.nodes[2].verifier_score is None
    assert state.nodes[3].verifier_score is None


def test_atomic_group_replay_is_idempotent(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=1)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    data = _group_record(fold(s.read_all()), [0, 1], [0.2, 0.8])
    s.append("verifier_group_scored", data)
    s.append("verifier_group_scored", data)
    state = fold(s.read_all())
    assert [state.nodes[nid].verifier_score for nid in (0, 1)] == [0.2, 0.8]
    assert state.best_node_id == 1


# --------------------------------------------------------------------------- #
# Engine cadence: _maybe_verify_ties / _metric_tie_groups
# --------------------------------------------------------------------------- #

def test_metric_tie_groups_mirrors_the_confirmed_pool(tmp_path):
    # when ANY eligible node is confirmed, _select_best ranks ONLY confirmed nodes; the cadence must not
    # burn verifier calls on unconfirmed ties the fold will never consult.
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                                  # an UNCONFIRMED tie
    _add(s, 2, 0.8)
    st = fold(s.read_all())
    st.nodes[2].confirmed_mean = 0.8                 # a confirmation exists -> pool = confirmed-only
    assert Engine._metric_tie_groups(None, st) == []   # the unconfirmed 0.9 tie is NOT surfaced


def test_metric_tie_groups_surfaces_confirmed_ties(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.7)
    _add(s, 1, 0.7)
    _add(s, 2, 0.9)                                  # unconfirmed, higher metric — excluded once confirmed exists
    st = fold(s.read_all())
    st.nodes[0].confirmed_mean = 0.8                 # #0 and #1 confirmed & TIED on confirmed mean
    st.nodes[1].confirmed_mean = 0.8
    groups = Engine._metric_tie_groups(None, st)
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1}   # only the confirmed tie

def test_metric_tie_groups_scores_the_ci_band_when_verifier_ci_tie(tmp_path):
    """CODEX #6 regression: the producer must create verifier work for CI-tied (near-equal) nodes — the
    exact set best_ci compares — else R1-d is a no-op (CI candidates sit unscored at the neutral midpoint)."""
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.90)
    _add(s, 1, 0.905)                            # DIFFERENT robust metrics -> NO exact tie
    st = fold(s.read_all())
    for nid, m in ((0, 0.90), (1, 0.905)):       # make them confirmed + CI-tied (SE=0.05/√3=0.029)
        st.nodes[nid].confirmed_mean = m
        st.nodes[nid].confirmed_std = 0.05
        st.nodes[nid].confirmed_seeds = 3

    st.verifier_ci_tie = True
    groups = Engine._metric_tie_groups(None, st)
    assert any({0, 1} <= {n.id for n in g} for g in groups)   # the CI-band is produced -> both get scored
    st.verifier_ci_tie = False
    assert Engine._metric_tie_groups(None, st) == []          # off -> distinct metrics, no group


def _cnode(nid, mean, *, std, seeds, score):
    from looplab.core.models import Idea, Node, NodeStatus
    n = Node(id=nid, operator="draft", idea=Idea(operator="draft"), metric=mean,
             status=NodeStatus.evaluated, feasible=True, confirmed_mean=mean)
    n.confirmed_std, n.confirmed_seeds, n.verifier_score = std, seeds, score
    return n


def test_non_significant_confirm_does_not_erase_best_ci(tmp_path):
    """CODEX #7 regression: a confirm certificate that found NO significant winner is a statistical tie the
    CI-tie exists to resolve — the override must NOT erase best_ci. A SIGNIFICANT certificate still wins."""
    from looplab.core.models import RunState
    from looplab.events.replay import _select_best
    st = RunState(direction="max")
    st.select_verifier_tiebreak = True
    st.verifier_ci_tie = True
    st.nodes[0] = _cnode(0, 0.905, std=0.05, seeds=3, score=0.2)   # raw leader, LOW soundness
    st.nodes[1] = _cnode(1, 0.90, std=0.05, seeds=3, score=0.95)   # CI-tied, HIGH soundness
    # confirm chose 0 but NOT significant -> the CI-tie soundness pick (id 1) must stand
    _select_best(st, set(), best_confirmed=0, best_confirmed_significant=False)
    assert st.best_node_id == 1
    # a SIGNIFICANT confirm of 0 DOES override (the confirm separated them) -> id 0
    _select_best(st, set(), best_confirmed=0, best_confirmed_significant=True)
    assert st.best_node_id == 0
    # ci_tie OFF -> unconditional override, byte-identical to before (even non-significant) -> id 0
    st.verifier_ci_tie = False
    _select_best(st, set(), best_confirmed=0, best_confirmed_significant=False)
    assert st.best_node_id == 0


def test_best_confirmed_rejects_non_boolean_significance_atomically(tmp_path):
    s = _run(tmp_path, "min")
    _add(s, 0, 2.0)
    _add(s, 1, 1.0)
    # Truthy string "false" used to close the confirmation gate and override the raw winner with #0.
    s.append("best_confirmed", {"node_id": 0, "significant": "false"})
    rejected = fold(s.read_all())
    assert rejected.confirmed_done is False
    assert rejected.best_node_id == 1

    # Missing `significant` is the explicitly supported legacy form and still defaults to True.
    s.append("best_confirmed", {"node_id": 0})
    legacy = fold(s.read_all())
    assert legacy.confirmed_done is True
    assert legacy.best_node_id == 0


def test_metric_tie_groups_includes_holdout_ties_when_holdout_select(tmp_path):
    """R1-c holdout completeness (§21.18): nodes tied on holdout_metric but NOT on robust_metric form a
    tie-COMPONENT when holdout_select is on, so their holdout_key verifier slot finally gets produced. With
    holdout_select off, the holdout tie is not surfaced (byte-identical to the old robust-only grouping)."""
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.7)
    _add(s, 1, 0.8)                              # DIFFERENT robust metrics -> no robust tie
    st = fold(s.read_all())
    st.nodes[0].holdout_metric = 0.5             # ...but TIED on the unseen-signal holdout metric
    st.nodes[1].holdout_metric = 0.5

    st.holdout_select = True
    groups = Engine._metric_tie_groups(None, st)
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1}   # holdout tie surfaced
    # holdout_select off -> no holdout linking -> the (robust-distinct) nodes are not a tie
    st.holdout_select = False
    assert Engine._metric_tie_groups(None, st) == []


def test_metric_tie_groups_surfaces_unconfirmed_holdout_tie_past_the_confirmed_pool(tmp_path):
    """R1-c completeness gap: the holdout pick ranks the FULL eligible holdout pool, NOT the confirmed
    subset. A confirmed node coexisting with an UNCONFIRMED node tied on the holdout metric must still
    surface the unconfirmed node — else it is never scored, stays at the neutral verifier midpoint, and can
    WIN the holdout tie unverified. Regression for the confirmed-subset vs full-holdout-pool mismatch."""
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.70)                                # UNCONFIRMED (would be dropped by the mean pool)
    _add(s, 1, 0.85)
    _add(s, 2, 0.90)                                # distinct robust metrics -> only a HOLDOUT tie
    st = fold(s.read_all())
    for nid in (0, 1, 2):
        st.nodes[nid].holdout_metric = 0.80         # all TIE on the unseen-signal holdout metric
    st.nodes[1].confirmed_mean = 0.85               # 1,2 confirmed -> mean pool = {1,2} only
    st.nodes[2].confirmed_mean = 0.90

    st.holdout_select = True
    groups = Engine._metric_tie_groups(None, st)
    # the unconfirmed node 0 must join the surfaced holdout tie-component, not be dropped with the mean pool
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1, 2}


def test_metric_tie_groups_returns_only_final_selector_champion_tie(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    for nid in range(8):
        _add(s, nid, 0.1)                 # large losing tie must never consume the cadence cap
    _add(s, 8, 0.9)
    _add(s, 9, 0.9)                       # robust champion tie
    st = fold(s.read_all())
    st.nodes[6].holdout_metric = 0.8
    st.nodes[7].holdout_metric = 0.8       # final-selector champion tie
    st.nodes[8].holdout_metric = 0.7
    st.nodes[9].holdout_metric = 0.7       # losing holdout tie

    st.holdout_select = True
    groups = Engine._metric_tie_groups(None, st)
    # Holdout is applied last. Its champion tie is the only reachable comparison; the robust
    # tie and the losing holdout tie can no longer affect the answer and must not consume verifier calls.
    assert [[n.id for n in group] for group in groups] == [[6, 7]]


def test_holdout_pool_suppresses_unreachable_mean_tie_even_without_a_holdout_tie(tmp_path):
    s = _run(tmp_path, select_verifier=True, holdout_select=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)                         # mean tie
    _holdout(s, 0, 0.8)
    _holdout(s, 1, 0.7)                     # unique final holdout winner
    assert Engine._metric_tie_groups(None, fold(s.read_all())) == []


def test_holdout_mode_falls_back_to_mean_tie_until_any_holdout_score_exists(tmp_path):
    s = _run(tmp_path, select_verifier=True, holdout_select=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    groups = Engine._metric_tie_groups(None, fold(s.read_all()))
    assert [[node.id for node in group] for group in groups] == [[0, 1]]


def test_reopen_clears_stale_confirm_override(tmp_path):
    # a best_confirmed from epoch N must NOT keep overriding after a reopen to epoch N+1 — the reopen
    # clears BOTH confirmed_done AND the threaded ctx.best_confirmed the confirm-override reads.
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    _add(s, 0, 0.5)
    s.append("best_confirmed", {"node_id": 0, "significant": True, "generations": {"0": 0},
                                "search_epoch": 0})
    s.append("run_finished", {})                               # finish so the reopen rotates the epoch
    assert fold(s.read_all()).best_node_id == 0               # epoch-0 confirm override
    s.append("run_reopened", {})                              # -> epoch 1, clears the completion gates
    _add(s, 1, 0.9)                                            # a better node in the new epoch
    assert fold(s.read_all()).best_node_id == 1              # stale confirm override cleared -> metric winner


class _VClient:
    """Fake reflect client: the §12 verifier's tool call returns a fixed verdict per criterion."""
    def __init__(self, verdict="yes"):
        self.verdict = verdict
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": [self.verdict], "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _FlakyVClient:
    """Returns an UNPARSEABLE verdict on the `fail_on`-th call (that node's verify -> None)."""
    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": ["" if self.calls == self.fail_on else "yes"], "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _NoisyVClient:
    """Cycles disagreeing verdicts across samples so cross-sample agreement is low (0.33 for 3)."""
    def __init__(self):
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": [["strong_yes", "strong_no", "unclear"][(self.calls - 1) % 3]],
                "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _SparseVClient:
    """Only the first of three requested samples parses; repeated evidence has no strict quorum."""
    def __init__(self):
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"verdicts": ["yes" if self.calls == 1 else ""], "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


class _Host:
    # bind the real engine methods so internal self-calls resolve
    _maybe_verify_ties = Engine._maybe_verify_ties
    _metric_tie_groups = Engine._metric_tie_groups
    _verifier_soundness = Engine._verifier_soundness

    def __init__(self, store, *, select_verifier=True, client=None, samples=1):
        self.store = store
        self._select_verifier = select_verifier
        self._select_verifier_samples = samples
        self.researcher = None
        self.developer = None
        self._client = client

    def _reflect_client(self):
        return self._client


def test_metric_tie_groups_finds_unresolved_ties(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _add(s, 2, 0.5)
    groups = Engine._metric_tie_groups(None, fold(s.read_all()))   # self unused
    assert len(groups) == 1 and {n.id for n in groups[0]} == {0, 1}


def test_maybe_verify_ties_scores_tied_nodes(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    client = _VClient("yes")                                        # "yes" -> 0.75
    st2 = _Host(s, client=client)._maybe_verify_ties(fold(s.read_all()))
    ev = [e for e in s.read_all() if e.type == "verifier_group_scored"]
    assert len(ev) == 1 and [row["score"] for row in ev[0].data["members"]] == [0.75, 0.75]
    assert client.calls == 6                                      # 3 pinned samples per tied node
    # both scored 0.75 -> the verifier component is itself tied -> the id tie-break stands (max -> #1)
    assert st2.best_node_id == 1


def test_maybe_verify_ties_noop_when_off(tmp_path):
    s = _run(tmp_path, select_verifier=False)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, select_verifier=True, client=_VClient())._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_maybe_verify_ties_unknown_recorded_contract_fails_before_paid_calls(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max",
                             "select_verifier": True, "select_verifier_samples": 1,
                             "select_verifier_contract": "selection-criteria:v999"})
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    client = _VClient()
    host = _Host(s, client=client, samples=1)

    # Repeated cadence invocations must remain a true no-op: no client traffic and no rejected audit rows.
    host._maybe_verify_ties(fold(s.read_all()))
    host._maybe_verify_ties(fold(s.read_all()))
    assert client.calls == 0
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_maybe_verify_ties_noop_without_client(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=None)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_maybe_verify_ties_noop_without_a_tie(tmp_path):
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.5)                                                 # no tie
    _Host(s, client=_VClient())._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_atomic_group_carries_provenance(tmp_path):
    # A selection-affecting treatment is one auditable record with per-member sample/evidence provenance.
    s = _run(tmp_path, select_verifier=True, samples=1)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_VClient("yes"), samples=1)._maybe_verify_ties(fold(s.read_all()))
    ev = [e for e in s.read_all() if e.type == "verifier_group_scored"]
    assert len(ev) == 1 and ev[0].data["contract"] == VERIFIER_SELECTION_CONTRACT
    assert all("n_samples" in row and "agreement" in row and "evidence_digest" in row
               for row in ev[0].data["members"])


def test_tie_group_abstains_atomically_on_a_failure(tmp_path):
    # ATOMIC: if ANY member of a tie group fails verification, the WHOLE group is left unscored (its tie
    # falls to the id break) — never half-scored (which would let a neutral 0.5 outrank a verified-low).
    s = _run(tmp_path, select_verifier=True, samples=1)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_FlakyVClient(fail_on=2), samples=1)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())  # neither member committed


def test_failed_attempt_guard_is_scoped_to_the_evidence_revision(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=1)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    host = _Host(s, client=_FlakyVClient(fail_on=1), samples=1)
    host._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())

    for nid in (0, 1):
        s.append("node_confirmed", {"node_id": nid, "generation": 0,
                                    "mean": 0.9, "std": 0.01, "seeds": 3})
    host._client = _VClient("yes")
    host._maybe_verify_ties(fold(s.read_all()))
    assert sum(e.type == "verifier_group_scored" for e in s.read_all()) == 1


def test_low_agreement_verdict_is_abstained(tmp_path):
    # a high-variance verdict (samples disagree -> agreement 0.33 < 0.5) must not decide a tie: abstain,
    # so the whole group (atomic) stays on the id tie-break rather than steering on noise.
    s = _run(tmp_path, select_verifier=True)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_NoisyVClient(), samples=3)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_fewer_than_a_majority_of_requested_samples_abstains(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=3)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_SparseVClient(), samples=1)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_exactly_half_agreement_is_not_a_majority(tmp_path):
    s = _run(tmp_path, select_verifier=True, samples=2)
    _add(s, 0, 0.9)
    _add(s, 1, 0.9)
    _Host(s, client=_NoisyVClient(), samples=2)._maybe_verify_ties(fold(s.read_all()))
    assert not any(e.type == "verifier_group_scored" for e in s.read_all())


def test_settings_default_off():
    assert Settings().select_verifier is False and Settings().select_verifier_samples == 3


def test_settings_caps_verifier_sampling_cost():
    with pytest.raises(ValueError):
        Settings(select_verifier_samples=33)
