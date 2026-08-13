"""Fail-closed architecture contract for future Card-driven selection."""
from __future__ import annotations

import pytest

from looplab.core.models import Card, Event, card_ownership_receipt, hypothesis_id
from looplab.events.replay import fold
from looplab.search.card_selection import (_card_generation_fences_current, card_action,
                                           eligible_cards)
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


def _card_against(
    card_id: str,
    statement: str,
    *,
    operator: str,
    parent_ids: tuple[int, ...],
    scored_against: int,
    generations: dict[int, int] | None = None,
):
    """One receipt-bound Card whose score anchor is named INDEPENDENTLY of its parents.

    `_native_card_added` and `_native_operator_card_added` both hard-code `scored_against=1`, which
    cannot express the question this file's champion tests ask: two cards proposed under DIFFERENT
    champions.
    """
    attempts = generations or {}
    idea = {"operator": operator, "params": {}, "eval_timeout": None}
    action = {
        "operator": operator,
        "params": {},
        "space": None,
        "eval_profile": None,
        "eval_timeout": None,
        "parent_id": parent_ids[0] if parent_ids else None,
        "parent_ids": list(parent_ids),
        "parent_generations": {str(parent): attempts.get(parent, 0) for parent in parent_ids},
        "scored_against": scored_against,
        "scored_against_generation": attempts.get(scored_against, 0),
        "scored_against_empty": False,
        "footprint": None,
    }
    receipt = card_ownership_receipt(card_id, statement, action)
    assert receipt is not None
    return ("card_added", {
        "id": card_id,
        "statement": statement,
        "source": "researcher",
        "idea": idea,
        "parent_id": action["parent_id"],
        "parent_ids": action["parent_ids"],
        "parent_generations": action["parent_generations"],
        "scored_against": scored_against,
        "scored_against_generation": action["scored_against_generation"],
        "scored_against_empty": False,
        "ownership_receipt": receipt,
    })


def _evaluated_node(node_id: int, metric: float):
    return [
        ("node_created", {
            "node_id": node_id,
            "operator": "draft",
            "idea": {"operator": "draft", "hypothesis": f"seed {node_id}"},
        }),
        ("node_evaluated", {"node_id": node_id, "metric": metric}),
    ]


def test_two_cards_proposed_under_different_champions_are_both_selectable():
    """The property `eval_parallel > 1` needs and could never have: a queue of depth two.

    Until 2026-08-13 the score fence also demanded `state.best_node_id == card.scored_against`, so a
    Card proposed while node 1 was champion went permanently `freshness_stale` the moment node 2 beat
    it — and with one proposer minting one Card at a time, two fresh selectable Cards could never
    coexist. Measured on `runs/rubertlite-dr-unified-v6`: `card-3 scored_against=1 blockers=
    ['freshness_stale']` beside `card-5 scored_against=2 status=running`, with the second H200 idle
    for the whole run. See `core/cards.py::card_score_fence_state`.

    Both halves are asserted, because they are two different gates on two sides of the layer cut: the
    FOLD's `selection_ready` receipt (Layer 3's queue) and the selection-time recheck
    `_card_generation_fences_current` (the Layer-5 producer/freshness gate). Narrowing only one would
    leave the other refusing the same Card.
    """
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        *_evaluated_node(1, 0.5),
        _card_against("under-champion-1", "improve on the first champion",
                      operator="improve", parent_ids=(1,), scored_against=1),
        *_evaluated_node(2, 0.9),
        _card_against("under-champion-2", "improve on the second champion",
                      operator="improve", parent_ids=(2,), scored_against=2),
    ]
    state = fold(_events(rows))
    assert state.best_node_id == 2

    older = state.cards["under-champion-1"]
    newer = state.cards["under-champion-2"]
    # The superseded Card's anchor is untouched: alive, un-reset, at the exact attempt it was scored
    # on. Nothing about it changed except that some OTHER node scored higher.
    assert older.scored_against == 1 and older.scored_against_generation == 0
    assert state.nodes[1].attempt == 0 and not state.nodes[1].tombstoned
    for card in (older, newer):
        assert card.selection_provenance.freshness == "current"
        assert card.selection_blockers == []
        assert card.selection_ready is True
        assert _card_generation_fences_current(state, card) is True

    assert [card.id for card in eligible_cards(state, GreedyTree())] == [
        "under-champion-1", "under-champion-2",
    ]


def test_a_dead_score_anchor_is_still_refused_after_the_champion_clause_was_narrowed():
    """The control for the test above: what the score fence actually protects still bites.

    The fence's job is that the node a proposal was scored against is still the SAME experiment.
    Losing that node — tombstoned, aborted, or re-run under a new attempt — is exactly the case it
    exists for, and narrowing the champion clause must not have touched it.
    """
    base = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        *_evaluated_node(1, 0.5),
        *_evaluated_node(2, 0.9),
        _card_against("anchored-on-1", "improve on the first champion",
                      operator="improve", parent_ids=(2,), scored_against=1),
    ]
    fresh = fold(_events(base)).cards["anchored-on-1"]
    assert fresh.selection_provenance.freshness == "current" and fresh.selection_ready is True

    for killer in (
        ("node_tombstoned", {"node_ids": [1]}),
        ("node_abort", {"node_id": 1, "generation": 0}),
        ("node_reset", {"node_id": 1, "generation": 0, "from_stage": "eval"}),
    ):
        state = fold(_events([*base, killer]))
        card = state.cards["anchored-on-1"]
        assert card.selection_provenance.freshness == "stale", killer[0]
        assert "freshness_stale" in card.selection_blockers, killer[0]
        assert card.selection_ready is False, killer[0]
        assert _card_generation_fences_current(state, card) is False, killer[0]
        assert eligible_cards(state, GreedyTree()) == [], killer[0]


def test_a_merge_whose_parents_left_the_top_two_is_still_refused():
    """The property the champion clause was a (crude, unsound) proxy for.

    A merge Card binds the two nodes that were the policy top-2 when it was proposed.  "The champion
    is unchanged" neither implies nor is implied by "the top-2 is unchanged", so removing that clause
    cannot be what keeps a merge honest — `search/card_selection.py::_live_card_action` rechecks the
    top-2 by METRIC, and it is what must still refuse this Card.  Asserted through the FOLD's own
    verdict too, so the test shows WHICH gate holds the line: the receipt is still `selection_ready`,
    and the live anchor recheck is what drops it out of the queue.
    """
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        *_evaluated_node(1, 0.5),
        *_evaluated_node(2, 0.9),
        _card_against("merge-of-the-old-top-two", "merge the two leaders",
                      operator="merge", parent_ids=(2, 1), scored_against=2),
    ]
    while_top_two = fold(_events(rows))
    assert [card.id for card in eligible_cards(while_top_two, GreedyTree())] == [
        "merge-of-the-old-top-two",
    ]

    # A third node outscores node 1, so the current top-2 is {3, 2} and this merge no longer names it.
    superseded = fold(_events([*rows, *_evaluated_node(3, 1.5)]))
    card = superseded.cards["merge-of-the-old-top-two"]
    assert card.selection_provenance.freshness == "current"
    assert card.selection_ready is True
    assert _card_generation_fences_current(superseded, card) is True
    assert eligible_cards(superseded, GreedyTree()) == []


def test_one_receipt_bound_fresh_work_item_is_selection_ready_independent_of_id_shape():
    # A native id may happen to look exactly like a legacy statement hash. Receipt ownership, not
    # spelling, is the discriminator.
    card_id = hypothesis_id("some unrelated direction")
    added = _native_card_added(card_id)
    assert added[1]["ownership_receipt"]["v"] == 2
    assert added[1]["ownership_receipt"]["action_digest"].startswith("card-action:v2:")
    state = fold(_events([*_baseline(), added]))

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
    """The fold's shape rule for a HISTORICAL `debug` Card, kept because old logs still contain them.

    Only the last assertion moved with F5: the fold still calls such a Card's receipt complete (a
    failed leaf IS a valid debug anchor, and `improve`/`merge` on the same node still are not — that
    asymmetry is what this case exists for), and `eligible_cards` now refuses to hand it to anyone.
    The `improve-failed` row beside it is the F5 property from the other side, and it always held:
    a failed node has never been an `improve` anchor, so no `improve` Card can be a Debug node under
    another name."""
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
    )] == [], "a historical debug Card is never executable again (F5)"


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


def test_frozen_legacy_v1_receipt_stays_native_but_cannot_claim_new_fences():
    # Fixture produced by the pre-lifecycle-fence writer. Keep the digest literal: deriving it through
    # today's helper would let a future accidental v1 preimage change rewrite both sides of the test.
    card_id = "legacy-v1"
    statement = "legacy queued improvement"
    legacy_receipt = {
        "v": 1,
        "card_id": card_id,
        "action_digest": (
            "card-action:v1:b11eab546c7801b0d8407cf3a65ab4ff"
            "9550498de48eaf62c395f85e5dc0d6a6"
        ),
    }
    state = fold(_events([*_baseline(), ("card_added", {
        "id": card_id,
        "statement": statement,
        "source": "researcher",
        "idea": {
            "operator": "improve",
            "params": {"lr": 0.2},
            "space": {},
            "eval_profile": None,
        },
        "parent_id": 1,
        "parent_ids": [1],
        "scored_against": 1,
        "ownership_receipt": legacy_receipt,
    })]))

    card = state.cards[card_id]
    assert card.identity.kind == "native"
    assert card.identity.receipt_valid is True
    assert card.identity.action_digest == legacy_receipt["action_digest"]
    assert card.selection_provenance.action_complete is False
    assert card.selection_provenance.freshness == "unknown"
    assert "action_receipt_incomplete" in card.selection_blockers
    assert card.selection_ready is False


def test_short_lived_expanded_v1_receipt_remains_current_during_v2_upgrade():
    # A few releases minted the expanded lifecycle-fenced preimage with the old v1 label. Accept those
    # exact durable rows during replay, while every new writer emits v2 (asserted above).
    card_id = "expanded-v1"
    statement = "transition queued improvement"
    state = fold(_events([*_baseline(), ("card_added", {
        "id": card_id,
        "statement": statement,
        "source": "researcher",
        "idea": {
            "operator": "improve",
            "params": {"lr": 0.2},
            "space": {},
            "eval_profile": None,
            "eval_timeout": 60.0,
        },
        "parent_id": 1,
        "parent_ids": [1],
        "parent_generations": {"1": 0},
        "scored_against": 1,
        "scored_against_generation": 0,
        "scored_against_empty": False,
        "ownership_receipt": {
            "v": 1,
            "card_id": card_id,
            "action_digest": (
                "card-action:v1:6302a6d374bf49ff2faaa5738f743e04"
                "7ae93a1132d19680a4212bc60e6d714c"
            ),
        },
    })]))

    card = state.cards[card_id]
    assert card.identity.kind == "native"
    assert card.identity.receipt_valid is True
    assert card.selection_provenance.action_complete is True
    assert card.selection_provenance.freshness == "current"
    assert card.selection_blockers == []
    assert card.selection_ready is True


def test_explicit_empty_score_and_parent_fences_are_current_only_while_run_is_empty():
    """The EMPTY score authority still requires an empty board — deliberately, and only here.

    ``card_score_fence_state`` dropped the champion-equality clause from the INCUMBENT branch on
    2026-08-13; this branch keeps its equivalent (``best_node_id is None``) on purpose, and that
    helper's docstring owns the reason. Pin the asymmetry so it stays a decision rather than drift.
    """
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

    # …and a receipt claiming empty authority AND an anchor generation is malformed, never merely
    # empty: stale even on the empty board that would otherwise make it current.
    malformed = ("card_added", {**added[1], "scored_against_generation": 0})
    broken = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}), malformed,
    ])).cards["first-seed"]
    assert broken.selection_provenance.freshness == "stale"
    assert broken.selection_ready is False


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


def test_an_outstanding_build_request_leaves_its_card_claimable_by_the_head_servicer():
    """`card_build_requested` must NOT make the card unready — the producer re-folds and needs it.

    A review proposal wanted outstanding request heads folded into the same in-flight ownership set
    as `node_building` markers, on the grounds that the engine already excludes them from election.
    That conflates two different questions. `_election_excluded_card_ids` prevents a SECOND build of
    the same card; `selection_ready` is what lets the FIRST one proceed — `_prepare_existing_card_claim`
    demands it, and both `_producer_card_reservation` and `_commit_card_build` re-fold AFTER the
    request is already durable. Blocking on the request therefore starves its own servicer: every
    speculative build returns "stale" and the engine test hangs (observed, not hypothesised).

    Ownership is stamped at `node_building`, one step later, once the claim has been validated.
    """
    from looplab.engine.speculation import SpeculationMixin
    from looplab.search.card_selection import _strictly_selection_ready

    card_id = "opaque-work-item"
    rows = [*_baseline(), _native_card_added(card_id)]
    reserved = fold(_events([
        *rows,
        ("card_build_requested", {"card_id": card_id, "generation": 0}),
    ]))
    card = reserved.cards[card_id]
    assert card.selection_provenance.owner_state == "none"
    assert card.selection_blockers == []
    assert card.selection_ready is True
    assert card.status == "proposed"
    # The exact predicate the claim path re-asserts before reconstructing the Idea.
    assert _strictly_selection_ready(card) is True
    # ...while the engine's election exclusion DOES hold it, so it cannot be built twice.
    assert SpeculationMixin._speculative_card_ids(reserved) == {card_id}

    # `node_building` is where ownership becomes visible to the fold.
    building = fold(_events([
        *rows,
        ("card_build_requested", {"card_id": card_id, "generation": 0}),
        ("node_building", {"node_id": 4, "operator": "improve", "parent_ids": [1],
                           "card_id": card_id}),
    ]))
    owned = building.cards[card_id]
    assert owned.selection_provenance.owner_state == "in_flight"
    assert "work_in_flight" in owned.selection_blockers
    assert owned.selection_ready is False
    assert owned.status == "building"


# --- the debug anchor vs. the Card's OWN work item (2026-08-05) -----------------------------------
#
# READ THIS BEFORE DELETING ANY OF THE SECTION BELOW. The Debug node was removed on 2026-08-13 (F5),
# so nothing AUTHORS a `debug` Card any more — but `fold` still has to answer correctly about the
# logs that already exist, and every preserved run under `runs/` with a `debug` Card folds through
# exactly this machinery. So these cases stay and their FOLD assertions are unchanged; what changed
# is the selection half, which now refuses the Card the fold still describes correctly. Replay
# reports what happened; selection refuses to repeat it. The two halves being allowed to disagree
# HERE (and nowhere else) is the point — see `events/card_ledger.py::_card_debug_leaf_children`.
#
# `_card_debuggable_leaf_ids` disqualified a failed node the moment it had ANY child — and a
# receipt-bound `debug` Card's own work item IS such a child. So the instant that node existed the
# Card's own anchor died and it folded to `action_receipt_incomplete`. The ordinary lane never
# noticed (it does not re-check a Card after its node exists); the L5 freshness gate did, refused any
# blocker set beyond `{work_in_flight}`, and superseded every speculative `debug` prefetch on sight.
# The lane then authored a fresh permanently-unselectable Card per loop turn until the runaway guard
# ended the run. These pin the exemption AND its exact boundary.


def _speculative_build(node_id: int, card_id: str, operator: str, parent_ids: list[int]):
    """The four-row durable lifecycle a Layer-5 build writes for one Card."""
    return [
        ("card_build_requested", {"card_id": card_id, "generation": 0}),
        ("node_building", {"node_id": node_id, "operator": operator, "parent_ids": parent_ids,
                           "card_id": card_id, "speculative": True, "card_build_generation": 0}),
        ("node_created", {
            "node_id": node_id, "parent_ids": parent_ids, "operator": operator,
            "idea": {"operator": operator, "params": {}, "card_id": card_id},
            "parent_generations": {str(parent): 0 for parent in parent_ids},
            "speculative": True, "card_build_generation": 0, "eval_start_boundary": True}),
        ("card_build_done", {"card_id": card_id, "generation": 0, "node_id": node_id,
                             "speculative": True}),
    ]


def _discarded(node_id: int):
    """The freshness gate's zero-cost terminal — the receipt `is_unevaluated_speculative_discard`
    reads to prove the build never ran."""
    return ("node_failed", {
        "node_id": node_id, "generation": 0, "error": "superseded by Card freshness gate",
        "reason": "superseded", "eval_seconds": 0.0, "never_evaluated": True})


def _failed_node_2(*, trust_gate: str | None = None):
    baseline = list(_baseline())
    if trust_gate is not None:
        # the trust-gated child class below needs a run whose gate actually excludes; `audit`
        # (the `run_started` default) flags nothing, so `breed_excluded` stays empty.
        baseline[0] = ("run_started", {**baseline[0][1], "trust_gate": trust_gate})
    return [
        *baseline,
        ("node_created", {
            "node_id": 2, "operator": "improve", "parent_ids": [1],
            "idea": {"operator": "improve", "hypothesis": "broken candidate"}}),
        ("node_failed", {"node_id": 2, "reason": "crash", "eval_seconds": 0.0}),
    ]


def test_a_debug_cards_own_work_item_does_not_close_its_own_anchor():
    """The load-bearing half. Its own node must leave the Card's blockers at exactly
    `{work_in_flight}` — the ONE set the L5 freshness counterfactual accepts."""
    rows = [
        *_failed_node_2(),
        _native_operator_card_added("debug-own", "repair the failed candidate", "debug", [2]),
    ]
    before = fold(_events(rows))
    assert before.cards["debug-own"].selection_ready is True

    owned = fold(_events([*rows, *_speculative_build(3, "debug-own", "debug", [2])]))
    card = owned.cards["debug-own"]
    assert card.evidence == [3], "its own work item"
    assert card.selection_blockers == ["work_in_flight"], (
        "a Card's own node must not revoke its own anchor — `action_receipt_incomplete` here is what "
        "made every speculative debug prefetch superseded on sight")
    assert card.selection_provenance.action_complete is True


def test_a_debug_card_whose_parent_gained_a_REAL_sibling_child_still_closes():
    """The exemption's boundary, and the proof it opens no hole: the Card's OWN node is spared,
    every other child of the same failed parent still disqualifies it."""
    rows = [
        *_failed_node_2(),
        _native_operator_card_added("debug-own", "repair the failed candidate", "debug", [2]),
        *_speculative_build(3, "debug-own", "debug", [2]),
    ]
    # A second, unrelated node bred from the same failed parent — an ordinary evaluated child, not a
    # discarded prefetch. Node 2 is no longer a leaf by any reading.
    sibling = ("node_created", {
        "node_id": 4, "operator": "debug", "parent_ids": [2],
        "idea": {"operator": "debug", "hypothesis": "someone else's repair"}})
    state = fold(_events([*rows, sibling]))
    card = state.cards["debug-own"]
    assert card.selection_provenance.action_complete is False
    assert "action_receipt_incomplete" in card.selection_blockers
    assert card.selection_ready is False

    # …and the parent is not a debuggable leaf for anyone.
    from looplab.events.card_ledger import _card_debuggable_leaf_ids
    assert 2 not in _card_debuggable_leaf_ids(state)


def test_a_discarded_prefetch_is_not_a_child_for_the_debug_anchor():
    """The second-order half. Replay counted the discard as a child while the Card lane's policy
    view (`_effective_policy_state` -> `node_counts_toward_card_budget`) hid it, so the policy kept
    proposing `debug` on the crashed parent and the lane authored a fresh unselectable Card every
    turn. Both views now read the SAME predicate."""
    from looplab.core.models import is_unevaluated_speculative_discard
    from looplab.events.card_ledger import _card_debuggable_leaf_ids

    rows = [
        *_failed_node_2(),
        _native_operator_card_added("debug-later", "repair the failed candidate", "debug", [2]),
        # a prefetch on the same failed parent that the freshness gate threw away before dispatch
        _native_operator_card_added("debug-thrown", "an earlier repair attempt", "debug", [2]),
        *_speculative_build(3, "debug-thrown", "debug", [2]),
        _discarded(3),
    ]
    state = fold(_events(rows))
    assert is_unevaluated_speculative_discard(state, state.nodes[3]) is True
    assert 2 in _card_debuggable_leaf_ids(state), (
        "a build that never ran spent no budget and must not end its parent's life as a leaf")
    assert state.cards["debug-later"].selection_ready is True
    # …and the fold saying so is now as far as it goes: `card_action` refuses to turn a historical
    # `debug` Card back into an executable action (F5). `selection_ready` is a statement about the
    # RECEIPT, not a licence, and this is where the two part company.
    assert card_action(state.cards["debug-later"]) is None


def test_an_ordinary_superseded_child_still_closes_the_debug_anchor():
    """Only a PROVEN discard is skipped. An ordinary build/reset race uses the same
    `reason='superseded'` and keeps its slot, so it is still a real child."""
    from looplab.core.models import is_unevaluated_speculative_discard
    from looplab.events.card_ledger import _card_debuggable_leaf_ids

    rows = [
        *_failed_node_2(),
        _native_operator_card_added("debug-later", "repair the failed candidate", "debug", [2]),
        _native_operator_card_added("debug-raced", "a raced repair attempt", "debug", [2]),
        *_speculative_build(3, "debug-raced", "debug", [2]),
        # no `never_evaluated`, no freshness receipt: the log does not prove this one never ran
        ("node_failed", {"node_id": 3, "generation": 0, "error": "lost the build race",
                         "reason": "superseded", "eval_seconds": 0.0}),
    ]
    state = fold(_events(rows))
    assert is_unevaluated_speculative_discard(state, state.nodes[3]) is False
    assert 2 not in _card_debuggable_leaf_ids(state)
    assert state.cards["debug-later"].selection_ready is False
    assert "action_receipt_incomplete" in state.cards["debug-later"].selection_blockers


# --- …and the OTHER THREE classes the Card lane's node universe hides (2026-08-05, second cut) ----
#
# The first cut shared exactly ONE clause of `node_counts_toward_card_budget` with the fold
# (`is_unevaluated_speculative_discard`) and left the other three re-derived by omission, so the
# identical runaway reopened the same day on a TOMBSTONED child, a constraint-gated
# (`feasible=False`) child and a trust-gated (`breed_excluded`) child. `_effective_policy_state`
# hides all four; the fold's child map hid one; a failed node whose only child is in the other three
# was therefore a debuggable leaf to the policy and a non-leaf to replay, and the lane authored a
# fresh permanently-unselectable `debug` Card every loop turn. Measured end-to-end on a 12-node
# budget, tombstoned and constraint-gated shapes alike: 7 nodes frozen, 89 `card_added` of which 84
# dead `debug` Cards on ONE parent, run dead on "stuck: 1 action(s) planned for 84 consecutive loop
# turns without creating a node"; the same prefix finishes 12-of-12 once the map is right. Both
# halves now call the predicate itself, which is why the tests below are one case per CLASS and one
# property over the predicate — not a fourth rule that can drift again.

# class -> (trust_gate, the rows that retire node 3 into it)
_CHILDREN_THE_POLICY_VIEW_HIDES = {
    "tombstoned": (None, [
        ("node_evaluated", {"node_id": 3, "metric": 0.9}),
        ("node_tombstoned", {"node_ids": [3]}),
    ]),
    "constraint-gated": (None, [
        ("node_evaluated", {"node_id": 3, "metric": 0.9,
                            "violations": [{"name": "latency_ms", "value": 900, "max": 500}]}),
    ]),
    "trust-gated": ("gate", [
        ("node_evaluated", {"node_id": 3, "metric": 0.9}),
        ("reward_hack_suspected", {"node_id": 3, "generation": 0,
                                   "signals": [{"signal": "critic:hardcoded_metric"}]}),
    ]),
}

# the same shape for children that DID spend budget — the boundary these must not cross.
_CHILDREN_THE_POLICY_VIEW_KEEPS = {
    "evaluated": [("node_evaluated", {"node_id": 3, "metric": 0.9})],
    "failed": [("node_failed", {"node_id": 3, "reason": "crash", "eval_seconds": 4.0})],
    # aborted attempts still consumed a real build; `node_counts_toward_card_budget` keeps them, and
    # the removed `debug_action`'s `has_child` counted them too, so the two views agreed without a
    # special case — which is why removing the producer left the fold half correct as it stood.
    "aborted": [("node_abort", {"node_id": 3, "reason": "operator"}),
                ("node_failed", {"node_id": 3, "reason": "aborted", "eval_seconds": 1.0})],
    "still pending": [],
}


def _failed_parent_with_child(kill_rows, *, trust_gate=None):
    return [
        *_failed_node_2(trust_gate=trust_gate),
        ("node_created", {"node_id": 3, "operator": "debug", "parent_ids": [2],
                          "idea": {"operator": "debug", "hypothesis": "the first repair"}}),
        *kill_rows,
        _native_operator_card_added("debug-2nd", "repair the failed candidate again", "debug", [2]),
    ]


@pytest.mark.parametrize("child_class", sorted(_CHILDREN_THE_POLICY_VIEW_HIDES))
def test_a_child_the_policy_view_hides_leaves_the_debug_anchor_open(child_class):
    """Each class on its own. The policy proposes `debug` on the crashed parent; before the fix the
    fold refused the resulting Card forever, which is the runaway."""
    from looplab.core.models import node_counts_toward_card_budget
    from looplab.events.card_ledger import _card_debug_leaf_children, _card_debuggable_leaf_ids
    trust_gate, kill_rows = _CHILDREN_THE_POLICY_VIEW_HIDES[child_class]
    state = fold(_events(_failed_parent_with_child(kill_rows, trust_gate=trust_gate)))

    assert node_counts_toward_card_budget(state, state.nodes[3]) is False, child_class
    # The fold, reading the predicate rather than a second rule. This half is unchanged by F5 and is
    # what a replay of a preserved run depends on; the `debug_action(...)` assertion that used to sit
    # above it is gone with the producer.
    assert _card_debug_leaf_children(state).get(2) is None
    assert 2 in _card_debuggable_leaf_ids(state)
    assert state.cards["debug-2nd"].selection_blockers == []
    # …and it is STILL not executable, because a Debug node cannot be created at all now. The
    # runaway this case was written for needed the fold and the policy to disagree; there is no
    # longer a proposal for them to disagree about.
    assert card_action(state.cards["debug-2nd"]) is None


@pytest.mark.parametrize("child_class", sorted(_CHILDREN_THE_POLICY_VIEW_KEEPS))
def test_a_child_the_policy_view_KEEPS_still_closes_the_debug_anchor(child_class):
    """The boundary in the other direction: closing the divergence must open no hole. A child that
    spent budget still ends its failed parent's life as a debuggable leaf, in BOTH views."""
    from looplab.core.models import node_counts_toward_card_budget
    from looplab.events.card_ledger import _card_debug_leaf_children, _card_debuggable_leaf_ids
    state = fold(_events(
        _failed_parent_with_child(_CHILDREN_THE_POLICY_VIEW_KEEPS[child_class])))

    assert node_counts_toward_card_budget(state, state.nodes[3]) is True, child_class
    assert _card_debug_leaf_children(state)[2] == frozenset({3})
    assert 2 not in _card_debuggable_leaf_ids(state)
    card = state.cards["debug-2nd"]
    assert card.selection_ready is False
    assert "action_receipt_incomplete" in card.selection_blockers


def test_the_folds_child_map_is_exactly_the_policy_views_child_map():
    """The PROPERTY the four cases are instances of, and the one that survives a fifth class.

    `_effective_policy_state` builds the policy's node universe by filtering `state.nodes` through
    `node_counts_toward_card_budget`; the fold's child map must be the parent->children map OF THAT
    UNIVERSE. Asserting the two maps are equal on a state carrying every class at once is what makes
    a future edit to the predicate move both halves together instead of reopening this defect on
    whatever class is added next.
    """
    from looplab.events.card_ledger import _card_debug_leaf_children
    from looplab.search.card_selection import _effective_policy_state

    rows = [
        *_failed_node_2(trust_gate="gate"),
        # one child per class, all on the same failed parent, plus a real one so the map is non-empty
        ("node_created", {"node_id": 3, "operator": "debug", "parent_ids": [2],
                          "idea": {"operator": "debug", "hypothesis": "tombstoned repair"}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.9}),
        ("node_tombstoned", {"node_ids": [3]}),
        ("node_created", {"node_id": 4, "operator": "debug", "parent_ids": [2],
                          "idea": {"operator": "debug", "hypothesis": "infeasible repair"}}),
        ("node_evaluated", {"node_id": 4, "metric": 0.9,
                            "violations": [{"name": "latency_ms", "value": 900, "max": 500}]}),
        ("node_created", {"node_id": 5, "operator": "debug", "parent_ids": [2],
                          "idea": {"operator": "debug", "hypothesis": "trust-gated repair"}}),
        ("node_evaluated", {"node_id": 5, "metric": 0.9}),
        ("reward_hack_suspected", {"node_id": 5, "generation": 0,
                                   "signals": [{"signal": "critic:hardcoded_metric"}]}),
        _native_operator_card_added("debug-thrown", "a discarded prefetch", "debug", [2]),
        *_speculative_build(6, "debug-thrown", "debug", [2]),
        _discarded(6),
        ("node_created", {"node_id": 7, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": "an ordinary child"}}),
        ("node_evaluated", {"node_id": 7, "metric": 0.7}),
    ]
    state = fold(_events(rows))

    expected: dict[int, set[int]] = {}
    for node in _effective_policy_state(state).nodes.values():
        for parent_id in node.parent_ids:
            expected.setdefault(parent_id, set()).add(node.id)
    assert _card_debug_leaf_children(state) == {
        parent_id: frozenset(ids) for parent_id, ids in expected.items()}
    # …and concretely: the crashed parent has FOUR dead children and is still a debuggable leaf,
    # while node 1 keeps its live children.
    assert set(state.nodes) == {1, 2, 3, 4, 5, 6, 7}
    assert _card_debug_leaf_children(state) == {1: frozenset({2, 7})}


def test_an_alias_node_is_in_the_canonical_cards_own_work_items_and_is_closed_anyway():
    """Pin the TRUE reason a merged chain stays shut, because 5620d11f's commit message named the
    wrong one and the code comment repeated it.

    `_derive_cards` keys `own_work_items_by_card` by `_canon(node.idea.card_id)`, so an alias's node
    IS in the canonical Card's own-work-item set — the node-row intersection does NOT stop a
    `card_merged` alias's node from reaching the debug exemption. What keeps the chain closed is the
    blocker pair a merge earns unconditionally: `merged_work_items` and `action_owner_ambiguous`.
    The intersection's real (narrower) protection is against evidence attached by the legacy
    STATEMENT-HASH join, where the node's own row never named this Card at all.
    """
    state = fold(_events([
        *_failed_node_2(),
        _native_operator_card_added("debug-canon", "repair the failed candidate", "debug", [2]),
        _native_operator_card_added("debug-alias", "repair it another way", "debug", [2]),
        # the ALIAS authors the work item…
        *_speculative_build(3, "debug-alias", "debug", [2]),
        ("card_merged", {"canonical": "debug-canon", "aliases": ["debug-alias"]}),
    ]))

    canonical = state.cards["debug-canon"]
    assert state.nodes[3].idea.card_id == "debug-alias"
    assert 3 in canonical.evidence, "…and the merge folds it into the canonical Card's evidence"
    # the exemption input the intersection was claimed to withhold — it does not withhold it
    assert set(canonical.evidence) & {3} == {3}
    # …yet the Card is unselectable, on the two blockers that actually do the work
    assert canonical.selection_ready is False
    assert "merged_work_items" in canonical.selection_blockers
    assert "action_owner_ambiguous" in canonical.selection_blockers
    # and the alias is NOT certified a belief — it owns a node, which is the whole launder vector
    assert canonical.belief_aliases == []


def _belief(statement: str):
    return ("hypothesis_added", {"statement": statement, "source": "researcher"})


def _r_drop_board():
    """The live shape from `runs/rubertlite-dr-unified-v6`, in six rows.

    The consolidator merges near-duplicate BELIEFS with each other (it never names a native card —
    `_maybe_merge_hypotheses` filters the board by `is_pure_belief`), and here the canonical it picks
    is a belief the Researcher LATER mints a work item for, word for word. `_card_identity_map` then
    bridges that belief's hash onto the native id — correctly, it is one claim — and every paraphrase
    arrives as an alias of a card that has never been touched.
    """
    seed = "Add R-Drop (alpha=0.5, symmetric KL between two dropout passes) on the contrastive loss."
    paraphrases = [
        "port r-drop onto the in-batch contrastive loss",
        "once the baseline lands, run r-drop on top of the contrastive loss",
    ]
    return seed, [
        *_baseline(),
        _belief(seed),
        *(_belief(text) for text in paraphrases),
        ("hypothesis_merged", {
            "canonical": hypothesis_id(seed),
            "aliases": [hypothesis_id(text) for text in paraphrases],
            "statement": seed}),
        _native_card_added("card-3", seed),
    ], [hypothesis_id(text) for text in paraphrases]


def test_consolidating_duplicate_beliefs_does_not_disable_the_work_item_they_name():
    """The defect measured on the LIVE run: the queue's only candidate, permanently unselectable.

    `card-3` was native, owned one complete action, was fresh, and had no work in flight — its
    blockers were exactly `['merged_work_items']`, earned for eight paraphrases of its OWN belief that
    a merge two hours older had folded onto its identity. Aliases never expire, so consolidating
    duplicate research beliefs killed the work item they named, and the run went serial on two H200s
    with nothing else selectable.

    Belief-ness is provable at fold time — a belief owns no node, no action and no receipt — so the
    two cases are distinguished rather than the blocker weakened.
    """
    seed, rows, paraphrase_ids = _r_drop_board()
    state = fold(_events(rows))

    card = state.cards["card-3"]
    assert card.identity.kind == "native"
    assert card.aliases == sorted(paraphrase_ids), "the paraphrases still land on the work item…"
    assert card.belief_aliases == sorted(paraphrase_ids), "…certified, every one, as pure beliefs"
    assert card.selection_blockers == [] and card.selection_ready is True
    # the belief rows are gone from the board — consolidation still consolidated
    assert not [cid for cid in state.cards if cid in paraphrase_ids]
    assert hypothesis_id(seed) not in state.cards

    # the same claim survives the model's own fail-closed validator and the public wire boundary,
    # which each re-state the rule and would otherwise strip the readiness they cannot verify.
    assert Card.model_validate(card.model_dump(mode="json")).selection_ready is True
    wire = public_cards(state.cards)["card-3"]
    assert wire["selection_ready"] is True
    assert wire["belief_aliases"] == sorted(paraphrase_ids)


def test_the_belief_certificate_is_order_tolerant_across_the_mint_merge_splice():
    """Invariant 5. The merge is position-keyed (`canon_at`), the certificate is not — it is a pure
    function of folded state — so minting the native card BEFORE the consolidation must fold to the
    identical answer, not merely to a still-selectable one."""
    seed, rows, _ = _r_drop_board()
    mint = rows[-1]
    merge = rows[-2]
    spliced = [*rows[:-2], mint, merge]

    after, before = fold(_events(rows)), fold(_events(spliced))
    assert [card.model_dump(mode="json") for _, card in sorted(after.cards.items())] == [
        card.model_dump(mode="json") for _, card in sorted(before.cards.items())]
    assert before.cards["card-3"].selection_ready is True


def test_an_alias_that_could_own_work_is_never_certified_a_belief():
    """The control the blocker exists for, on the two shapes the readiness gate does NOT otherwise
    catch — because "merged into another work item" has to keep meaning something.

    A merged-in WORK ITEM usually also pushes `action_owner_count` past one (every node linked to a
    card id records an owner for it), and that is what shuts the classic laundering case. These two
    do not, and `merged_work_items` is the only thing standing on them:

      * a THIN native `card_added` — a work-item identity of its own whose action block never landed.
        It contributes no action owner at all, so the canonical still folds to exactly one.
      * a GHOST id: suppressed as a conflicted native identity, so it materializes no card row and
        records no owner, while a real node still names it as its `idea.card_id` — which is precisely
        what puts that node in the canonical's `own_work_items_by_card` set.
    """
    thin = fold(_events([
        *_baseline(),
        _native_card_added("card-9", "try a bounded improvement"),
        ("card_added", {"id": "thin-alias", "source": "engine",
                        "statement": "a second work item whose action block never landed"}),
        ("card_merged", {"canonical": "card-9", "aliases": ["thin-alias"]}),
    ]))
    canonical = thin.cards["card-9"]
    assert canonical.aliases == ["thin-alias"] and canonical.belief_aliases == []
    assert canonical.selection_provenance.action_owner_count == 1, "no ambiguity to fall back on"
    assert canonical.selection_blockers == ["merged_work_items"]
    assert canonical.selection_ready is False
    assert public_cards(thin.cards)["card-9"]["selection_ready"] is False

    ghost = fold(_events([
        *_failed_node_2(),
        _native_operator_card_added("debug-canon", "repair the failed candidate", "debug", [2]),
        # one id, two statements: `_card_identity_map` suppresses it, so no card row and no owner row
        _native_operator_card_added("ghost-alias", "ghost one", "debug", [2]),
        _native_operator_card_added("ghost-alias", "ghost two", "debug", [2]),
        *_speculative_build(3, "ghost-alias", "debug", [2]),
        ("card_merged", {"canonical": "debug-canon", "aliases": ["ghost-alias"]}),
    ]))
    canonical = ghost.cards["debug-canon"]
    assert "ghost-alias" not in ghost.cards and ghost.nodes[3].idea.card_id == "ghost-alias"
    assert canonical.aliases == ["ghost-alias"] and canonical.belief_aliases == []
    assert canonical.selection_provenance.action_owner_count == 1
    assert "merged_work_items" in canonical.selection_blockers
    assert canonical.selection_ready is False


def test_the_alias_rule_is_one_statable_subtraction_that_fails_closed():
    """Three consumers state this rule — the fold's blocker, `Card`'s fail-closed validator and the
    public projection — so it is ONE function with a truth table rather than three re-derivations.

    The direction matters: an alias blocks unless the fold CERTIFIED it a belief. An uncertified
    alias (an older log with no certificate, a future alias kind nobody classified) keeps blocking.
    """
    from looplab.core.models import surviving_work_item_aliases

    seed = "one belief"
    def card(**kw):
        return Card(id="card-1", statement=seed, seed_statement=seed, **kw)

    assert surviving_work_item_aliases(card()) == []
    # certified beliefs are subtracted; the card's own belief spelling was never another work item
    assert surviving_work_item_aliases(
        card(aliases=["b1", "b2"], belief_aliases=["b1", "b2"])) == []
    assert surviving_work_item_aliases(card(aliases=[hypothesis_id(seed)])) == []
    # …everything else survives, including a half-certified merge and an uncertified legacy row
    assert surviving_work_item_aliases(card(aliases=["b1", "w1"], belief_aliases=["b1"])) == ["w1"]
    assert surviving_work_item_aliases(card(aliases=["w1", "w2"])) == ["w1", "w2"]
    # a certificate can only name ids this card actually absorbed
    with pytest.raises(ValueError):
        card(aliases=["b1"], belief_aliases=["b1", "never-folded-in"])


def test_the_fold_never_imports_search_which_is_why_the_predicate_lives_in_core():
    """The layering rule that decided this predicate's home, made a red test rather than a comment.

    `events` may import only `core` (CLAUDE.md → Conventions → Layering). That is the entire reason
    `node_counts_toward_card_budget` and its discard clause were moved DOWN out of
    `search/card_selection.py`: the fold has to answer "which children count" with the SAME object
    the Card lane's policy view uses, and it cannot reach into `search` to get it. Without this,
    the next person who needs the answer replay-side writes a second copy — which is exactly how
    this defect was produced, twice.
    """
    import ast

    from _source_scan import PKG, iter_trees

    events_pkg = PKG / "events"
    offenders: list[str] = []
    for path, tree in iter_trees(events_pkg):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("looplab."):
                package = node.module.split(".")[1]
            elif isinstance(node, ast.Import):
                package = next((alias.name.split(".")[1] for alias in node.names
                                if alias.name.startswith("looplab.")
                                and len(alias.name.split(".")) > 1), None)
            else:
                continue
            if package in {"search", "engine", "agents", "trust", "adapters", "serve",
                           "tools", "runtime"}:
                offenders.append(f"{path.name} imports looplab.{package}")
    assert offenders == [], (
        "events may import only core; a fold that can reach `search` will grow a second answer to "
        f"'which children count': {offenders}")


def test_a_gated_failed_PARENT_stays_a_replay_leaf_and_fails_closed_at_the_claim():
    """The asymmetry `_card_debug_leaf_children` documents, pinned so it stays deliberate.

    Only the CHILD side of the leaf test reads `node_counts_toward_card_budget`. A failed node the
    policy view itself hides (here: trust-gated) is still a debug CANDIDATE to replay, so replay is
    more permissive than the policy about the anchor. That direction cannot run away — the lane never
    proposes such a parent, so nothing re-authors a Card — and `eligible_cards` is where it closes.

    Since F5 that last clause is stronger and no longer conditional: `eligible_cards` used to recheck
    a ready debug Card against the live `debug_action`, so this case closed because that particular
    parent produced no proposal. There is no `debug_action` at all now, so it closes for every debug
    Card whatever its parent. The replay side is deliberately untouched — this is exactly the
    asymmetry the section header describes.
    """
    from looplab.events.card_ledger import _card_debuggable_leaf_ids
    from looplab.search.card_selection import _effective_policy_state

    state = fold(_events([
        *_failed_node_2(trust_gate="gate"),
        ("reward_hack_suspected", {"node_id": 2, "generation": 0,
                                   "signals": [{"signal": "critic:hardcoded_metric"}]}),
        _native_operator_card_added("debug-gated-parent", "repair the gated failure", "debug", [2]),
    ]))

    assert state.breed_excluded == {2}
    assert sorted(_effective_policy_state(state).nodes) == [1], "the policy cannot see node 2"
    # replay is the permissive one here, and says so
    assert 2 in _card_debuggable_leaf_ids(state)
    assert state.cards["debug-gated-parent"].selection_ready is True
    # …and Layer 3 is where it fails closed: no live `debug_action` to match, so nothing is claimable
    assert eligible_cards(state, GreedyTree(n_seeds=1, max_nodes=8, debug_depth=1)) == []
