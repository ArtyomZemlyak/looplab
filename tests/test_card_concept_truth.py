"""Card concept tags are an exact folded-node projection, never a mixed-evidence union."""
from __future__ import annotations

from itertools import permutations

from looplab.core.models import Card, Event
from looplab.events.replay import _derive_cards, fold
from looplab.serve.public_cards import public_cards


def _events(rows):
    return [Event(seq=index, type=event_type, data=data)
            for index, (event_type, data) in enumerate(rows)]


def _source(card):
    assert card.concept_source is not None
    return card.concept_source.model_dump(mode="json")


def _node(node_id, statement, card_id, *, concepts=None, concept_mode=None, generation=0,
          operator="draft"):
    idea = {"operator": operator, "hypothesis": statement, "card_id": card_id}
    if concepts is not None:
        idea["concepts"] = concepts
    if concept_mode is not None:
        idea["concept_mode"] = concept_mode
    return {
        "node_id": node_id, "generation": generation, "operator": operator, "idea": idea,
    }


def test_classifier_and_operator_replace_authored_card_tags_with_exact_provenance():
    base = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "one direction", "card-one",
            concepts=["authored/tag"], concept_mode="full")),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm",
            "concepts": ["classifier/tag"],
        }),
    ]
    classified = fold(_events(base)).cards["card-one"]
    assert classified.concept_tags == ["classifier/tag"]
    assert classified.provenance_tier == "classifier"
    assert _source(classified) == {
        "kind": "node", "node_id": 1, "node_generation": 0,
        "provenance": "classifier", "membership_present": True,
        "complete": True, "receipt_valid": True, "materialization_receipt": None,
    }

    edited = fold(_events(base + [
        ("concept_tag_edited", {
            "node_id": 1, "node_generation": 0, "concepts": ["operator/tag"],
        }),
        # The classifier cadence yields to the exact operator receipt regardless of later arrival.
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["late/tag"],
        }),
    ])).cards["card-one"]
    assert edited.concept_tags == ["operator/tag"]
    assert edited.provenance_tier == "operator-edited"
    assert _source(edited)["provenance"] == "operator-edited"


def test_delta_materialization_and_explicit_empty_are_not_confused_with_absence():
    state = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("run_concepts", {"concepts": ["base/keep", "base/remove"]}),
        ("node_created", {
            **_node(1, "delta direction", "card-delta", concept_mode="delta"),
            "idea": {
                "operator": "draft", "hypothesis": "delta direction", "card_id": "card-delta",
                "concept_mode": "delta", "concepts_added": ["child/new"],
                "concepts_removed": ["base/remove"],
            },
        }),
        ("node_created", _node(
            2, "known empty", "card-empty", concepts=[], concept_mode="full")),
        ("node_created", _node(3, "legacy absent", "card-absent")),
        ("node_created", _node(
            4, "classifier empty", "card-classifier-empty",
            concepts=["before/classifier"], concept_mode="full")),
        ("node_concepts", {
            "node_id": 4, "generation": 0, "mode": "llm", "concepts": [],
        }),
    ]))

    delta = state.cards["card-delta"]
    assert delta.concept_tags == ["base/keep", "child/new"]
    assert delta.provenance_tier == "researcher-authored"
    assert _source(delta)["complete"] is True

    empty = state.cards["card-empty"]
    assert empty.concept_tags == []
    assert _source(empty)["membership_present"] is True
    assert _source(empty)["complete"] is True

    absent = state.cards["card-absent"]
    assert absent.concept_tags == []
    assert _source(absent)["membership_present"] is False
    assert _source(absent)["complete"] is False

    classifier_empty = state.cards["card-classifier-empty"]
    assert classifier_empty.concept_tags == []
    assert classifier_empty.provenance_tier == "classifier"
    assert _source(classifier_empty)["membership_present"] is True
    assert _source(classifier_empty)["complete"] is True


def test_partial_and_unavailable_materialization_receipts_follow_the_owner():
    partial = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "partially tagged", "card-partial",
            concepts=["authored/tag"], concept_mode="full")),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm",
            "concepts": ["classifier/retained", {"invalid": True}],
        }),
    ])).cards["card-partial"]
    assert partial.concept_tags == ["classifier/retained"]
    assert _source(partial)["complete"] is False
    assert _source(partial)["materialization_receipt"] == {
        "status": "partial", "reasons": ["invalid_concept_id"],
    }

    unavailable = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {
            **_node(2, "missing base", "card-unavailable", concept_mode="delta"),
            "idea": {
                "operator": "draft", "hypothesis": "missing base",
                "card_id": "card-unavailable", "concept_mode": "delta",
                "concepts_added": ["child/new"], "concepts_removed": [],
            },
        }),
    ])).cards["card-unavailable"]
    assert unavailable.concept_tags == []
    assert _source(unavailable)["membership_present"] is True
    assert _source(unavailable)["complete"] is False
    assert _source(unavailable)["materialization_receipt"] == {
        "status": "unavailable", "reasons": ["delta_dependency_missing_run_base"],
    }


def test_corrupt_folded_receipt_and_future_provenance_fail_closed_without_losing_tags():
    state = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "corrupt sidecar", "card-corrupt",
            concepts=["retained/tag"], concept_mode="full")),
    ]))
    state.node_concept_materialization_receipts[1] = {  # type: ignore[assignment]
        "status": "partial", "reasons": ["future_unknown_reason"],
    }
    state.node_concept_provenance[1] = "future-self-certified-producer"
    _derive_cards(state)

    card = state.cards["card-corrupt"]
    assert card.concept_tags == ["retained/tag"]
    assert card.provenance_tier == "untrusted-source"
    assert _source(card)["complete"] is False
    assert _source(card)["receipt_valid"] is False
    assert _source(card)["materialization_receipt"] is None


def test_propose_reset_rebinds_generation_and_rejects_stale_concept_results():
    state = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "same card", "card-reset", concepts=["old/authored"],
            concept_mode="full", generation=0)),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["old/classified"],
        }),
        ("node_reset", {"node_id": 1, "generation": 0, "from_stage": "propose"}),
        ("node_created", _node(
            1, "same card", "card-reset", concepts=["new/authored"],
            concept_mode="full", generation=1)),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["stale/result"],
        }),
        ("node_concepts", {
            "node_id": 1, "generation": 1, "mode": "llm", "concepts": ["current/result"],
        }),
    ]))
    card = state.cards["card-reset"]
    assert card.evidence == [1] and card.concept_tags == ["current/result"]
    assert _source(card)["node_generation"] == 1
    assert _source(card)["provenance"] == "classifier"


def test_merged_mixed_provenance_keeps_canonical_owner_instead_of_union():
    def project(card_order):
        rows = [
            ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
            *(('card_added', {"id": cid, "statement": statement})
              for cid, statement in card_order),
            ("card_merged", {"canonical": "card-b", "aliases": ["card-a"]}),
            ("node_created", _node(
                1, "direction A", "card-a", concepts=["a/authored"], concept_mode="full")),
            ("node_concepts", {
                "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["a/classifier"],
            }),
            ("node_created", _node(
                2, "direction B", "card-b", concepts=["b/authored"], concept_mode="full")),
            ("concept_tag_edited", {
                "node_id": 2, "node_generation": 0, "concepts": ["b/operator"],
            }),
        ]
        return fold(_events(rows)).cards["card-b"]

    forward = project([("card-a", "direction A"), ("card-b", "direction B")])
    reverse = project([("card-b", "direction B"), ("card-a", "direction A")])
    for card in (forward, reverse):
        assert card.evidence == [1, 2]
        assert card.concept_tags == ["b/operator"]
        assert card.provenance_tier == "operator-edited"
        assert _source(card)["node_id"] == 2


def test_mixed_provenance_merge_is_stable_across_independent_event_block_permutations():
    a = [
        ("node_created", _node(
            1, "direction A", "card-a", concepts=["a/authored"], concept_mode="full")),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["a/classifier"],
        }),
    ]
    b = [
        ("node_created", _node(
            2, "direction B", "card-b", concepts=["b/authored"], concept_mode="full")),
        ("concept_tag_edited", {
            "node_id": 2, "node_generation": 0, "concepts": ["b/operator"],
        }),
    ]
    merge = [("card_merged", {"canonical": "card-b", "aliases": ["card-a"]})]
    snapshots = []
    for blocks in permutations((a, b, merge)):
        rows = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
        rows.extend(row for block in blocks for row in block)
        snapshots.append(fold(_events(rows)).cards["card-b"].model_dump(mode="json"))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert snapshots[0]["concept_tags"] == ["b/operator"]
    assert snapshots[0]["concept_source"]["node_id"] == 2


def test_merge_without_materialized_canonical_selects_one_action_owner_deterministically():
    def project(node_rows):
        return fold(_events([
            ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
            ("card_merged", {"canonical": "card-z", "aliases": ["card-b", "card-a"]}),
            *node_rows,
        ])).cards["card-z"]

    a = ("node_created", _node(
        2, "direction A", "card-a", concepts=["a/tag"], concept_mode="full", operator="draft"))
    b = ("node_created", _node(
        1, "direction B", "card-b", concepts=["b/tag"], concept_mode="full", operator="improve"))
    forward, reverse = project([a, b]), project([b, a])
    for card in (forward, reverse):
        assert card.evidence == [1, 2]
        assert card.operator == "draft" and card.concept_tags == ["a/tag"]
        assert _source(card)["node_id"] == 2
    assert forward.model_dump(mode="json") == reverse.model_dump(mode="json")


def test_thin_canonical_merge_adopts_one_action_while_explicit_proposal_keeps_its_own():
    common = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "alias action", "card-alias", concepts=["alias/tag"], concept_mode="full")),
        ("card_merged", {"canonical": "card-canonical", "aliases": ["card-alias"]}),
    ]
    thin = fold(_events([
        common[0],
        ("card_added", {"id": "card-canonical", "statement": "canonical"}),
        *common[1:],
    ])).cards["card-canonical"]
    assert thin.operator == "draft" and thin.concept_tags == ["alias/tag"]
    assert _source(thin)["kind"] == "node" and _source(thin)["node_id"] == 1

    proposed = fold(_events([
        common[0],
        ("card_added", {
            "id": "card-canonical", "statement": "canonical",
            "idea": {"operator": "improve", "concept_tags": ["proposal/tag"]},
        }),
        *common[1:],
    ])).cards["card-canonical"]
    assert proposed.operator == "improve" and proposed.concept_tags == ["proposal/tag"]
    assert _source(proposed)["kind"] == "card_added"
    assert proposed.provenance_tier is None


def test_enrichment_cannot_forge_node_provenance_but_remains_useful_before_linking():
    linked = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "linked", "card-linked", concepts=["authored/tag"], concept_mode="full")),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["trusted/tag"],
        }),
        ("card_enriched", {
            "id": "card-linked", "concept_tags": ["forged/tag"],
            "provenance_tier": "operator-edited",
        }),
    ])).cards["card-linked"]
    assert linked.concept_tags == ["trusted/tag"]
    assert linked.provenance_tier == "classifier"
    assert _source(linked)["kind"] == "node"

    proposed_state = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", {"id": "card-proposed", "statement": "proposed"}),
        ("card_enriched", {
            "id": "card-proposed", "concept_tags": ["proposal/tag", {"bad": True}],
            "provenance_tier": "classifier",
        }),
    ]))
    proposed = proposed_state.cards["card-proposed"]
    assert proposed.concept_tags == ["proposal/tag"]
    assert proposed.provenance_tier is None
    assert _source(proposed)["kind"] == "card_enriched"
    assert _source(proposed)["complete"] is False
    assert "provenance_tier" not in proposed_state.cards_enriched[0]


def test_public_projection_has_a_separate_strict_card_concept_receipt_and_schema():
    state = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", _node(
            1, "public", "card-public", concepts=["public/tag"], concept_mode="full")),
        ("node_concepts", {
            "node_id": 1, "generation": 0, "mode": "llm", "concepts": ["public/classified"],
        }),
    ]))
    dto = public_cards(state.cards)["card-public"]
    assert dto["concept_source"] == {
        "kind": "node", "node_id": 1, "node_generation": 0,
        "provenance": "classifier", "membership_present": True,
        "complete": True, "receipt_valid": True,
    }
    assert dto["provenance_tier"] == dto["concept_source"]["provenance"]

    hostile = public_cards({
        "card-hostile": {
            "status": "proposed", "actionable": True, "statement": "hostile",
            "concept_source": {
                "kind": "node", "node_id": 4, "node_generation": 2,
                "provenance": "classifier", "membership_present": True, "complete": False,
                "receipt_valid": True,
                "materialization_receipt": {
                    "status": "partial", "reasons": ["invalid_concept_id"],
                    "private": "must-not-ship",
                },
                "private": "must-not-ship",
            },
        },
    })["card-hostile"]
    # Non-canonical receipts fail closed; neither nested private field is copied to the DTO.
    assert hostile["concept_source"] == {
        "kind": "node", "node_id": 4, "node_generation": 2,
        "provenance": "classifier", "membership_present": True,
        "complete": False, "receipt_valid": False,
    }

    truncated = public_cards({
        "card-truncated": {
            "status": "proposed", "actionable": True, "statement": "many tags",
            "concept_tags": [f"axis/tag-{index}" for index in range(40)],
            "concept_source": {
                "kind": "node", "node_id": 7, "node_generation": 0,
                "provenance": "classifier", "membership_present": True,
                "complete": True, "receipt_valid": True,
            },
            "provenance_tier": "operator-edited",
        },
    })["card-truncated"]
    assert len(truncated["concept_tags"]) == 32
    assert truncated["concept_source"]["complete"] is False
    assert truncated["provenance_tier"] == "classifier"

    schema = Card.model_json_schema()
    assert schema["properties"]["concept_source"]["anyOf"][0]["$ref"] == "#/$defs/CardConceptSource"
    receipt_schema = schema["$defs"]["CardConceptSource"]
    assert receipt_schema["additionalProperties"] is False
    assert set(receipt_schema["properties"]) == {
        "kind", "node_id", "node_generation", "provenance", "membership_present",
        "complete", "receipt_valid", "materialization_receipt",
    }
