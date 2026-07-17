"""PART V Phase 2b: EV_CONCEPT_TAG_EDITED — an operator re-tags ONE node's concepts.

The operator edit is authoritative for the run's read models (node_concepts) and OPERATOR-provenance
stamped, so the classifier re-tag cadence (`node_concepts` events) yields to it REGARDLESS of arrival
order (invariant 5). It is NOT independent classifier evidence. A node reset clears the override.
"""
from __future__ import annotations

from looplab.core.models import (NODE_CONCEPT_PROVENANCE_CLASSIFIER, NODE_CONCEPT_PROVENANCE_OPERATOR,
                                  classifier_verified_node_concepts)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _base(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "concepts": ["loss/a"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.9})
    return s


def test_operator_edit_sets_concepts_and_provenance(tmp_path):
    s = _base(tmp_path)
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["loss/contrastive", "arch/moe"]})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/contrastive", "arch/moe"]
    assert st.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OPERATOR


def test_operator_edit_is_not_clobbered_by_classifier_either_order(tmp_path):
    # Invariant 5: {classifier, operator} must fold to the OPERATOR's tags regardless of order.
    forward = _base(tmp_path / "f")
    forward.append("node_concepts", {"node_id": 0, "concepts": ["classifier/tag"], "mode": "llm"})
    forward.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/tag"]})
    reverse = _base(tmp_path / "r")
    reverse.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/tag"]})
    reverse.append("node_concepts", {"node_id": 0, "concepts": ["classifier/tag"], "mode": "llm"})
    a, b = fold(forward.read_all()), fold(reverse.read_all())
    assert a.node_concepts[0] == b.node_concepts[0] == ["operator/tag"]        # operator wins both ways
    assert a.node_concept_provenance[0] == b.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OPERATOR


def test_last_operator_edit_wins(tmp_path):
    s = _base(tmp_path)
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["first"]})
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["second"]})
    assert fold(s.read_all()).node_concepts[0] == ["second"]


def test_operator_edit_is_not_classifier_evidence(tmp_path):
    # Human curation must NOT silently become independent cross-run/novelty evidence.
    s = _base(tmp_path)
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["loss/contrastive"]})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/contrastive"]                          # visible in read models
    assert classifier_verified_node_concepts(st, 0) == []                       # but NOT evidence


def test_stale_generation_edit_is_dropped(tmp_path):
    s = _base(tmp_path)
    # node 0 is at attempt 0 and carries its AUTHORED idea.concepts (['loss/a']). An edit claiming
    # generation 5 is stale -> dropped, so the node keeps its authored tags (not the operator's).
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["x"], "node_generation": 5})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/a"]                                    # unchanged
    assert st.node_concept_provenance.get(0) != NODE_CONCEPT_PROVENANCE_OPERATOR
    # a matching generation (0) applies
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["y"], "node_generation": 0})
    assert fold(s.read_all()).node_concepts[0] == ["y"]


def test_reset_clears_operator_override_so_classifier_can_retag(tmp_path):
    s = _base(tmp_path)
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/tag"]})
    s.append("node_reset", {"node_id": 0, "from_stage": "propose"})            # bumps attempt, clears tags
    st = fold(s.read_all())
    assert st.node_concept_provenance.get(0) != NODE_CONCEPT_PROVENANCE_OPERATOR   # override cleared
    # the classifier can now tag the fresh node again (generation must match the new attempt)
    s.append("node_concepts", {"node_id": 0, "concepts": ["fresh"], "mode": "llm",
                               "generation": st.nodes[0].attempt})
    st2 = fold(s.read_all())
    assert st2.node_concepts[0] == ["fresh"]
    assert st2.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_CLASSIFIER


def test_operator_edit_survives_implement_reset_and_node_created_reemit(tmp_path):
    # REVIEW (HIGH): an implement/eval reset keeps the SAME idea and does NOT clear tags; the engine then
    # re-emits node_created for that idea. The operator override must SURVIVE that re-emission (it describes
    # the unchanged idea) — it must not be downgraded to researcher-authored. The re-emit guard in
    # _on_node_created protects OPERATOR provenance, not just CLASSIFIER.
    s = _base(tmp_path)
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/tag"]})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})           # same idea, tags NOT cleared
    st = fold(s.read_all())
    assert st.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OPERATOR     # survived the reset
    # the engine re-emits node_created for the SAME idea at the bumped generation
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "concepts": ["loss/a"]}})
    st2 = fold(s.read_all())
    assert st2.node_concepts[0] == ["operator/tag"]                             # NOT clobbered to ['loss/a']
    assert st2.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OPERATOR   # NOT downgraded to authored


def test_unknown_node_and_malformed_concepts_are_safe(tmp_path):
    s = _base(tmp_path)
    s.append("concept_tag_edited", {"node_id": 99, "concepts": ["x"]})          # unknown node -> no-op
    s.append("concept_tag_edited", {"node_id": 0, "concepts": "not-a-list"})    # malformed -> empty
    st = fold(s.read_all())
    assert 99 not in st.node_concepts
    assert st.node_concepts[0] == [] and st.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OPERATOR
