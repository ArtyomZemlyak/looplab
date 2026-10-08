"""Concept contribution must not confuse a high-scoring recipe with an ablation."""
import json

import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.search.concept_effects import concept_effects, valid_effect


def run(rows, direction="max"):
    state = RunState(run_id="effects", task_id="toy", goal="contrast", direction=direction)
    for i, (tags, metric, parents) in enumerate(rows):
        state.nodes[i] = Node(id=i, operator="draft", idea=Idea(operator="draft", params={}, rationale="r"),
                              parent_ids=parents, status=NodeStatus.evaluated, metric=metric,
                              metric_provenance={"comparability": {
                                  "authority": "declared", "keys": {"declared": "same-ruler"}}})
        if tags is not None:
            state.node_concepts[i] = tags
            state.node_concept_provenance[i] = "classifier"
    return state


def effect(state, cid="c", **kwargs):
    return concept_effects(state, [cid], **kwargs)[cid]


def test_confounded_winner_gets_no_concept_credit():
    st = run([(["base"], 0.1, []), (["base", "c", "other-change"], 0.9, [0])])
    e = effect(st)
    assert e["estimate"] is None and e["reason"] == "no_matching_context_or_phase"


@pytest.mark.parametrize("direction,with_metric,expected", [("max", 0.8, 0.3), ("min", 0.2, 0.3)])
def test_added_and_removed_concept_have_same_oriented_effect(direction, with_metric, expected):
    st = run([(["base", "c"], with_metric, []), (["base"], 0.5, [0])], direction)
    e = effect(st)
    assert e["estimate"] == pytest.approx(expected)
    assert e["pairs"][0]["kind"] == "parent"
    assert e["pairs"][0]["with_node"] == 0 and e["pairs"][0]["without_node"] == 1
    assert valid_effect(e)


def test_missing_tags_are_unknown_but_explicit_empty_is_control():
    st = run([(None, 0.1, []), (["c"], 0.8, [0])])
    assert effect(st)["reason"] == "no_without_concept"
    st.node_concepts[0] = []
    st.node_concept_provenance[0] = "classifier"
    assert effect(st)["estimate"] == pytest.approx(0.7)


@pytest.mark.parametrize("record", [None, {"authority": "inferred", "keys": {"inferred": "same"}},
                                    {"authority": "declared", "keys": {"declared": "different"}}])
def test_unknown_and_different_evaluation_never_produce_delta(record):
    st = run([([], 0.1, []), (["c"], 0.8, [0])])
    st.nodes[0].metric_provenance = {"comparability": record}
    assert effect(st)["estimate"] is None


def test_one_control_is_not_reused_for_every_good_candidate():
    st = run([([], 0.1, []), (["c"], 0.3, [0]), (["c"], 0.8, [0]), (["c"], 0.9, [0])])
    e = effect(st)
    assert e["n_pairs"] == 1 and e["estimate"] == pytest.approx(0.2)
    assert e["pairs"][0]["with_node"] == 1  # deterministic, never pick the winner by its score


def test_single_parent_contrast_preferred_over_distant_control():
    st = run([([], 0.1, []), ([], 0.5, []), (["c"], 0.7, [1])])
    assert effect(st)["pairs"][0]["without_node"] == 1


def test_contexts_have_equal_weight_not_experiment_count():
    rows = [(["a"], 0.0, []), (["a", "c"], 1.0, [0]),
            (["a"], 0.0, []), (["a", "c"], 1.0, [2]),
            (["a"], 0.0, []), (["a", "c"], 1.0, [4]),
            (["b"], 0.0, []), (["b", "c"], -1.0, [6])]
    e = effect(run(rows))
    assert e["estimate"] == 0 and e["mean"] == 0 and e["n_contexts"] == 2
    assert e["n_pairs"] == 4 and e["positive"] == 3 and e["negative"] == 1


def test_subtree_union_removes_all_descendants_from_matching_context():
    st = run([(["base"], 0.1, []), (["base", "loss/a", "loss/b"], 0.5, [0])])
    assert effect(st, "loss", subtree=True)["estimate"] == pytest.approx(0.4)
    assert effect(st, "loss/a")["estimate"] is None  # loss/b is an additional change


def test_partial_membership_cannot_be_negative_control():
    st = run([([], 0.1, []), (["c"], 0.5, [0])])
    st.node_concept_materialization_receipts[0] = {"status": "partial", "reasons": ["concept_list_truncated"]}
    assert effect(st)["estimate"] is None


@pytest.mark.parametrize("change", ["failed", "deleted", "aborted", "infeasible", "nan", "pending"])
def test_ineligible_controls_cannot_support_contribution(change):
    st = run([([], 0.1, []), (["c"], 0.5, [0])])
    n = st.nodes[0]
    if change == "failed": n.status = NodeStatus.failed
    if change == "pending": n.status = NodeStatus.pending
    if change == "deleted": n.tombstoned = True
    if change == "aborted": st.aborted_nodes = [0]
    if change == "infeasible": n.feasible = False
    if change == "nan": n.metric = float("nan")
    assert effect(st)["estimate"] is None


def test_search_and_confirmation_are_not_mixed():
    st = run([([], 0.1, []), (["c"], 0.5, [0])])
    st.nodes[1].confirmed_mean = 0.6
    st.nodes[1].confirmed_ruler = "full"
    assert effect(st)["estimate"] is None
    st.nodes[0].confirmed_mean = 0.2
    assert effect(st)["estimate"] is None
    st.nodes[0].confirmed_ruler = "full"
    assert effect(st)["estimate"] == pytest.approx(0.4)


def test_advisory_warnings_are_disclosed_and_pending_classifier_is_not_memory_evidence():
    st = run([([], 0.1, []), (["c"], 0.5, [0])])
    st.reward_hacks = [{"node_id": 1, "signals": [{"signal": "perfect_metric"}]}]
    assert effect(st)["has_advisory_warnings"] is True
    st.node_concepts_at_pending[1] = 1
    assert effect(st, classifier_only=True)["estimate"] is None


def test_different_evaluation_cohorts_are_not_averaged():
    st = run([([], 0.1, []), (["c"], 0.5, [0]), (["a"], 10, []), (["a", "c"], 20, [2])])
    for nid in (2, 3):
        st.nodes[nid].metric_provenance["comparability"]["keys"]["declared"] = "different"
    assert effect(st)["reason"] == "mixed_evaluation_conditions"


def test_replay_order_and_receipt_preview_are_bounded():
    rows = []
    for i in range(12):
        rows.extend([([], 0.1, []), (["c"], 0.2, [2 * i])])
    st = run(rows)
    first = effect(st)
    st.nodes = dict(reversed(list(st.nodes.items())))
    assert json.dumps(first, sort_keys=True) == json.dumps(effect(st), sort_keys=True)
    assert first["n_pairs"] == 12 and len(first["pairs"]) == 8 and first["pairs_omitted"] == 4
    assert valid_effect(first)


def test_invalid_durable_receipt_is_refused():
    e = effect(run([([], 0.1, []), (["c"], 0.5, [0])]))
    assert not valid_effect({**e, "estimate": float("nan")})
    assert not valid_effect({**e, "n_pairs": True})
    assert not valid_effect({**e, "pairs": [{**e["pairs"][0], "without_node": 1}]})


def test_portfolio_counts_matched_effects_not_rank_and_refuses_alias_pooling():
    from looplab.engine.concept_capsules import build_concept_capsule, portfolio_concept_overview
    st = run([([], 0.1, []), (["c"], 0.5, [0])])
    cap = build_concept_capsule(run_id="r", fingerprint=["toy"], direction="max", concepts=["c"],
                                concept_outcomes={"c": 0.5}, concept_effects={"c": effect(st)})
    row = portfolio_concept_overview([cap])["concepts"][0]
    assert row["n_helped"] == 1 and row["runs"][0]["effect"]["n_pairs"] == 1
    cap["concept_effects"]["c"]["n_pairs"] = True
    assert portfolio_concept_overview([cap])["n_runs"] == 0


def test_budget_abstains_instead_of_selecting_a_score_biased_prefix(monkeypatch):
    import looplab.search.concept_effects as module
    monkeypatch.setattr(module, "MAX_EFFECT_WORK", 1)
    e = effect(run([([], 0.1, []), (["c"], 0.5, [0])]))
    assert e["status"] == "unavailable" and e["reason"] == "analysis_limit"


# Seven equal deltas whose float mean rounds one ulp PAST them (review 2026-10-07): the mean of
# seven -0.21987892246138063 is -0.21987892246138066, so an unclamped estimate sat outside
# [low, high], `valid_effect` refused it, and the WHOLE cross-run capsule carrying it was dropped.
_ULP_DELTA = -0.21987892246138063


def _seven_equal_pairs():
    rows = []
    for i in range(7):
        rows.extend([([], 0.0, []), (["c"], _ULP_DELTA, [2 * i])])
    return run(rows)


def test_a_mean_of_equal_deltas_is_clamped_into_its_pair_range():
    import math
    assert math.fsum(d / 7 for d in [_ULP_DELTA] * 7) < _ULP_DELTA   # the defect is real
    e = effect(_seven_equal_pairs())
    assert e["status"] == "matched" and e["n_pairs"] == 7 and e["n_contexts"] == 1
    assert e["low"] == e["high"] == _ULP_DELTA
    assert e["estimate"] == _ULP_DELTA and e["mean"] == _ULP_DELTA
    assert valid_effect(e)


def test_reader_tolerates_an_old_row_one_ulp_outside_but_stays_strict():
    e = effect(_seven_equal_pairs())
    old_row = {**e, "estimate": -0.21987892246138066, "mean": -0.21987892246138066}
    assert valid_effect(old_row)                                 # rounding noise is the same number
    assert not valid_effect({**e, "estimate": _ULP_DELTA - 1e-9})   # a different number is not
    assert not valid_effect({**e, "estimate": _ULP_DELTA + 1e-9})
    assert not valid_effect({**e, "low": 0.0, "high": -1.0, "estimate": -0.5})   # inverted range
    zero = {**e, "low": 0.0, "high": 0.0, "estimate": 0.0, "mean": 0.0}
    assert valid_effect(zero) and not valid_effect({**zero, "estimate": 5e-324})


def test_capsule_store_keeps_a_capsule_whose_old_effect_is_one_ulp_outside(tmp_path):
    from looplab.engine.concept_capsules import ConceptCapsuleStore, build_concept_capsule
    e = effect(_seven_equal_pairs())
    old_row = {**e, "estimate": -0.21987892246138066}
    cap = build_concept_capsule(run_id="r", fingerprint=["toy"], direction="max", concepts=["c"],
                                concept_outcomes={"c": 0.5}, concept_effects={"c": old_row})
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    assert store.add(cap) is True
    assert [c["run_id"] for c in store.all()] == ["r"]


def test_one_aborted_tagged_node_does_not_blank_every_effect_in_the_concept_frame():
    """`build_core` hands the metric columns a lifecycle-filtered copy; the effect estimator must
    read the complete fold, or the dropped node's membership poisons the projection run-wide."""
    from looplab.search.concept_lens import default_lenses
    from looplab.serve import concept_frame
    rows = [(["base"], 0.1, []), (["base", "loss/a"], 0.5, [0]), (["base"], 0.2, []),
            (["base", "loss/b"], 0.6, [2]), (["base", "x"], 0.3, [])]
    effects = {}
    for aborted in ([], [4]):
        st = run(rows)
        st.aborted_nodes = aborted
        core = concept_frame.build_core(
            st, run_id="r", lens_pack=default_lenses(), generation="g", requested_seq=None,
            captured_seq=1, max_seq=1, source_divergence=None)
        effects[bool(aborted)] = core["metrics"]["rollup"]["loss/a"]["effect"]
    assert effects[True]["status"] == "matched", effects[True]
    assert effects[True]["estimate"] == effects[False]["estimate"]
