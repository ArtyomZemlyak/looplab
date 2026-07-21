"""Focused acceptance coverage for docs/23 Stage 1c Card lifecycle producers.

These tests deliberately keep the Card board advisory: they pin durable writer order,
merge-mirror idempotence, and replay exclusions without making Card state affect search.
"""
from __future__ import annotations

import threading

from looplab.core.models import Event, Idea, durable_idea_payload
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _events(rows):
    return [Event(type=event_type, data=data) for event_type, data in rows]


def _bare_engine(tmp_path) -> Engine:
    engine = Engine.__new__(Engine)
    engine.store = EventStore(tmp_path / "events.jsonl")
    engine._id_lock = threading.Lock()
    engine.store.append("run_started", {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "max",
    })
    return engine


def _native_card(card_id: str, statement: str, *, at_node: int = 0):
    idea = Idea(operator="draft", hypothesis=statement, card_id=card_id)
    action = Engine._card_action(
        idea, [], {}, None, None, scored_against_empty=True,
    )
    payload = Engine._card_added_payload(
        card_id, statement, action, idea,
        source="researcher", at_node=at_node,
    )
    return idea, payload


def test_intra_batch_duplicate_is_journaled_after_accepted_reservations(tmp_path):
    """A discarded sibling gets its own closed Card without shifting accepted ids."""
    engine = _bare_engine(tmp_path)

    class _BatchResearcher:
        _steering_context = []

        def propose_batch(self, state, n):
            return [
                Idea(operator="draft", hypothesis="same executable trial", params={"x": 1}),
                Idea(operator="draft", hypothesis="same executable trial", params={"x": 1}),
            ][:n]

        def propose(self, state, parent):
            raise AssertionError("a successful native batch must not use the fallback")

    engine.researcher = _BatchResearcher()
    engine._novelty_mode = "off"
    engine._novelty_stance = None
    # Isolate Stage 1c from unrelated prompt/novelty integrations. The real batch code still performs
    # canonical executable-action dedupe and Card preplanning around these no-op seams.
    engine._set_complexity_hint = lambda *_args, **_kwargs: None
    engine._apply_novelty_gate = lambda _state, idea, **_kwargs: idea
    engine._effective_researcher_eval_timeout = lambda idea: idea.eval_timeout

    state = fold(engine.store.read_all())
    accepted = engine._propose_batch(state, 2)
    dropped = list(engine._pending_batch_dropped)
    assert len(accepted) == 1 and len(dropped) == 1

    reservation = engine._reserve_node_build(
        {"kind": "draft"}, accepted[0], scored_against=state.best_node_id,
        source="researcher", steering_context=[],
    )
    assert reservation is not None
    rejected_id = engine._record_node_less_card(
        dropped[0]["idea"], reason=dropped[0]["reason"],
        steering_context=dropped[0]["steering_context"],
    )

    lifecycle = [
        event for event in engine.store.read_all()
        if event.type in {"card_added", "node_building", "card_dropped"}
    ]
    assert [event.type for event in lifecycle] == [
        "card_added", "node_building", "card_added", "card_dropped",
    ]
    accepted_id = lifecycle[0].data["id"]
    assert lifecycle[1].data["card_id"] == accepted_id == reservation.card_id
    assert lifecycle[2].data["id"] == lifecycle[3].data["id"] == rejected_id
    assert accepted_id != rejected_id
    assert lifecycle[3].data["reason"] == "intra_batch_duplicate"

    # Crash-prefix safety: the rejected proposal must never become executable in the gap between its
    # registration and terminal receipt. A node-less rejection needs an intrinsically non-selectable
    # registration (or an equivalent atomic terminal-at-mint proof), because there is no build marker
    # from which recovery could reconstruct the missing card_dropped event.
    all_events = engine.store.read_all()
    rejected_drop_index = next(
        index for index, event in enumerate(all_events)
        if event.type == "card_dropped" and event.data.get("id") == rejected_id
    )
    rejected_prefix = fold(all_events[:rejected_drop_index])
    assert rejected_prefix.cards[rejected_id].selection_ready is False

    projected = fold(all_events)
    assert projected.cards[accepted_id].status == "building"
    assert projected.cards[rejected_id].status == "dropped"
    assert projected.cards[rejected_id].selection_ready is False


def test_hypothesis_merge_mirror_is_main_task_idempotent_by_source_seq(tmp_path):
    engine = _bare_engine(tmp_path)
    source = engine.store.append("hypothesis_merged", {
        "canonical": "hyp-b", "aliases": ["hyp-a", "hyp-a"],
        "statement": "one canonical direction",
    })

    state = fold(engine.store.read_all())
    first = engine._mirror_hypothesis_card_merges(state)
    second = engine._mirror_hypothesis_card_merges(first)

    mirrors = [event for event in engine.store.read_all() if event.type == "card_merged"]
    assert len(mirrors) == 1
    assert mirrors[0].data == {
        "canonical": "hyp-b",
        "aliases": ["hyp-a"],
        "source_event_seq": source.seq,
        "merged_by": "engine",
        "statement": "one canonical direction",
    }
    assert second.model_dump(mode="json") == fold(engine.store.read_all()).model_dump(mode="json")


def test_true_open_gated_and_superseded_only_evidence_have_distinct_lifecycles():
    open_idea, open_payload = _native_card("card-open", "unbuilt open direction")
    gated_idea, gated_payload = _native_card("card-gated", "trust-gated direction")
    superseded_idea, superseded_payload = _native_card(
        "card-superseded", "superseded direction",
    )
    state = fold(_events([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_added", open_payload),
        ("card_added", gated_payload),
        ("card_added", superseded_payload),
        ("node_created", {
            "node_id": 0, "generation": 0, "operator": "draft", "parent_ids": [],
            "idea": durable_idea_payload(gated_idea), "code": "candidate",
        }),
        ("node_evaluated", {
            "node_id": 0, "generation": 0, "metric": 0.9,
            "violations": ["used the test set"],
        }),
        ("node_created", {
            "node_id": 1, "generation": 0, "operator": "draft", "parent_ids": [],
            "idea": durable_idea_payload(superseded_idea), "code": "candidate",
        }),
        ("node_failed", {
            "node_id": 1, "generation": 0, "reason": "superseded",
            "error": "parent lifecycle changed", "eval_seconds": 0.0,
        }),
    ]))

    open_card = state.cards[open_idea.card_id]
    gated_card = state.cards[gated_idea.card_id]
    superseded_card = state.cards[superseded_idea.card_id]
    assert (open_card.status, open_card.verdict, open_card.actionable) == (
        "proposed", "open", True,
    )
    assert open_card.selection_ready is True
    assert gated_card.evidence == [0]
    assert (gated_card.status, gated_card.verdict, gated_card.actionable) == (
        "gated", "open", False,
    )
    assert gated_card.selection_ready is False
    assert "card_terminal" in gated_card.selection_blockers
    # A superseded terminal remains historically evaluated/actionable on the compatibility board. It
    # is nevertheless closed to strict queue selection by its terminal work owner.
    assert superseded_card.evidence == [1]
    assert (superseded_card.status, superseded_card.verdict, superseded_card.actionable) == (
        "evaluated", "open", True,
    )
    assert superseded_card.selection_ready is False
    assert "work_terminal" in superseded_card.selection_blockers


def test_drop_and_merge_exclusions_are_order_tolerant_and_preserve_legacy_actionable():
    _alias_idea, alias_payload = _native_card("card-alias", "alias direction")
    _canonical_idea, canonical_payload = _native_card("card-canonical", "canonical direction")
    prefix = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
    registrations = [
        ("card_added", alias_payload),
        ("card_added", canonical_payload),
    ]
    merge = [("card_merged", {
        "canonical": "card-canonical", "aliases": ["card-alias"],
    })]

    before = fold(_events(prefix + merge + registrations))
    after = fold(_events(prefix + registrations + merge))
    for state in (before, after):
        assert set(state.cards) == {"card-canonical"}
        card = state.cards["card-canonical"]
        assert card.aliases == ["card-alias"]
        # Compatibility `actionable` means only "not administratively dead"; a merged work item is
        # deliberately excluded by the strict queue contract without changing that legacy scalar.
        assert card.actionable is True
        assert card.selection_ready is False
        assert "merged_work_items" in card.selection_blockers

    drop = [("card_dropped", {
        "id": "card-alias", "reason": "superseded duplicate", "dropped_by": "engine",
    })]
    dropped_before = fold(_events(prefix + drop + merge + registrations))
    dropped_after = fold(_events(prefix + registrations + merge + drop))
    for state in (dropped_before, dropped_after):
        card = state.cards["card-canonical"]
        assert card.status == "dropped"
        assert card.dropped_reason == "superseded duplicate"
        assert card.actionable is False
        assert card.selection_ready is False
        assert "card_terminal" in card.selection_blockers
