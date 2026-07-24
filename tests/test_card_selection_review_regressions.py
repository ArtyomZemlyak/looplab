"""Focused regressions for crash-atomic Card lanes and receipt-aware coverage scoring."""
from __future__ import annotations

import orjson
import pytest

from looplab.core.models import (
    Card,
    CardIdentityProvenance,
    CardSelectionProvenance,
    Event,
    Idea,
    Node,
    NodeStatus,
    RunState,
)
from looplab.events.eventstore import EventStore, iter_event_jsonl, iter_jsonl
from looplab.search.card_selection import card_selection_set
from looplab.search.policy import GreedyTree


def test_append_many_complete_envelope_is_invisible_and_preserves_logical_events(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    first = store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})

    batch = store.append_many([
        ("node_building", {"node_id": 0}),
        ("node_building", {"node_id": 1}),
    ], expected_last_seq=first.seq)

    physical = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    assert len(physical) == 2
    assert physical[-1]["type"] == ["__looplab_event_batch_v1__"]
    # The storage marker intentionally violates the pre-batch Event envelope. An older binary stops
    # at its complete-row corruption boundary instead of accepting one unknown event and losing both
    # nested lifecycle actions during resume.
    with pytest.raises(ValueError):
        Event(**physical[-1])
    assert physical[-1]["seq"] == batch[-1].seq
    expected_types = ["run_started", "node_building", "node_building"]
    assert [row["type"] for row in iter_event_jsonl(path)] == expected_types
    replayed = EventStore(path).read_all()
    assert [event.type for event in replayed] == expected_types
    assert [event.seq for event in replayed] == [first.seq, first.seq + 1, first.seq + 2]


def test_initial_string_batch_marker_remains_readable_after_guard_upgrade(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append_many([("node_building", {"node_id": 0})])

    physical = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    physical[-1]["type"] = "__looplab_event_batch_v1__"
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in physical))

    replayed = EventStore(path).read_all()
    assert [event.type for event in replayed] == ["run_started", "node_building"]
    assert EventStore(path).divergence is None


def test_torn_append_many_envelope_exposes_no_partial_lane_and_heals_densely(tmp_path):
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    first = store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append_many([
        ("node_building", {"node_id": 0}),
        ("node_building", {"node_id": 1}),
    ], expected_last_seq=first.seq)

    raw = path.read_bytes()
    # Simulate a crash after the first nested member was completely written but before the envelope's
    # one physical JSONL record and newline completed.
    cut = raw.index(b'"node_id":1') + len(b'"node_id":1')
    path.write_bytes(raw[:cut])

    assert [row["type"] for row in iter_event_jsonl(path)] == ["run_started"]
    recovered = EventStore(path)
    assert [event.type for event in recovered.read_all()] == ["run_started"]
    healed = recovered.append("pause", {}, expected_last_seq=first.seq)
    assert healed.seq == first.seq + 1
    assert [event.type for event in recovered.read_all()] == ["run_started", "pause"]


def test_generic_iter_jsonl_does_not_interpret_reserved_type_in_foreign_store(tmp_path):
    path = tmp_path / "chat.jsonl"
    rows = [
        {"role": "user", "type": "__looplab_event_batch_v1__", "content": "ordinary chat"},
        {"role": "assistant", "content": "still visible"},
    ]
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))

    assert list(iter_jsonl(path)) == rows


_DIGEST = "card-action:v1:" + "0" * 64


def _ready_card(card_id: str, concept: str) -> Card:
    return Card(
        id=card_id,
        statement=card_id,
        seed_statement=card_id,
        source="engine",
        status="proposed",
        verdict="open",
        identity=CardIdentityProvenance(
            kind="native",
            source="card_added_receipt",
            durable=True,
            receipt_valid=True,
            action_digest=_DIGEST,
        ),
        selection_provenance=CardSelectionProvenance(
            action_source="card_added",
            action_owner_count=1,
            action_complete=True,
            freshness="current",
            owner_state="none",
        ),
        selection_blockers=[],
        selection_ready=True,
        operator="improve",
        parent_id=0,
        parent_ids=[0],
        parent_generations={"0": 0},
        scored_against=0,
        scored_against_generation=0,
        concept_tags=[concept],
    )


@pytest.mark.parametrize(
    ("memberships", "provenance", "receipts", "expected"),
    [
        ({0: ["seen"]}, {0: "classifier"}, {}, "z-new"),
        ({0: ["seen"]}, {0: "untrusted-source"}, {}, "a-seen"),
        (
            {0: ["seen"]},
            {0: "classifier"},
            {0: {"status": "partial", "reasons": ["invalid_concept_id"]}},
            "a-seen",
        ),
        ({}, {0: "researcher-authored"}, {}, "a-seen"),
    ],
)
def test_coverage_uses_only_complete_authorized_current_membership(
    memberships, provenance, receipts, expected,
):
    node = Node(
        id=0,
        parent_ids=[],
        operator="draft",
        idea=Idea(operator="draft", concepts=["seen"]),
        status=NodeStatus.evaluated,
        metric=0.5,
    )
    state = RunState(
        nodes={0: node},
        best_node_id=0,
        node_concepts=memberships,
        node_concept_provenance=provenance,
        node_concept_materialization_receipts=receipts,
        cards={
            "a-seen": _ready_card("a-seen", "seen"),
            "z-new": _ready_card("z-new", "new"),
        },
    )

    selected = card_selection_set(
        state,
        GreedyTree(n_seeds=1, max_nodes=3, debug_depth=0),
        3,
        scoring={"stance": "explore", "novelty_weight": 0.0, "coverage_weight": 1.0},
    )
    assert [card.id for card in selected] == [expected]


def test_coverage_canonicalizes_card_alias_case_and_space_with_explored_identity():
    node = Node(
        id=0,
        parent_ids=[],
        operator="draft",
        idea=Idea(operator="draft", concepts=["loss/canonical"]),
        status=NodeStatus.evaluated,
        metric=0.5,
    )
    state = RunState(
        nodes={0: node},
        best_node_id=0,
        node_concepts={0: ["loss/canonical"]},
        node_concept_provenance={0: "classifier"},
        concept_consolidation={"legacy loss": "loss/canonical"},
        cards={
            "a-alias": _ready_card("a-alias", "  LEGACY LOSS  "),
            "z-new": _ready_card("z-new", "loss/new-family"),
        },
    )

    selected = card_selection_set(
        state,
        GreedyTree(n_seeds=1, max_nodes=3, debug_depth=0),
        3,
        scoring={"stance": "explore", "novelty_weight": 0.0, "coverage_weight": 1.0},
    )
    assert [card.id for card in selected] == ["z-new"]


def test_reserved_speculative_slots_excludes_a_merged_away_alias():
    """A merged-away Card is folded OUT of state.cards (absent) and recorded only in its canonical's
    `aliases` — `Card.merged_into` is never set. `_reserved_speculative_slots` must not count such an
    absent alias as an outstanding reservation FOREVER (which monotonically starves speculative
    capacity); it excludes it via alias membership, matching the sibling counterfactual path."""
    from looplab.search.card_selection import _reserved_speculative_slots

    canonical = _ready_card("canon", "c").model_copy(update={
        "selection_ready": False,                        # avoid the RunState fresh-work-item validator
        "aliases": ["merged-alias"],                     # "merged-alias" was folded INTO canon
    })
    state = RunState(direction="min", cards={"canon": canonical})

    # canon itself has no evidence yet -> a genuine outstanding reservation (counts).
    assert _reserved_speculative_slots(state, frozenset({"canon"})) == 1
    # the merged-away alias is absent AND a known alias -> NOT outstanding (must be excluded).
    assert _reserved_speculative_slots(state, frozenset({"merged-alias"})) == 0
    # a genuinely-unknown absent id is still a reservation (fix must not over-exclude).
    assert _reserved_speculative_slots(state, frozenset({"ghost"})) == 1
    # combined: canon (1) + ghost (1) counted, merged-alias (0) excluded -> 2.
    assert _reserved_speculative_slots(
        state, frozenset({"canon", "merged-alias", "ghost"})) == 2
