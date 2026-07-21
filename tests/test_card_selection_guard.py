"""Fail-closed architecture contract for future Card-driven selection."""
from __future__ import annotations

import pytest

from looplab.core.models import Card, Event, card_ownership_receipt, hypothesis_id
from looplab.events.replay import fold
from looplab.serve.public_cards import public_cards


def _events(rows):
    return [Event(seq=index, type=kind, data=data) for index, (kind, data) in enumerate(rows)]


def _baseline():
    return [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {
            "node_id": 1,
            "operator": "draft",
            "idea": {"operator": "draft", "hypothesis": "baseline direction"},
        }),
        ("node_evaluated", {"node_id": 1, "metric": 0.5}),
    ]


def _native_card_added(card_id="opaque-work-item", statement="try a bounded improvement"):
    idea = {"operator": "improve", "params": {"lr": 0.2}}
    action = {
        "operator": "improve",
        "params": {"lr": 0.2},
        "space": None,
        "eval_profile": None,
        "parent_id": 1,
        "parent_ids": [1],
        "scored_against": 1,
        "footprint": None,
    }
    receipt = card_ownership_receipt(card_id, statement, action)
    assert receipt is not None
    return ("card_added", {
        "id": card_id,
        "statement": statement,
        "source": "engine",
        "idea": idea,
        "parent_id": 1,
        "parent_ids": [1],
        "scored_against": 1,
        "ownership_receipt": receipt,
    })


def test_one_receipt_bound_fresh_work_item_is_selection_ready_independent_of_id_shape():
    # A native id may happen to look exactly like a legacy statement hash. Receipt ownership, not
    # spelling, is the discriminator.
    card_id = hypothesis_id("some unrelated direction")
    state = fold(_events([*_baseline(), _native_card_added(card_id)]))

    card = state.cards[card_id]
    assert card.identity.kind == "native"
    assert card.identity.source == "card_added_receipt"
    assert card.identity.durable is True and card.identity.receipt_valid is True
    assert card.selection_provenance.model_dump(mode="json") == {
        "action_source": "card_added",
        "action_owner_count": 1,
        "action_complete": True,
        "freshness": "current",
        "owner_state": "none",
    }
    assert card.selection_blockers == [] and card.selection_ready is True
    assert Card.model_validate(card.model_dump(mode="json")).selection_ready is True


def test_receipt_sanitizer_preserves_concept_metadata_but_rejects_lossy_future_actions():
    with_concepts = _native_card_added("concept-card")[1]
    with_concepts["idea"]["concept_tags"] = ["optimizer", "schedule"]
    future_action = _native_card_added("future-card")[1]
    future_action["idea"]["future_execution_mode"] = {"unsafe": "new semantics"}
    extended_receipt = _native_card_added("extended-receipt")[1]
    extended_receipt["ownership_receipt"]["future_proof"] = "must-not-be-ignored"

    state = fold(_events([
        *_baseline(),
        ("card_added", with_concepts),
        ("card_added", future_action),
        ("card_added", extended_receipt),
    ]))

    # Concept membership is metadata under a separate completeness contract, not executable meaning.
    assert state.cards["concept-card"].selection_ready is True
    assert state.cards["concept-card"].concept_tags == ["optimizer", "schedule"]

    # CODEX AGENT: bounded replay records only that an unknown action member existed. It must neither
    # retain attacker-sized content nor silently validate a digest after discarding future semantics.
    future = state.cards["future-card"]
    assert future.identity.kind == "native"
    assert future.selection_provenance.action_complete is False
    assert "action_receipt_incomplete" in future.selection_blockers
    assert future.selection_ready is False

    # An extended proof is not v1. Dropping its extra member while retaining the rest would forge an
    # exact receipt, so the whole ownership claim is rejected.
    extended = state.cards["extended-receipt"]
    assert extended.identity.kind != "native"
    assert extended.selection_ready is False


def test_current_unbound_card_added_and_proposed_without_action_fail_closed():
    ready_shape = _native_card_added("unbound-card")[1]
    ready_shape.pop("ownership_receipt")
    state = fold(_events([
        *_baseline(),
        ("card_added", ready_shape),
        ("card_added", {
            "id": "direction-only", "statement": "research direction without a work item",
        }),
    ]))

    unbound = state.cards["unbound-card"]
    assert unbound.actionable is True  # compatibility display flag, not executability proof
    assert unbound.identity.model_dump(mode="json") == {
        "kind": "synthesized_shadow",
        "source": "card_added_unbound",
        "durable": False,
        "receipt_valid": False,
        "action_digest": None,
    }
    assert "identity_not_native" in unbound.selection_blockers
    assert unbound.selection_ready is False

    direction = state.cards["direction-only"]
    assert direction.status == "proposed" and direction.actionable is True
    assert "action_owner_missing" in direction.selection_blockers
    assert direction.selection_ready is False


def test_legacy_hash_and_node_only_card_id_are_never_native_identity():
    state = fold(_events([
        *_baseline(),
        ("node_created", {
            "node_id": 2,
            "operator": "draft",
            "idea": {"operator": "draft", "hypothesis": "legacy hash shadow"},
        }),
        ("node_created", {
            "node_id": 3,
            "operator": "draft",
            "idea": {
                "operator": "draft", "hypothesis": "unregistered stable-looking id",
                "card_id": "card-123",
            },
        }),
    ]))

    legacy = state.cards[hypothesis_id("legacy hash shadow")]
    synthesized = state.cards["card-123"]
    assert (legacy.identity.kind, legacy.identity.source) == ("legacy_hash", "node_statement_hash")
    assert (synthesized.identity.kind, synthesized.identity.source) == (
        "synthesized_shadow", "node_card_id")
    assert legacy.selection_ready is synthesized.selection_ready is False
    assert "identity_not_native" in legacy.selection_blockers
    assert "identity_not_native" in synthesized.selection_blockers


@pytest.mark.parametrize(
    ("terminal_event", "expected_status", "work_blocker"),
    [
        (None, "running", "work_in_flight"),
        (("node_evaluated", {"node_id": 2, "metric": 0.6}), "evaluated", "work_terminal"),
        (("node_failed", {"node_id": 2, "reason": "superseded", "eval_seconds": 0}),
         "evaluated", "work_terminal"),
    ],
)
def test_linked_running_evaluated_and_superseded_work_is_not_selection_ready(
    terminal_event, expected_status, work_blocker,
):
    card_id = "native-lifecycle-card"
    statement = "one immutable proposal"
    rows = [
        *_baseline(),
        _native_card_added(card_id, statement),
        ("node_created", {
            "node_id": 2,
            "operator": "improve",
            "parent_ids": [1],
            "idea": {
                "operator": "improve", "params": {"lr": 0.2},
                "hypothesis": statement, "card_id": card_id,
            },
        }),
    ]
    if terminal_event is not None:
        rows.append(terminal_event)
    card = fold(_events(rows)).cards[card_id]

    assert card.identity.kind == "native"
    assert card.status == expected_status
    assert work_blocker in card.selection_blockers
    assert card.selection_ready is False
    # Evaluated/superseded work may remain actionable for board compatibility, which is exactly why
    # callers must never substitute this legacy flag for selection_ready.
    if expected_status == "evaluated":
        assert card.actionable is True


def test_stale_and_merged_native_work_items_fail_closed():
    stale = _native_card_added("stale-card")[1]
    stale["scored_against"] = 0
    stale["ownership_receipt"] = card_ownership_receipt(
        stale["id"], stale["statement"], {
            "operator": "improve", "params": {"lr": 0.2}, "space": None,
            "eval_profile": None, "parent_id": 1, "parent_ids": [1],
            "scored_against": 0, "footprint": None,
        })
    state = fold(_events([
        *_baseline(),
        ("card_added", stale),
        _native_card_added("merge-a", "proposal A"),
        _native_card_added("merge-b", "proposal B"),
        ("card_merged", {"canonical": "merge-a", "aliases": ["merge-b"]}),
    ]))

    assert state.cards["stale-card"].selection_ready is False
    assert "freshness_stale" in state.cards["stale-card"].selection_blockers
    merged = state.cards["merge-a"]
    assert merged.selection_ready is False
    assert "action_owner_ambiguous" in merged.selection_blockers
    assert "merged_work_items" in merged.selection_blockers


def test_public_dto_allowlists_selection_proof_and_downgrades_incomplete_claims():
    card = fold(_events([*_baseline(), _native_card_added()])).cards["opaque-work-item"]
    dto = public_cards({card.id: card})[card.id]
    assert dto["selection_ready"] is True
    assert dto["selection_blockers"] == []
    assert dto["identity"]["kind"] == "native"
    assert dto["selection_provenance"]["owner_state"] == "none"

    hostile = public_cards({
        "forged": {
            "status": "proposed", "verdict": "open", "actionable": True,
            "selection_ready": True, "selection_blockers": [],
            "statement": "forged without proof",
        },
    })["forged"]
    assert hostile["selection_ready"] is False
    assert hostile["selection_blockers"]


def test_card_schema_exposes_identity_readiness_and_stable_blockers():
    schema = Card.model_json_schema()
    assert schema["properties"]["identity"]["$ref"] == "#/$defs/CardIdentityProvenance"
    assert schema["properties"]["selection_provenance"]["$ref"] == (
        "#/$defs/CardSelectionProvenance")
    assert schema["properties"]["selection_ready"]["type"] == "boolean"
    blockers = schema["properties"]["selection_blockers"]["items"]["enum"]
    assert "identity_not_native" in blockers and "work_terminal" in blockers
