"""The narrative uses completion evidence rather than an unfenced first/best delta."""
import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.serve.report import _report_context, _SYSTEM


def pair(direction="max"):
    state = RunState(run_id="r", task_id="t", direction=direction)
    for nid, score in enumerate((0.5, 0.7)):
        state.nodes[nid] = Node(id=nid, operator="draft", idea=Idea(operator="draft"),
                               status=NodeStatus.evaluated, metric=score,
                               parent_ids=[0] if nid else [],
                               parent_generations={"0": 0} if nid else {},
                               metric_provenance={"comparability": {
                                   "version": 1, "authority": "declared",
                                   "keys": {"declared": "a" * 16}}})
    return state


@pytest.mark.parametrize("direction,outcome,gain", [("max", "better", "+0.2"), ("min", "worse", "-0.2")])
def test_report_uses_primary_scores_with_direction_and_separate_confirmation(direction, outcome, gain):
    state = pair(direction)
    state.nodes[1].confirmed_mean = 0.1
    state.nodes[1].confirmed_seeds = 3
    text = _report_context(state)
    assert f"primary scores 0.5 → 0.7, direction-normalized gain {gain}, {outcome}" in text
    assert "Improvement: baseline" not in text
    assert "No eligible same-ruler" not in text
    assert "observational, not isolated ablations" in text


@pytest.mark.parametrize("reason", ["unknown", "different", "parent_unavailable", "retargeted", "ineligible", "base_unknown"])
def test_report_abstains_for_unfenced_or_ineligible_evidence(reason):
    state = pair()
    child = state.nodes[1]
    if reason == "unknown":
        child.metric_provenance = {}
    elif reason == "different":
        child.metric_provenance["comparability"]["keys"]["declared"] = "b" * 16
    elif reason == "parent_unavailable":
        state.nodes[0].attempt = 1
    elif reason == "retargeted":
        state.objective_key = "other"
    elif reason == "ineligible":
        child.metric_provenance["salvaged"] = True
    elif reason == "base_unknown":
        state.upstream_enabled = True
    text = _report_context(state)
    assert f"'{reason}': 1" in text
    assert "No eligible same-ruler primary-score comparison establishes improvement." in text
    assert "direction-normalized gain" not in text
    assert "Improvement: baseline" not in text


def test_report_keeps_hypothesis_search_out_of_measured_knowledge():
    state = pair()
    state.research = [{"summary": "Try a larger model"}]
    text = _report_context(state)
    assert "Latest hypothesis-search memo (untested proposals, not measured learnings): Try a larger model" in text
    assert "deep-research conclusion" not in text
    assert "never in learnings or what_worked" in _SYSTEM
    assert "Failed evaluations show execution problems" in _SYSTEM


def test_report_concept_contrasts_reuse_with_without_estimator_and_abstain_for_confounds():
    state = pair()
    state.node_concepts = {0: ["base"], 1: ["base", "c"]}
    state.node_concept_provenance = {0: "classifier", 1: "classifier"}
    text = _report_context(state)
    assert "Concept c: status=matched" in text
    assert "gain=0.2, pairs=1, contexts=1" in text
    assert "observational with/without evidence, not causal ablations" in text
    state.node_concepts[1].append("confound")
    text = _report_context(state)
    assert "Concept c: status=insufficient" in text
    assert "no_matching_context_or_phase" in text
