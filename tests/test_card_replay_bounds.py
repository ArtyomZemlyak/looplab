"""Adversarial bounds for the internal Card replay receipts."""
from __future__ import annotations

import json

from looplab.core.models import Event
from looplab.events.replay import FoldCursor, fold


def _encoded(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def test_card_replay_journals_are_allowlisted_and_size_bounded():
    marker = "RAW-CARD-TAIL-MUST-NOT-ENTER-RUNSTATE"
    huge = ("x" * 250_000) + marker
    concepts = [f"axis/concept-{index:05d}" for index in reversed(range(5_000))]
    concepts.extend([{"invalid": huge}, huge])
    params = {f"param-{index:04d}": index for index in range(1_000)}
    space = {
        f"space-{index:04d}": [*range(100), huge]
        for index in reversed(range(400))
    }
    steering = [
        {"kind": "coverage", "note": huge, **{f"key-{i:03d}": huge for i in range(300)}}
        for _ in range(100)
    ]
    aliases = [f"alias-{index:04d}" for index in range(2_000)]
    events = [
        Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(type="card_added", data={
            "id": " card-safe ", "statement": " bounded proposal ", "source": " engine ",
            "rationale": "bounded rationale",
            "idea": {
                "operator": " improve ", "params": params, "space": space,
                "eval_profile": "smoke", "concept_tags": concepts,
                "parent_ids": [*range(10_000), huge],
                "private_action": huge,
            },
            "parent_ids": [*range(10_000), huge], "scored_against": 7,
            "footprint": {"gpus": 2, "gpu_mem_mib": 8_000, "private": huge},
            "steering_context": steering,
            "private_note": huge,
        }),
        Event(type="card_merged", data={
            "canonical": "card-safe", "aliases": aliases, "private_note": huge,
        }),
        Event(type="card_dropped", data={
            "id": "card-safe", "reason": huge, "dropped_by": {"private": huge},
            "private_note": huge,
        }),
    ]

    state = fold(events)
    added = state.cards_added[0]
    assert set(added) == {
        "id", "statement", "source", "rationale", "idea", "parent_ids",
        "scored_against", "footprint", "steering_context", "_footprint_invalid",
    }
    assert added["id"] == "card-safe" and added["statement"] == "bounded proposal"
    assert set(added["idea"]) == {
        "operator", "params", "space", "eval_profile", "concept_tags",
        "_concept_tags_overflow", "_concept_tags_invalid", "_unknown_action_fields", "parent_ids",
    }
    assert added["idea"]["_unknown_action_fields"] is True
    assert len(added["idea"]["params"]) <= 64
    assert len(added["idea"]["space"]) <= 64
    assert all(len(values) <= 64 for values in added["idea"]["space"].values())
    assert len(added["idea"]["concept_tags"]) == 64
    assert added["idea"]["_concept_tags_overflow"] is True
    assert added["idea"]["_concept_tags_invalid"] is True
    assert len(added["idea"]["parent_ids"]) == 64
    assert len(added["parent_ids"]) == 64
    assert len(added["steering_context"]) <= 64
    assert added["_footprint_invalid"] is True

    assert state.cards_merged == [{"canonical": "card-safe", "aliases": aliases[:256]}]
    assert state.cards_dropped == [{"id": "card-safe"}]
    card = state.cards["card-safe"]
    assert card.status == "dropped" and card.dropped_reason is None and card.dropped_by == "engine"
    assert card.concept_source is not None and card.concept_source.complete is False

    journals = {
        "added": state.cards_added,
        "merged": state.cards_merged,
        "dropped": state.cards_dropped,
    }
    assert len(_encoded(journals)) < 256 * 1_024
    dumped = state.model_dump(mode="json")
    encoded = _encoded(dumped)
    assert len(encoded) < 512 * 1_024
    assert marker.encode() not in encoded


def test_card_receipts_detach_from_mutable_event_payloads():
    added = Event(type="card_added", data={
        "id": "card-1", "statement": "stable",
        "idea": {"operator": "improve", "params": {"lr": 0.1}},
    })
    merged = Event(type="card_merged", data={
        "canonical": "card-1", "aliases": ["alias-1"],
    })
    dropped = Event(type="card_dropped", data={
        "id": "card-1", "reason": "duplicate", "dropped_by": "operator",
    })
    cursor = FoldCursor()
    cursor.extend([added, merged, dropped])

    # Mutating caller-owned Event objects after extend must not mutate the accumulated replay state.
    added.data["idea"]["params"]["lr"] = 99
    added.data["statement"] = "mutated"
    merged.data["aliases"][0] = "mutated-alias"
    dropped.data["reason"] = "mutated-reason"

    state = cursor.snapshot()
    assert state.cards_added[0]["statement"] == "stable"
    assert state.cards_added[0]["idea"]["params"] == {"lr": 0.1}
    assert state.cards_merged[0]["aliases"] == ["alias-1"]
    assert state.cards_dropped[0] == {
        "id": "card-1", "reason": "duplicate", "dropped_by": "operator",
    }


def test_card_receipts_fail_closed_and_prefix_replay_matches_full_fold():
    huge = "x" * 10_000
    events = [
        Event(type="card_added", data={
            "id": "card-kept", "statement": huge, "source": ["not", "text"],
            "idea": {"operator": ["bad"], "concept_tags": "not-a-list"},
        }),
        Event(type="card_added", data={"id": huge, "statement": huge, "private": huge}),
        Event(type="card_merged", data={
            "canonical": "card-kept", "aliases": [7, huge, " alias-ok ", "alias-ok"],
            "statement": huge,
        }),
        Event(type="card_dropped", data={"id": ["bad"], "reason": huge}),
        Event(type="card_dropped", data={
            "id": "alias-ok", "reason": 7, "by": " operator ", "private": huge,
        }),
    ]
    expected_receipts = [
        ([{"id": "card-kept", "idea": {
            "_concept_tags_overflow": False, "_concept_tags_invalid": True,
        }}], [], []),
        ([{"id": "card-kept", "idea": {
            "_concept_tags_overflow": False, "_concept_tags_invalid": True,
        }}], [], []),
        ([{"id": "card-kept", "idea": {
            "_concept_tags_overflow": False, "_concept_tags_invalid": True,
        }}], [{"canonical": "card-kept", "aliases": ["alias-ok"]}], []),
        ([{"id": "card-kept", "idea": {
            "_concept_tags_overflow": False, "_concept_tags_invalid": True,
        }}], [{"canonical": "card-kept", "aliases": ["alias-ok"]}], []),
        ([{"id": "card-kept", "idea": {
            "_concept_tags_overflow": False, "_concept_tags_invalid": True,
        }}], [{"canonical": "card-kept", "aliases": ["alias-ok"]}],
         [{"id": "alias-ok", "dropped_by": "operator"}]),
    ]

    cursor = FoldCursor()
    for index, event in enumerate(events, start=1):
        cursor.extend([event])
        actual = cursor.snapshot()
        expected = fold(events[:index])
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        assert (actual.cards_added, actual.cards_merged, actual.cards_dropped) == expected_receipts[index - 1]
        assert len(actual.model_dump_json().encode("utf-8")) < 128 * 1_024


def test_card_added_receipt_is_canonical_for_ordinary_mapping_order():
    params = {f"param-{index:03d}": index for index in range(300)}
    space = {f"space-{index:03d}": [index, index + 1] for index in range(300)}
    fields = [
        ("id", "card-1"),
        ("statement", "same"),
        ("source", "engine"),
        ("idea", {
            "operator": "improve",
            "params": params,
            "space": space,
        }),
    ]
    forward = Event(type="card_added", data=dict(fields))
    reverse = Event(type="card_added", data=dict(reversed(fields)))
    reverse.data["idea"] = dict(reversed(list(reverse.data["idea"].items())))
    reverse.data["idea"]["params"] = dict(reversed(list(params.items())))
    reverse.data["idea"]["space"] = dict(reversed(list(space.items())))

    left = fold([forward]).cards_added
    right = fold([reverse]).cards_added
    assert left == right
    assert _encoded(left) == _encoded(right)
    assert list(left[0]["idea"]["params"]) == [f"param-{index:03d}" for index in range(64)]
    assert list(left[0]["idea"]["space"]) == [f"space-{index:03d}" for index in range(64)]


def test_bounded_receipts_preserve_legacy_empty_profile_and_by_fallback():
    events = [
        Event(type="card_added", data={
            "id": "card-1", "statement": "same", "idea": {"eval_profile": ""},
        }),
        Event(type="node_created", data={
            "node_id": 1, "operator": "improve",
            "idea": {"operator": "improve", "hypothesis": "same", "card_id": "card-1"},
        }),
        Event(type="card_dropped", data={
            "id": "card-1", "dropped_by": "", "by": "operator",
        }),
    ]
    state = fold(events)
    card = state.cards["card-1"]
    assert state.cards_added[0]["idea"] == {"eval_profile": ""}
    assert card.eval_profile == "" and card.operator is None
    assert state.cards_dropped == [{"id": "card-1", "dropped_by": "operator"}]
    assert card.dropped_by == "operator"
