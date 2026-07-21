"""Fail-closed architecture contract for future Card-driven selection."""
from __future__ import annotations

import pytest

from looplab.core.models import Card, Event, card_ownership_receipt, hypothesis_id
from looplab.events.replay import fold
from looplab.search.card_selection import card_action, eligible_cards
from looplab.search.policy import GreedyTree
from looplab.serve.public_cards import public_cards, public_cards_projection


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
    idea = {"operator": "improve", "params": {"lr": 0.2}, "eval_timeout": None}
    action = {
        "operator": "improve",
        "params": {"lr": 0.2},
        "space": None,
        "eval_profile": None,
        "eval_timeout": None,
        "parent_id": 1,
        "parent_ids": [1],
        "parent_generations": {"1": 0},
        "scored_against": 1,
        "scored_against_generation": 0,
        "scored_against_empty": False,
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
        "parent_generations": {"1": 0},
        "scored_against": 1,
        "scored_against_generation": 0,
        "scored_against_empty": False,
        "ownership_receipt": receipt,
    })


def _native_operator_card_added(
    card_id: str,
    statement: str,
    operator: str,
    parent_ids: list[int],
):
    parent_id = parent_ids[0] if parent_ids else None
    idea = {"operator": operator, "params": {}, "eval_timeout": None}
    action = {
        "operator": operator,
        "params": {},
        "space": None,
        "eval_profile": None,
        "eval_timeout": None,
        "parent_id": parent_id,
        "parent_ids": parent_ids,
        "parent_generations": {str(parent): 0 for parent in parent_ids},
        "scored_against": 1,
        "scored_against_generation": 0,
        "scored_against_empty": False,
        "footprint": None,
    }
    receipt = card_ownership_receipt(card_id, statement, action)
    assert receipt is not None
    return ("card_added", {
        "id": card_id,
        "statement": statement,
        "source": "engine",
        "idea": idea,
        "parent_id": parent_id,
        "parent_ids": parent_ids,
        "parent_generations": action["parent_generations"],
        "scored_against": 1,
        "scored_against_generation": 0,
        "scored_against_empty": False,
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


def test_receipt_bound_debug_card_accepts_failed_leaf_without_broadening_mutating_anchors():
    state = fold(_events([
        *_baseline(),
        ("node_created", {
            "node_id": 2,
            "operator": "improve",
            "parent_ids": [1],
            "idea": {"operator": "improve", "hypothesis": "broken candidate"},
        }),
        ("node_failed", {"node_id": 2, "reason": "crash", "eval_seconds": 0}),
        _native_operator_card_added(
            "debug-failed", "repair the failed candidate", "debug", [2]),
        _native_operator_card_added(
            "improve-failed", "mutate the failed candidate", "improve", [2]),
        _native_operator_card_added(
            "merge-failed", "merge with the failed candidate", "merge", [1, 2]),
    ]))

    assert [node.id for node in state.breedable_nodes()] == [1]
    debug = state.cards["debug-failed"]
    assert debug.selection_provenance.action_complete is True
    assert debug.selection_blockers == []
    assert debug.selection_ready is True
    assert state.cards["improve-failed"].selection_ready is False
    assert "action_receipt_incomplete" in state.cards["improve-failed"].selection_blockers
    assert state.cards["merge-failed"].selection_ready is False
    assert "action_receipt_incomplete" in state.cards["merge-failed"].selection_blockers
    assert [card.id for card in eligible_cards(
        state, GreedyTree(n_seeds=1, max_nodes=5, debug_depth=1),
    )] == ["debug-failed"]


def test_receipt_bound_expand_card_uses_improve_macro_but_preserves_idea_operator():
    state = fold(_events([
        *_baseline(),
        _native_operator_card_added(
            "expand-ready", "add a missing capability", "expand", [1]),
    ]))

    card = state.cards["expand-ready"]
    assert card.selection_ready is True
    assert card.operator == "expand"
    assert card_action(card) == {
        "kind": "improve", "parent_id": 1, "_card_id": "expand-ready",
    }


@pytest.mark.parametrize("disqualifying_rows", [
    [
        ("node_created", {
            "node_id": 3,
            "operator": "debug",
            "parent_ids": [2],
            "idea": {"operator": "debug", "hypothesis": "existing repair child"},
        }),
    ],
    [],
])
def test_debug_card_closes_when_failed_anchor_is_not_an_eligible_leaf(disqualifying_rows):
    failed_reason = "crash" if disqualifying_rows else "idea_rejected"
    state = fold(_events([
        *_baseline(),
        ("node_created", {
            "node_id": 2,
            "operator": "improve",
            "parent_ids": [1],
            "idea": {"operator": "improve", "hypothesis": "broken candidate"},
        }),
        ("node_failed", {"node_id": 2, "reason": failed_reason, "eval_seconds": 0}),
        _native_operator_card_added(
            "debug-closed", "repair a no-longer-eligible failure", "debug", [2]),
        *disqualifying_rows,
    ]))

    card = state.cards["debug-closed"]
    assert card.selection_ready is False
    assert card.selection_provenance.action_complete is False
    assert "action_receipt_incomplete" in card.selection_blockers


def test_eval_timeout_and_lifecycle_fences_are_receipt_bound():
    short = _native_card_added("short-timeout")[1]
    long = _native_card_added("long-timeout")[1]
    short["idea"]["eval_timeout"] = 60.0
    long["idea"]["eval_timeout"] = 3600.0
    for row in (short, long):
        action = {
            "operator": row["idea"]["operator"], "params": row["idea"]["params"],
            "space": None, "eval_profile": None,
            "eval_timeout": row["idea"]["eval_timeout"],
            "parent_id": 1, "parent_ids": [1], "parent_generations": {"1": 0},
            "scored_against": 1, "scored_against_generation": 0,
            "scored_against_empty": False, "footprint": None,
        }
        row["ownership_receipt"] = card_ownership_receipt(row["id"], row["statement"], action)

    same_identity_short = {**action, "eval_timeout": 60.0}
    same_identity_long = {**action, "eval_timeout": 3600.0}
    assert card_ownership_receipt(
        "same-card", "same statement", same_identity_short,
    )["action_digest"] != card_ownership_receipt(
        "same-card", "same statement", same_identity_long,
    )["action_digest"]
    state = fold(_events([*_baseline(), ("card_added", short), ("card_added", long)]))
    assert state.cards["short-timeout"].eval_timeout == 60.0
    assert state.cards["long-timeout"].eval_timeout == 3600.0
    assert state.cards["short-timeout"].selection_ready is True
    assert state.cards["long-timeout"].selection_ready is True


def test_lifecycle_generation_change_makes_receipt_bound_action_stale():
    rows = [*_baseline(), _native_card_added("generation-fenced")]
    current = fold(_events(rows)).cards["generation-fenced"]
    assert current.selection_provenance.freshness == "current"

    reset = fold(_events([
        *rows,
        ("node_reset", {"node_id": 1, "generation": 0, "from_stage": "eval"}),
    ])).cards["generation-fenced"]
    assert reset.parent_generations == {"1": 0}
    assert reset.scored_against_generation == 0
    assert reset.selection_provenance.freshness == "stale"
    assert "freshness_stale" in reset.selection_blockers


def test_sparse_receipt_missing_new_action_fences_remains_visible_but_unknown():
    card_id, statement = "sparse-receipt", "sparse queued action"
    sparse_action = {
        "operator": "improve", "params": {"lr": 0.2}, "space": None,
        "eval_profile": None, "parent_id": 1, "parent_ids": [1],
        "scored_against": 1, "footprint": None,
    }
    state = fold(_events([*_baseline(), ("card_added", {
        "id": card_id, "statement": statement,
        "idea": {"operator": "improve", "params": {"lr": 0.2}},
        "parent_id": 1, "parent_ids": [1], "scored_against": 1,
        "ownership_receipt": card_ownership_receipt(card_id, statement, sparse_action),
    })]))
    card = state.cards[card_id]
    assert card.identity.kind == "native"  # sparse proof stays visible but cannot enter the queue
    assert card.parent_generations is None
    assert card.scored_against_generation is None
    assert card.selection_provenance.action_complete is False
    assert card.selection_provenance.freshness == "unknown"
    assert card.selection_ready is False


def test_explicit_empty_score_and_parent_fences_are_current_only_while_run_is_empty():
    statement = "first seed"
    action = {
        "operator": "draft", "params": {}, "space": {}, "eval_profile": None,
        "eval_timeout": None, "parent_id": None, "parent_ids": [],
        "parent_generations": {}, "scored_against": None,
        "scored_against_generation": None, "scored_against_empty": True,
        "footprint": None,
    }
    added = ("card_added", {
        "id": "first-seed", "statement": statement,
        "idea": {"operator": "draft", "params": {}, "space": {},
                 "eval_profile": None, "eval_timeout": None},
        "parent_id": None, "parent_ids": [], "parent_generations": {},
        "scored_against": None, "scored_against_generation": None,
        "scored_against_empty": True,
        "ownership_receipt": card_ownership_receipt("first-seed", statement, action),
    })
    empty = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}), added,
    ])).cards["first-seed"]
    assert empty.selection_provenance.freshness == "current"
    assert empty.selection_ready is True

    with_best = fold(_events([*_baseline(), added])).cards["first-seed"]
    assert with_best.selection_provenance.freshness == "stale"
    assert with_best.selection_ready is False


def test_node_building_card_link_marks_only_its_native_card_in_flight_and_fail_closed():
    card_id = "native-building-card"
    other_id = "native-proposed-card"
    state = fold(_events([
        *_baseline(),
        _native_card_added(card_id, "proposal being implemented"),
        _native_card_added(other_id, "independent queued proposal"),
        ("node_building", {
            "node_id": 2, "operator": "improve", "parent_ids": [1], "card_id": card_id,
        }),
    ]))

    card = state.cards[card_id]
    assert card.status == "building"
    assert card.evidence == []  # a reservation is not node evidence
    assert card.selection_provenance.owner_state == "in_flight"
    assert card.selection_blockers == ["work_in_flight"]
    assert card.selection_ready is False
    assert state.buildings[2]["card_id"] == card_id

    other = state.cards[other_id]
    assert other.status == "proposed"
    assert other.selection_provenance.owner_state == "none"
    assert other.selection_blockers == [] and other.selection_ready is True


def test_node_created_replaces_the_build_link_with_normal_card_evidence():
    card_id = "native-build-completes"
    statement = "proposal that finishes building"
    rows = [
        *_baseline(),
        _native_card_added(card_id, statement),
        ("node_building", {
            "node_id": 2, "operator": "improve", "parent_ids": [1], "card_id": card_id,
        }),
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

    building = fold(_events(rows[:-1])).cards[card_id]
    assert building.status == "building" and building.evidence == []

    completed = fold(_events(rows))
    card = completed.cards[card_id]
    assert completed.buildings == {}
    assert card.status == "running" and card.evidence == [2]
    assert card.selection_provenance.owner_state == "in_flight"
    assert card.selection_ready is False


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


def test_native_footprint_stamps_researcher_authority_and_rejects_forged_authority():
    valid = _native_card_added("valid-footprint")[1]
    forged = _native_card_added("forged-footprint")[1]
    for row, footprint in (
        (valid, {"gpus": 2, "gpu_mem_mib": 8192}),
        (forged, {"gpus": 2, "pinned_by": "operator"}),
    ):
        row["footprint"] = footprint
        row["ownership_receipt"] = card_ownership_receipt(row["id"], row["statement"], {
            "operator": "improve", "params": {"lr": 0.2}, "space": None,
            "eval_profile": None, "eval_timeout": None,
            "parent_id": 1, "parent_ids": [1], "parent_generations": {"1": 0},
            "scored_against": 1, "scored_against_generation": 0,
            "scored_against_empty": False, "footprint": footprint,
        })

    state = fold(_events([*_baseline(), ("card_added", valid), ("card_added", forged)]))
    assert state.cards["valid-footprint"].footprint == {
        "gpus": 2, "gpu_mem_mib": 8192, "proposed_by": "researcher",
    }
    assert state.cards["valid-footprint"].selection_ready is True
    assert state.cards["forged-footprint"].footprint == {
        "gpus": 2, "proposed_by": "researcher",
    }
    assert state.cards["forged-footprint"].selection_provenance.action_complete is False
    assert state.cards["forged-footprint"].selection_ready is False


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
            "eval_profile": None, "eval_timeout": None,
            "parent_id": 1, "parent_ids": [1], "parent_generations": {"1": 0},
            "scored_against": 0, "scored_against_generation": 0,
            "scored_against_empty": False, "footprint": None,
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
    envelope = public_cards_projection({card.id: card}).model_dump(mode="json")
    assert envelope["cards_projection"]["complete"] is True
    assert envelope["cards_projection"]["items"][card.id]["complete"] is True
    assert dto["selection_ready"] is True
    assert dto["selection_blockers"] == []
    assert dto["identity"]["kind"] == "native"
    assert dto["selection_provenance"]["owner_state"] == "none"
    assert dto["parent_generations"] == {"1": 0}
    assert dto["scored_against_generation"] == 0
    assert dto["scored_against_empty"] is False

    hostile = public_cards({
        "forged": {
            "status": "proposed", "verdict": "open", "actionable": True,
            "selection_ready": True, "selection_blockers": [],
            "statement": "forged without proof",
        },
    })["forged"]
    assert hostile["selection_ready"] is False
    assert hostile["selection_blockers"]

    valid_proof = card.model_dump(mode="json")
    for mutation in (
        {"status": "running", "evidence": [7]},
        {"status": "evaluated", "evidence": [7]},
        {"dropped_reason": "operator rejected it"},
        {"merged_into": "another-work-item"},
        {"aliases": ["another-work-item"]},
        {"seed_statement": "oversized" * 100_000, "aliases": ["legacy-shadow"]},
    ):
        contradictory = public_cards({
            "contradictory": {**valid_proof, "id": "contradictory", **mutation},
        })["contradictory"]
        assert contradictory["selection_ready"] is False
        assert contradictory["selection_blockers"]


def test_card_schema_exposes_identity_readiness_and_stable_blockers():
    schema = Card.model_json_schema()
    assert schema["properties"]["identity"]["$ref"] == "#/$defs/CardIdentityProvenance"
    assert schema["properties"]["selection_provenance"]["$ref"] == (
        "#/$defs/CardSelectionProvenance")
    assert schema["properties"]["selection_ready"]["type"] == "boolean"
    assert schema["properties"]["pinned"]["type"] == "boolean"
    blockers = schema["properties"]["selection_blockers"]["items"]["enum"]
    assert "identity_not_native" in blockers and "work_terminal" in blockers
