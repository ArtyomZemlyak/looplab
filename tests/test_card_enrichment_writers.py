"""Focused writer contracts for the Stage-1b Card enrichment producers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.core.advisory_payloads import (
    research_claim_ref,
    research_lesson_receipt,
    research_memo_ref,
    sanitize_research_memo_payload,
    valid_advisory_ref,
)
from looplab.core.models import (
    Event, Idea, ResearchMemo, RunState, durable_idea_payload, hypothesis_id, idea_proposal_ref,
)
from looplab.engine.orchestrator import Engine
from looplab.engine.proposal_cues import ProposalCuesMixin, normalize_steering_context
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.serve.public_cards import public_cards_projection


def test_card_writer_accepts_only_bounded_structured_steering_refs():
    digest_ref = "sha256:" + "a" * 64
    safe = [
        {"kind": "complexity", "siblings": 3, "level": "moderate"},
        {"kind": "cross_run_advisory", "ref": digest_ref, "status": "available"},
        {"kind": "failure_reflection", "node_ids": [7, 7, 9]},
        {"kind": "strategy", "novelty_stance": "explore", "fidelity": "cheap"},
    ]
    expected = [
        {"kind": "complexity", "level": "moderate", "siblings": 3},
        {"kind": "cross_run_advisory", "ref": digest_ref, "status": "available"},
        {"kind": "failure_reflection", "node_ids": [7, 9]},
        {"kind": "strategy", "fidelity": "cheap", "novelty_stance": "explore"},
    ]
    assert normalize_steering_context(safe) == expected

    secret = "Authorization: Bearer writer-must-never-persist-this"
    unsafe = (
        [{"kind": "strategy", "note": secret}],
        [{"kind": "cross_run_advisory", "ref": "memo body, not a digest ref"}],
        [{"kind": "cross_run_advisory", "ref": "memo:sha256:" + "b" * 64}],
        [{"kind": "research_memo", "ref": digest_ref}],
        [{"kind": "cross_run_advisory", "ref": digest_ref, "body": secret}],
        [{"kind": "failure_reflection", "node_ids": [-1]}],
        [secret],
        {"kind": "strategy"},
    )
    assert all(normalize_steering_context(value) is None for value in unsafe)

    idea = Idea(operator="draft", hypothesis="test a bounded structured cue", card_id="card-1")
    action = Engine._card_action(
        idea, [], {}, None, None, scored_against_empty=True,
    )
    payload = Engine._card_added_payload(
        "card-1", idea.hypothesis, action, idea,
        source="researcher", at_node=0, steering_context=safe,
    )
    assert payload["steering_context"] == expected
    assert secret not in repr(payload)

    with pytest.raises(ValueError, match="bounded native card receipt"):
        Engine._card_added_payload(
            "card-1", idea.hypothesis, action, idea,
            source="researcher", at_node=0,
            steering_context=[{"kind": "strategy", "note": secret}],
        )

    public = public_cards_projection({
        "card-1": {
            "id": "card-1", "status": "proposed", "verdict": "open",
            "actionable": True, "statement": idea.hypothesis,
            "steering_context": expected,
        },
    }).model_dump(mode="json")
    assert public["cards"]["card-1"]["steering_context"] == expected
    receipt = public["cards_projection"]["items"]["card-1"]
    assert receipt["complete"] is True
    assert "steering_context" not in receipt["omissions"]


def test_card_enriched_rejects_body_shaped_values_at_the_fold_boundary():
    body = "benign memo prose must remain in its source ledger"
    state = fold([
        Event(type="run_started", data={
            "run_id": "r", "task_id": "t", "goal": "g", "direction": "max",
        }),
        Event(type="card_added", data={
            "id": "card-1", "statement": "safe card", "source": "researcher",
        }),
        Event(type="card_enriched", data={
            "id": "card-1",
            "research_origin": {"memo_body": body},
            "steering_context": [{"kind": "strategy", "note": body}],
            "cross_run_prior": {"matched_concepts": ["axis/model"], "body": body},
        }),
    ])
    card = state.cards["card-1"]
    assert card.research_origin is None
    assert card.steering_context == []
    assert "body" not in (card.cross_run_prior or {})


def test_live_card_ranked_maps_hypothesis_hashes_to_native_cards_and_clears_confidence(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "max",
    })
    alpha = "alpha direction"
    beta = "beta direction"
    # Two immutable work items may test one research direction. The producer must retain both,
    # ordered by native id, rather than collapsing them back to the statement-hash aggregate.
    store.append("card_added", {"id": "card-2", "statement": alpha, "source": "researcher"})
    store.append("card_added", {"id": "card-1", "statement": alpha, "source": "researcher"})
    store.append("card_added", {"id": "card-3", "statement": beta, "source": "researcher"})

    class _Researcher:
        last_hyp_priority = {
            "order": [hypothesis_id(alpha), hypothesis_id(beta)],
            "confidence": 0.75,
            "reason": "free-form analysis remains on hypothesis_ranked only",
        }

    engine = Engine.__new__(Engine)
    engine.store = store
    engine.researcher = _Researcher()
    engine._emit_hypothesis_ranked(7, generation=0)

    emitted = store.read_all()
    hypothesis_event = [event for event in emitted if event.type == "hypothesis_ranked"][-1]
    card_event = [event for event in emitted if event.type == "card_ranked"][-1]
    assert hypothesis_event.data["reason"] == "free-form analysis remains on hypothesis_ranked only"
    assert card_event.data == {
        "order": ["card-1", "card-2", "card-3"],
        "at_node": 7,
        "confidence": 0.75,
    }

    ranked = fold(emitted)
    assert [ranked.cards[card_id].priority for card_id in ("card-1", "card-2", "card-3")] == [0, 1, 2]
    assert all(ranked.cards[card_id].confidence == 0.75 for card_id in ranked.cards)
    assert all(ranked.cards[card_id].pinned is False for card_id in ranked.cards)

    # A later native snapshot owns the whole projection: omission is an explicit confidence clear,
    # and no stale rank survives on cards that disappeared from the new order.
    engine.researcher.last_hyp_priority = {"order": [hypothesis_id(beta)]}
    engine._emit_hypothesis_ranked(8, generation=1)
    reranked = fold(store.read_all())
    assert reranked.cards["card-3"].priority == 0
    assert reranked.cards["card-3"].foresight_rank == 0
    assert reranked.cards["card-1"].priority is None
    assert reranked.cards["card-2"].priority is None
    assert all(card.confidence is None for card in reranked.cards.values())
    assert all(card.pinned is False for card in reranked.cards.values())


def test_research_writer_mints_stable_bounded_memo_and_claim_refs():
    class _Store:
        def __init__(self):
            self.events = []

        def append(self, event_type, data):
            self.events.append((event_type, data))

        def read_all(self):
            return []

    engine = Engine.__new__(Engine)
    engine.store = _Store()
    engine._research_verify = False
    engine._track_hypotheses = False
    engine.deep_researcher = None
    memo = ResearchMemo(
        summary="bounded conclusion",
        claims=[{"statement": "node zero supports the direction", "node_ids": [0], "urls": []}],
        at_node=1,
        trigger="cadence",
    )

    engine._record_deep_research(memo, trigger="cadence", manual=False)
    engine._record_deep_research(memo, trigger="cadence", manual=False)
    completed = [data for event_type, data in engine.store.events
                 if event_type == "research_completed"]
    assert len(completed) == 2
    first, second = completed
    assert valid_advisory_ref(first["memo_id"], "memo")
    assert first["memo_id"] == first["memo"]["memo_id"] == second["memo_id"]
    assert valid_advisory_ref(first["memo"]["claims"][0]["claim_id"], "claim")
    assert first["memo"]["claims"][0]["claim_id"] == second["memo"]["claims"][0]["claim_id"]
    # The replay sanitizer retains only validated opaque ids; malformed ref-like strings fail closed.
    replayed = sanitize_research_memo_payload(first["memo"])
    assert replayed["memo_id"] == first["memo_id"]
    assert replayed["claims"][0]["claim_id"] == first["memo"]["claims"][0]["claim_id"]
    poisoned = sanitize_research_memo_payload({
        **first["memo"], "memo_id": "memo:C:/private/research.txt",
        "claims": [{**first["memo"]["claims"][0], "claim_id": "claim:full body"}],
    })
    assert "memo_id" not in poisoned and "claim_id" not in poisoned["claims"][0]


def test_latest_research_memo_is_stamped_as_a_structured_card_cue():
    memo_id = "memo:sha256:" + "a" * 64

    class _Host(ProposalCuesMixin):
        pass

    host = _Host()
    host.researcher = SimpleNamespace()
    host._complexity_cue = False
    host._budget_aware = False
    host._repo_spec = {}
    host._failure_reflection = False
    host._watchdog_reflection = False
    host._localize_faults = False
    host._feature_engineering = False
    host.task_has_columns = False
    host._assets = {}
    host._prior_note_text = ""
    host._advisory_lock = None
    host._cross_run_advisory = False
    host._cross_run_read_tools = False
    host._concept_run_base = False
    host._concept_pivot = False
    host._prefer_sweep = False
    host._novelty_stance = "balanced"
    host._strategy_fidelity = None
    host.memory_dir = ""
    host.store = SimpleNamespace(read_all=lambda: [])
    state = RunState(direction="max", research=[{"memo_id": memo_id}])

    host._set_complexity_hint(state, None)

    assert host.researcher._steering_context == [
        {"kind": "research_memo", "ref": memo_id},
    ]


def test_main_task_enrichment_writer_emits_only_exact_ref_fences(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "max",
    })
    memo = sanitize_research_memo_payload({
        "summary": "timeline-only memo body",
        "claims": [{"statement": "node zero supports it", "node_ids": [0], "urls": []}],
        "verification": {
            "method": "deterministic",
            "verdicts": [{
                "statement": "node zero supports it", "verdict": "supported", "note": "",
                "evidence": {
                    "v": 1, "node_refs": [{"node_id": 0, "generation": 0}],
                    "url_identities": [], "complete": True,
                },
            }],
        },
        "at_node": 0,
        "trigger": "cadence",
    })
    memo_id = research_memo_ref(memo)
    assert memo_id is not None
    memo["memo_id"] = memo_id
    claim_id = research_claim_ref(memo_id, 0, memo["claims"][0])
    assert claim_id is not None
    memo["claims"][0]["claim_id"] = claim_id

    idea = Idea(operator="draft", hypothesis="card receives only opaque refs", card_id="card-1")
    action = Engine._card_action(idea, [], {}, None, None, scored_against_empty=True)
    store.append("card_added", Engine._card_added_payload(
        "card-1", idea.hypothesis, action, idea,
        source="researcher", at_node=0,
        steering_context=[{"kind": "research_memo", "ref": memo_id}],
    ))
    store.append("node_created", {
        "node_id": 0, "generation": 0, "operator": "draft",
        "idea": durable_idea_payload(idea),
        "research_origin": {"memo_id": memo_id, "at_node": 0, "trigger": "cadence"},
    })
    preliminary = fold(store.read_all())
    lesson = research_lesson_receipt({
        "statement": "a private lesson body", "outcome": "supported", "evidence": [0],
    }, preliminary)
    assert valid_advisory_ref(lesson.get("lesson_id"), "lesson")
    store.append("research_completed", {"memo": memo, "memo_id": memo_id, "at_node": 0})
    store.append("lessons_distilled", {
        "at_node": 1, "trigger": "cadence", "count": 1, "lessons": [lesson],
    })

    engine = Engine.__new__(Engine)
    engine.store = store
    synced = engine._sync_card_enrichments(fold(store.read_all()))
    event = [row for row in store.read_all() if row.type == "card_enriched"][-1]
    assert event.data["id"] == "card-1"
    assert event.data["node_id"] == 0 and event.data["generation"] == 0
    assert event.data["proposal_ref"] == idea_proposal_ref(idea)
    assert event.data["lesson_refs"] == [lesson["lesson_id"]]
    assert event.data["claim_refs"] == [claim_id]
    assert not ({"statement", "summary", "body", "path", "urls"} & set(event.data))
    assert synced.cards["card-1"].lesson_refs == [lesson["lesson_id"]]
    assert synced.cards["card-1"].claim_refs == [claim_id]
    assert synced.cards["card-1"].research_origin == memo_id


def test_developer_finalized_footprint_is_durable_and_idempotently_enriched(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "max",
    })
    idea = Idea(
        operator="draft", hypothesis="fit a two GPU model", card_id="card-1",
        footprint={"gpus": 2, "gpu_mem_mib": 12_000},
    )
    action = Engine._card_action(idea, [], {}, None, None, scored_against_empty=True)
    store.append("card_added", Engine._card_added_payload(
        "card-1", idea.hypothesis, action, idea,
        source="researcher", at_node=0, steering_context=[],
    ))
    store.append("node_created", {
        "node_id": 0, "generation": 0, "operator": "draft", "parent_ids": [],
        "idea": durable_idea_payload(idea), "code": "print('ok')", "files": {},
        "footprint_finalized": True,
    })

    engine = Engine.__new__(Engine)
    engine.store = store
    before = fold(store.read_all())
    assert before.nodes[0].footprint_finalized is True
    assert "footprint_finalized" not in before.nodes[0].model_dump()

    first = engine._sync_card_enrichments(before)
    enrichments = [row for row in store.read_all() if row.type == "card_enriched"]
    assert enrichments[-1].data["footprint"] == {
        "gpus": 2, "gpu_mem_mib": 12_000,
        "proposed_by": "researcher", "finalized_by": "developer",
    }
    assert first.cards["card-1"].footprint == enrichments[-1].data["footprint"]

    engine._sync_card_enrichments(first)
    assert len([row for row in store.read_all() if row.type == "card_enriched"]) == 1

    # Inline repair keeps the same lifecycle/card but may finalize a smaller held-envelope request.
    store.append("node_repaired", {
        "node_id": 0, "generation": 0, "attempt": 1, "code": "print('repaired')",
        "idea_footprint": {"gpus": 1, "gpu_mem_mib": 8_000},
        "footprint_finalized": True,
    })
    repaired = fold(store.read_all())
    assert repaired.nodes[0].idea.footprint == {"gpus": 1, "gpu_mem_mib": 8_000}
    synced = engine._sync_card_enrichments(repaired)
    assert synced.cards["card-1"].footprint["gpus"] == 1
    assert len([row for row in store.read_all() if row.type == "card_enriched"]) == 2
