"""Writer-side Card identity and node-build lifecycle.

These tests deliberately assert the durable log/fold contract instead of the private shape of a
build reservation.  A native card is one engine-minted action receipt, linked first to the transient
``node_building`` marker and then to the persisted ``Idea`` on ``node_created``.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, card_ownership_receipt, idea_proposal_ref
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.types import (
    EV_CARD_ADDED,
    EV_CARD_DROPPED,
    EV_CARD_MERGED,
    EV_NODE_BUILDING,
    EV_NODE_CREATED,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_NODE_RESET,
    EV_NOVELTY_GRADED,
    EV_NOVELTY_REJECTED,
)
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


class _FixedResearcher:
    def __init__(self, idea: Idea):
        self.idea = idea
        self.calls = 0

    def propose(self, state, parent):
        self.calls += 1
        return self.idea.model_copy(deep=True)


class _Developer:
    def __init__(self):
        self.calls = 0

    def implement(self, idea):
        self.calls += 1
        return f"print({self.calls})"


class _MutatingDeveloper:
    """A hostile plugin that mutates every receipt-bearing field it receives."""

    def implement(self, idea):
        idea.hypothesis = "developer replaced the research claim"
        idea.rationale = "developer replaced the rationale"
        idea.params["x"] = 999.0
        idea.eval_timeout = 999.0
        idea.card_id = "developer-forged-card"
        return "print('mutated working copy')"


class _RaisingDeveloper:
    def implement(self, idea):
        raise RuntimeError("developer exploded")


class _NativeBatchResearcher:
    def __init__(self, ideas: list[Idea]):
        self.ideas = ideas

    def propose_batch(self, state, n):
        return [idea.model_copy(deep=True) for idea in self.ideas[:n]]

    def propose(self, state, parent):
        # The native batch should retain the valid sibling, so fallback is not expected.
        return self.ideas[-1].model_copy(deep=True)


def _engine(run_dir, *, idea: Idea | None = None, developer=None) -> Engine:
    task = ToyTask.load(TASK_FILE)
    researcher, default_developer = task.build_roles()
    engine = Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=default_developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=3, max_nodes=8),
        n_seeds=3,
        max_nodes=8,
    )
    # These tests isolate writer identity from the advisory novelty subsystem.
    engine._novelty_mode = "off"
    if idea is not None:
        engine.researcher = _FixedResearcher(idea)
    if developer is not None:
        engine.developer = developer
    return engine


def _start(engine: Engine) -> None:
    engine.store.append(
        "run_started",
        {"run_id": "card-writer", "task_id": "toy", "goal": "g", "direction": "min"},
    )


def _idea(label: str, x: float, *, card_id: str | None = None) -> Idea:
    return Idea(
        operator="draft",
        params={"x": x, "y": -1.0},
        rationale=f"build {label}",
        hypothesis=f"{label} improves the objective",
        card_id=card_id,
    )


def _receipt_action(data: dict) -> dict:
    """Reconstruct exactly the v1 action owned by a thin ``card_added`` envelope."""
    idea = data.get("idea") if isinstance(data.get("idea"), dict) else data
    return {
        "operator": idea.get("operator"),
        "params": idea.get("params"),
        "space": idea.get("space"),
        "eval_profile": idea.get("eval_profile"),
        "eval_timeout": idea.get("eval_timeout"),
        "parent_id": data.get("parent_id", idea.get("parent_id")),
        "parent_ids": data.get("parent_ids", idea.get("parent_ids", [])),
        "parent_generations": data.get("parent_generations"),
        "scored_against": data.get("scored_against"),
        "scored_against_generation": data.get("scored_against_generation"),
        "scored_against_empty": data.get("scored_against_empty"),
        "footprint": data.get("footprint"),
    }


def _native_card_payload(
    card_id: str,
    idea: Idea,
    *,
    scored_against=None,
    action: dict | None = None,
    source: str = "researcher",
    at_node: int = 0,
) -> dict:
    """Build the exact writer prefix used to exercise crash recovery."""
    rebound = idea.model_copy(deep=True, update={"card_id": card_id})
    statement = Engine._card_statement(rebound)
    assert statement is not None
    if action is None:
        action = {
            "operator": rebound.operator,
            "params": dict(rebound.params),
            "space": {key: list(values) for key, values in rebound.space.items()},
            "eval_profile": rebound.eval_profile,
            "eval_timeout": rebound.eval_timeout,
            "parent_id": None,
            "parent_ids": [],
            "parent_generations": {},
            "scored_against": scored_against,
            "scored_against_generation": 0 if scored_against is not None else None,
            "scored_against_empty": scored_against is None,
            "footprint": rebound.footprint,
        }
    return Engine._card_added_payload(
        card_id, statement, action, rebound, source=source, at_node=at_node,
    )


def test_serial_create_mints_one_exact_native_card_before_build_and_persists_link(tmp_path):
    # An arbitrary Researcher-supplied id is not authority to mint in the engine namespace.
    proposed = _idea("serial direction", 0.25, card_id="caller-chosen-id")
    developer = _Developer()
    engine = _engine(tmp_path / "serial", idea=proposed, developer=developer)
    _start(engine)

    engine._create_node({"kind": "draft"})

    events = engine.store.read_all()
    added = [event for event in events if event.type == EV_CARD_ADDED]
    buildings = [event for event in events if event.type == EV_NODE_BUILDING]
    created = [event for event in events if event.type == EV_NODE_CREATED]
    assert len(added) == len(buildings) == len(created) == 1

    card_event, building, node_event = added[0], buildings[0], created[0]
    card_id = card_event.data["id"]
    assert card_id == "card-0" and card_id != proposed.card_id
    assert events.index(card_event) < events.index(building) < events.index(node_event)
    assert building.data["card_id"] == node_event.data["idea"]["card_id"] == card_id

    expected = card_ownership_receipt(
        card_id, card_event.data["statement"], _receipt_action(card_event.data))
    assert expected is not None and card_event.data["ownership_receipt"] == expected

    # The prefix alone is enough to prove native ownership and the exact in-flight link.
    building_state = fold(events[:events.index(building) + 1])
    in_flight = building_state.cards[card_id]
    assert in_flight.identity.action_digest == expected["action_digest"]
    assert in_flight.selection_provenance.action_source == "card_added"
    assert in_flight.selection_provenance.action_owner_count == 1
    assert in_flight.selection_provenance.action_complete is True
    assert in_flight.selection_provenance.owner_state == "in_flight"
    assert "work_in_flight" in in_flight.selection_blockers
    assert in_flight.selection_ready is False

    persisted = fold(events)
    node = persisted.nodes[node_event.data["node_id"]]
    assert node.idea.card_id == card_id and node.idea.hypothesis == proposed.hypothesis
    assert persisted.cards[card_id].evidence == [node.id]
    assert persisted.cards[card_id].selection_provenance.owner_state == "in_flight"

    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": node.id, "generation": 0, "metric": 1.0, "eval_seconds": 0.01,
    })
    terminal = fold(engine.store.read_all()).cards[card_id]
    assert terminal.selection_provenance.action_complete is True
    assert terminal.selection_provenance.owner_state == "terminal"
    assert "work_terminal" in terminal.selection_blockers
    assert terminal.selection_ready is False


def test_depth_zero_card_registration_does_not_gain_layer5_cross_run_receipt(tmp_path):
    proposed = _idea("legacy receipt identity", 0.375)
    developer = _Developer()
    engine = _engine(tmp_path / "depth-zero-receipt", developer=developer)
    receipt = {
        "v": 2,
        "status": "unavailable",
        "complete": False,
        "governance": {
            "v": 1,
            "status": "unavailable",
            "complete": False,
            "code": "governance_ledger_unavailable",
            "ledger": "concept_aliases",
            "reason": "torn_tail",
        },
    }
    engine.researcher._cross_run_advisory_receipt = receipt
    _start(engine)

    engine._create_node({"kind": "draft"}, preproposed=proposed)

    added = next(event for event in engine.store.read_all() if event.type == EV_CARD_ADDED)
    created = next(event for event in engine.store.read_all() if event.type == EV_NODE_CREATED)
    assert engine.speculation_depth == 0
    assert "cross_run_receipt" not in added.data
    # Existing node telemetry remains unchanged; only Layer-5 staged Card inventory owns the new
    # durable registration receipt used to reconstruct speculative provenance after restart.
    assert created.data["cross_run_receipt"] == receipt


def test_card_allocator_uses_only_gap_safe_numeric_card_added_ceiling(tmp_path):
    engine = _engine(tmp_path / "ceiling", developer=_Developer())
    _start(engine)
    for card_id, idea in (
        ("card-2", _idea("old two", 2.0)),
        ("card-7", _idea("old seven", 7.0)),
        ("opaque-native", _idea("old opaque", 11.0)),
    ):
        engine.store.append(EV_CARD_ADDED, _native_card_payload(card_id, idea))

    # Even a malformed/unreceipted row reserves its canonical numeric spelling in the raw journal.
    # The allocator must trim exactly like replay before advancing the monotonic ceiling.
    engine.store.append(EV_CARD_ADDED, {
        "id": " card-8 ", "statement": "whitespace reservation", "source": "legacy",
    })

    # High card-shaped ids outside card_added are read-model links, never allocator authority.
    engine.store.append(EV_NODE_CREATED, {
        "node_id": 0,
        "parent_ids": [],
        "operator": "draft",
        "idea": {
            "operator": "draft", "params": {"x": 99.0},
            "hypothesis": "node-only identity", "card_id": "card-99",
        },
        "code": "print(99)",
    })
    # A node-only id does not move the ceiling, but an exact collision with the next candidate must be
    # skipped so legacy evidence is never silently joined to a newly-native action.
    engine.store.append(EV_NODE_CREATED, {
        "node_id": 1,
        "parent_ids": [],
        "operator": "draft",
        "idea": {
            "operator": "draft", "params": {"x": 9.0},
            "hypothesis": "exact next-id collision", "card_id": "card-9",
        },
        "code": "print(9)",
    })
    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 4, "operator": "draft", "parent_ids": [], "card_id": "card-100",
    })

    engine._create_node({"kind": "draft"}, preproposed=_idea("new eight", 8.0))
    engine._create_node({"kind": "draft"}, preproposed=_idea("new nine", 9.0))

    ids = [event.data["id"] for event in engine.store.read_all()
           if event.type == EV_CARD_ADDED]
    assert ids[-2:] == ["card-10", "card-11"]


def test_exact_orphan_card_prefix_is_reused_after_crash_without_reregistering(tmp_path):
    run_dir = tmp_path / "resume-prefix"
    idea = _idea("durable orphan", 3.5)
    before_crash = _engine(run_dir)
    _start(before_crash)
    before_crash.store.append(EV_CARD_ADDED, _native_card_payload("card-7", idea))

    # A fresh Engine sees the durable mint but no build marker: the process died between the two.
    resumed = _engine(run_dir, developer=_Developer())
    resumed._create_node({"kind": "draft"}, preproposed=idea)

    events = resumed.store.read_all()
    added = [event for event in events if event.type == EV_CARD_ADDED]
    buildings = [event for event in events if event.type == EV_NODE_BUILDING]
    created = [event for event in events if event.type == EV_NODE_CREATED]
    assert [event.data["id"] for event in added] == ["card-7"]
    assert len(buildings) == len(created) == 1
    assert buildings[0].data["card_id"] == "card-7"
    assert created[0].data["idea"]["card_id"] == "card-7"


def test_orphan_reuse_binds_eval_timeout_as_part_of_the_exact_action(tmp_path):
    run_dir = tmp_path / "timeout-identity"
    original = _idea("timeout-bound", 3.5)
    original.eval_timeout = 10.0
    before_crash = _engine(run_dir)
    _start(before_crash)
    before_crash.store.append(EV_CARD_ADDED, _native_card_payload("card-7", original))

    changed = original.model_copy(deep=True, update={"eval_timeout": 11.0})
    resumed = _engine(run_dir, developer=_Developer())
    resumed._create_node({"kind": "draft"}, preproposed=changed)

    added = [event for event in resumed.store.read_all() if event.type == EV_CARD_ADDED]
    assert [event.data["id"] for event in added] == ["card-7", "card-8"]
    assert [event.data["idea"]["eval_timeout"] for event in added] == [10.0, 11.0]
    assert (added[0].data["ownership_receipt"]["action_digest"]
            != added[1].data["ownership_receipt"]["action_digest"])
    created = next(event for event in resumed.store.read_all()
                   if event.type == EV_NODE_CREATED)
    assert created.data["idea"]["card_id"] == "card-8"


def test_orphan_reuse_requires_the_same_writer_source(tmp_path):
    run_dir = tmp_path / "source-identity"
    idea = _idea("same action different authority", 3.5)
    before_crash = _engine(run_dir)
    _start(before_crash)
    before_crash.store.append(
        EV_CARD_ADDED,
        _native_card_payload("card-7", idea, source="operator"),
    )

    resumed = _engine(run_dir, developer=_Developer())
    resumed._create_node({"kind": "draft"}, preproposed=idea)

    added = [event for event in resumed.store.read_all() if event.type == EV_CARD_ADDED]
    assert [event.data["id"] for event in added] == ["card-7", "card-8"]
    assert [event.data["source"] for event in added] == ["operator", "researcher"]
    created = next(event for event in resumed.store.read_all()
                   if event.type == EV_NODE_CREATED)
    assert created.data["idea"]["card_id"] == "card-8"


def test_batch_prereservations_mint_on_main_thread_and_dedupe_exact_active_work(
        tmp_path, monkeypatch):
    engine = _engine(tmp_path / "batch")
    _start(engine)
    ideas = [_idea(f"parallel direction {index}", float(index)) for index in range(3)]
    main_thread = threading.get_ident()
    append_threads: list[tuple[str, int]] = []
    original_append = engine.store.append

    def _recording_append(event_type, data, **kwargs):
        append_threads.append((event_type, threading.get_ident()))
        return original_append(event_type, data, **kwargs)

    monkeypatch.setattr(engine.store, "append", _recording_append)

    # Production does this serially before fan-out: monotonic card events are never worker-written.
    reservations = [
        engine._reserve_node_build({"kind": "draft"}, idea=idea)
        for idea in ideas
    ]
    assert all(reservation is not None for reservation in reservations)

    events_before_duplicate = engine.store.read_all()
    duplicate = engine._reserve_node_build(
        {"kind": "draft"}, idea=ideas[0].model_copy(deep=True))
    assert duplicate is None
    assert engine.store.read_all() == events_before_duplicate

    errors: list[BaseException] = []

    def _build(reservation, idea):
        try:
            engine._create_node(
                {"kind": "draft"},
                roles=(_FixedResearcher(idea), _Developer()),
                reserved=reservation,
                preproposed=idea,
            )
        except BaseException as exc:  # surface worker failures in the test thread
            errors.append(exc)

    threads = [
        threading.Thread(target=_build, args=(reservation, idea))
        for reservation, idea in zip(reservations, ideas)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

    events = engine.store.read_all()
    added = [event for event in events if event.type == EV_CARD_ADDED]
    buildings = [event for event in events if event.type == EV_NODE_BUILDING]
    created = [event for event in events if event.type == EV_NODE_CREATED]
    assert [event.data["id"] for event in added] == ["card-0", "card-1", "card-2"]
    assert [event.data["card_id"] for event in buildings] == ["card-0", "card-1", "card-2"]
    assert {event.data["idea"]["card_id"] for event in created} == {
        "card-0", "card-1", "card-2",
    }
    registration_pairs = [
        (event.type, event.data.get("id") or event.data.get("card_id"))
        for event in events if event.type in {EV_CARD_ADDED, EV_NODE_BUILDING}
    ]
    assert registration_pairs == [
        (EV_CARD_ADDED, "card-0"), (EV_NODE_BUILDING, "card-0"),
        (EV_CARD_ADDED, "card-1"), (EV_NODE_BUILDING, "card-1"),
        (EV_CARD_ADDED, "card-2"), (EV_NODE_BUILDING, "card-2"),
    ]
    card_writes = [thread_id for event_type, thread_id in append_threads
                   if event_type in {EV_CARD_ADDED, EV_NODE_BUILDING}]
    assert card_writes and set(card_writes) == {main_thread}


def test_invalid_long_and_oversized_batch_ideas_do_not_strand_a_valid_sibling(tmp_path):
    long_statement = _idea("placeholder", 1.0)
    long_statement.hypothesis = "h" * 2_049
    oversized_identity = _idea("bounded statement", 2.0)
    oversized_identity.rationale = "r" * 66_000
    valid = _idea("surviving sibling", 3.0)

    engine = _engine(tmp_path / "bounded-batch", developer=_Developer())
    engine.researcher = _NativeBatchResearcher(
        [long_statement, oversized_identity, valid])
    _start(engine)

    ideas = engine._propose_batch(fold(engine.store.read_all()), 3)

    assert len(ideas) == 1
    assert ideas[0].hypothesis == valid.hypothesis
    assert ideas[0].card_id == "card-0"
    proposal_events = engine.store.read_all()
    rejects = [event for event in proposal_events if event.type == EV_NOVELTY_REJECTED]
    assert len(rejects) == 2
    assert all(event.data.get("kind") == "card_contract" for event in rejects)
    assert not any(event.type in {EV_CARD_ADDED, EV_NODE_BUILDING}
                   for event in proposal_events)

    reservation = engine._reserve_node_build({"kind": "draft"}, idea=ideas[0])
    assert reservation is not None
    engine._create_node(
        {"kind": "draft"}, reserved=reservation, preproposed=ideas[0])

    events = engine.store.read_all()
    assert sum(event.type == EV_CARD_ADDED for event in events) == 1
    assert sum(event.type == EV_NODE_BUILDING for event in events) == 1
    assert sum(event.type == EV_NODE_CREATED for event in events) == 1
    state = fold(events)
    assert state.buildings == {}
    assert state.nodes[0].idea.card_id == "card-0"


def test_developer_mutation_cannot_change_the_receipt_bound_persisted_idea(tmp_path):
    proposed = _idea("immutable research claim", 0.125)
    proposed.eval_timeout = 23.0
    engine = _engine(
        tmp_path / "mutating-developer",
        idea=proposed,
        developer=_MutatingDeveloper(),
    )
    _start(engine)

    engine._create_node({"kind": "draft"})

    events = engine.store.read_all()
    card_event = next(event for event in events if event.type == EV_CARD_ADDED)
    node_event = next(event for event in events if event.type == EV_NODE_CREATED)
    persisted = fold(events).nodes[node_event.data["node_id"]].idea
    assert persisted.hypothesis == proposed.hypothesis
    assert persisted.rationale == proposed.rationale
    assert persisted.params == proposed.params
    assert persisted.eval_timeout == proposed.eval_timeout
    assert persisted.card_id == card_event.data["id"] == "card-0"
    assert card_event.data["proposal_ref"] == idea_proposal_ref(persisted)
    assert card_event.data["idea"]["eval_timeout"] == proposed.eval_timeout


def test_injected_developer_exception_drops_card_before_clearing_build(tmp_path):
    engine = _engine(tmp_path / "inject-crash", developer=_RaisingDeveloper())
    _start(engine)

    with pytest.raises(RuntimeError, match="developer exploded"):
        engine._create_injected_node({
            "idea": {
                "operator": "manual",
                "params": {"x": 4.0},
                "rationale": "operator supplied direction",
                "hypothesis": "manual direction should remain auditable",
            },
        })

    events = engine.store.read_all()
    drop = next(event for event in events if event.type == EV_CARD_DROPPED)
    failed = next(event for event in events if event.type == EV_NODE_FAILED)
    assert drop.data["id"] == failed.data["card_id"] == "card-0"
    assert events.index(drop) < events.index(failed)
    state = fold(events)
    assert state.buildings == {}
    assert state.nodes == {}
    assert state.cards["card-0"].status == "dropped"
    assert state.cards["card-0"].selection_ready is False


def test_rerun_preserves_native_card_id_without_a_second_registration(tmp_path):
    engine = _engine(tmp_path / "rerun", developer=_Developer())
    _start(engine)
    engine._create_node({"kind": "draft"}, preproposed=_idea("rerun direction", 4.0))
    first = fold(engine.store.read_all())
    node = next(iter(first.nodes.values()))
    card_id = node.idea.card_id
    assert card_id is not None
    registrations_before = [event for event in engine.store.read_all()
                            if event.type == EV_CARD_ADDED]

    engine.store.append(EV_NODE_FAILED, {
        "node_id": node.id, "generation": 0, "error": "boom", "reason": "crash",
        "eval_seconds": 0.0,
    })
    engine.store.append(EV_NODE_RESET, {
        "node_id": node.id, "generation": 0, "from_stage": "implement",
    })
    reset = fold(engine.store.read_all())
    engine._rerun_node(reset.nodes[node.id], reset)

    events = engine.store.read_all()
    registrations_after = [event for event in events if event.type == EV_CARD_ADDED]
    rerun_building = [event for event in events
                      if event.type == EV_NODE_BUILDING and event.data.get("generation") == 1]
    creates = [event for event in events
               if event.type == EV_NODE_CREATED and event.data.get("node_id") == node.id]
    assert registrations_after == registrations_before
    assert len(rerun_building) == 1 and rerun_building[0].data["card_id"] == card_id
    assert len(creates) == 2 and creates[-1].data["idea"]["card_id"] == card_id

    final_card = fold(events).cards[card_id]
    assert final_card.identity.kind == "native"
    assert final_card.selection_provenance.action_owner_count == 1
    assert final_card.selection_provenance.action_complete is True


def test_propose_reset_replaces_card_on_same_node_and_drops_old_work_item(tmp_path):
    engine = _engine(tmp_path / "repropose", developer=_Developer())
    _start(engine)
    engine._create_node({"kind": "draft"}, preproposed=_idea("first claim", 1.0))
    initial = fold(engine.store.read_all())
    node = initial.nodes[0]
    old_card_id = node.idea.card_id
    assert old_card_id == "card-0"

    engine.store.append(EV_NODE_FAILED, {
        "node_id": node.id,
        "generation": 0,
        "error": "replace the proposal",
        "reason": "operator",
        "eval_seconds": 0.0,
    })
    engine.store.append(EV_NODE_RESET, {
        "node_id": node.id, "generation": 0, "from_stage": "propose",
    })
    replacement = _idea("replacement claim", 2.0, card_id=old_card_id)
    engine.researcher = _FixedResearcher(replacement)
    reset = fold(engine.store.read_all())

    engine._rerun_node(reset.nodes[node.id], reset)

    events = engine.store.read_all()
    registrations = [event for event in events if event.type == EV_CARD_ADDED]
    assert [event.data["id"] for event in registrations] == ["card-0", "card-1"]
    drops = [event for event in events if event.type == EV_CARD_DROPPED]
    assert len(drops) == 1
    assert drops[0].data["id"] == old_card_id
    assert drops[0].data["reason"] == "reproposed"
    replacement_build = next(
        event for event in events
        if event.type == EV_NODE_BUILDING and event.data.get("generation") == 1)
    replacement_create = [
        event for event in events
        if event.type == EV_NODE_CREATED and event.data.get("node_id") == node.id
    ][-1]
    assert replacement_build.data["card_id"] == "card-1"
    assert replacement_create.data["idea"]["card_id"] == "card-1"
    assert replacement_create.data["idea"]["hypothesis"] == replacement.hypothesis

    final = fold(events)
    assert final.nodes[node.id].attempt == 1
    assert final.nodes[node.id].idea.card_id == "card-1"
    assert final.cards[old_card_id].status == "dropped"
    assert final.cards[old_card_id].selection_ready is False
    assert final.cards["card-1"].evidence == [node.id]


def test_drop_first_crash_prefix_is_fail_closed_and_recovery_is_idempotent(
        tmp_path, monkeypatch):
    engine = _engine(tmp_path / "drop-first", developer=_Developer())
    _start(engine)
    reservation = engine._reserve_node_build(
        {"kind": "draft"}, idea=_idea("crash between terminals", 5.0))
    assert reservation is not None and reservation.card_id == "card-0"

    original_append = engine.store.append

    def _crash_before_node_terminal(event_type, data, **kwargs):
        if event_type == EV_NODE_FAILED:
            raise RuntimeError("process died after card_dropped")
        return original_append(event_type, data, **kwargs)

    monkeypatch.setattr(engine.store, "append", _crash_before_node_terminal)
    with pytest.raises(RuntimeError, match="process died"):
        engine._fail_reserved_build(
            node_id=reservation.node_id,
            card_id=reservation.card_id,
            generation=0,
            reason="build_crash",
            error="developer raised",
        )

    prefix = engine.store.read_all()
    prefix_state = fold(prefix)
    assert reservation.node_id in prefix_state.buildings
    assert prefix_state.cards[reservation.card_id].status == "dropped"
    assert prefix_state.cards[reservation.card_id].selection_ready is False
    assert sum(event.type == EV_CARD_DROPPED for event in prefix) == 1
    assert not any(event.type == EV_NODE_FAILED for event in prefix)

    monkeypatch.setattr(engine.store, "append", original_append)
    assert engine._recover_interrupted_builds(prefix_state) is True
    recovered_events = engine.store.read_all()
    recovered = fold(recovered_events)
    assert recovered.buildings == {}
    assert sum(event.type == EV_CARD_DROPPED for event in recovered_events) == 1
    assert sum(event.type == EV_NODE_FAILED for event in recovered_events) == 1
    count = len(recovered_events)
    assert engine._recover_interrupted_builds(recovered) is False
    assert len(engine.store.read_all()) == count


def test_parent_generation_change_prevents_reusing_an_old_orphan_prefix(tmp_path):
    engine = _engine(tmp_path / "parent-generation", developer=_Developer())
    _start(engine)
    engine._create_node({"kind": "draft"}, preproposed=_idea("parent", 1.0))
    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 0.01,
    })

    improve = Idea(
        operator="improve",
        params={"x": 2.0},
        rationale="improve the parent",
        hypothesis="parent-aware candidate",
    )
    old_action = Engine._card_action(
        improve,
        [0],
        {"0": 0},
        0,
        0,
        scored_against_empty=False,
    )
    engine.store.append(EV_CARD_ADDED, _native_card_payload(
        "card-7", improve, action=old_action, at_node=1))

    engine.store.append(EV_NODE_RESET, {
        "node_id": 0, "generation": 0, "from_stage": "eval",
    })
    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "generation": 1, "metric": 0.9, "eval_seconds": 0.01,
    })
    assert fold(engine.store.read_all()).nodes[0].attempt == 1

    engine._create_node({
        "kind": "improve", "parent_id": 0, "parent_generations": {"0": 1},
    }, preproposed=improve)

    events = engine.store.read_all()
    registrations = [event for event in events if event.type == EV_CARD_ADDED]
    assert [event.data["id"] for event in registrations][-2:] == ["card-7", "card-8"]
    newest = registrations[-1]
    assert newest.data["parent_generations"] == {"0": 1}
    assert newest.data["scored_against"] == 0
    assert newest.data["scored_against_generation"] == 1
    created = [event for event in events if event.type == EV_NODE_CREATED][-1]
    assert created.data["idea"]["card_id"] == "card-8"


def test_dropped_orphan_is_not_resurrected_for_an_exact_retry(tmp_path):
    engine = _engine(tmp_path / "dropped-orphan", developer=_Developer())
    _start(engine)
    idea = _idea("retry dropped work", 6.0)
    engine.store.append(EV_CARD_ADDED, _native_card_payload("card-7", idea))
    engine.store.append(EV_CARD_DROPPED, {
        "id": "card-7", "reason": "cancelled", "dropped_by": "operator",
    })

    engine._create_node({"kind": "draft"}, preproposed=idea)

    events = engine.store.read_all()
    assert [event.data["id"] for event in events if event.type == EV_CARD_ADDED] == [
        "card-7", "card-8",
    ]
    assert next(event for event in events if event.type == EV_NODE_CREATED).data[
        "idea"]["card_id"] == "card-8"
    state = fold(events)
    assert state.cards["card-7"].status == "dropped"
    assert state.cards["card-7"].selection_ready is False


def test_merged_orphan_alias_is_closed_but_does_not_ban_a_fresh_retry(tmp_path):
    engine = _engine(tmp_path / "merged-orphan", developer=_Developer())
    _start(engine)
    retry = _idea("retry merged work", 7.0)
    canonical = _idea("canonical survivor", 8.0)
    engine.store.append(EV_CARD_ADDED, _native_card_payload("card-7", retry))
    engine.store.append(EV_CARD_ADDED, _native_card_payload("card-8", canonical))
    engine.store.append(EV_CARD_MERGED, {
        "canonical": "card-8", "aliases": ["card-7"],
    })

    engine._create_node({"kind": "draft"}, preproposed=retry)

    events = engine.store.read_all()
    assert [event.data["id"] for event in events if event.type == EV_CARD_ADDED] == [
        "card-7", "card-8", "card-9",
    ]
    created = next(event for event in events if event.type == EV_NODE_CREATED)
    assert created.data["idea"]["card_id"] == "card-9"
    merged = fold(events).cards["card-8"]
    assert "card-7" in merged.aliases
    assert "merged_work_items" in merged.selection_blockers


def test_novelty_sidecar_uses_the_final_reused_card_identity(tmp_path, monkeypatch):
    run_dir = tmp_path / "novelty-binding"
    idea = _idea("novelty-bound orphan", 9.0)
    before_crash = _engine(run_dir)
    _start(before_crash)
    before_crash.store.append(EV_CARD_ADDED, _native_card_payload("card-7", idea))

    # Reuse must ride the FRESH-proposal path so it crosses the novelty-admission boundary: a
    # `preproposed` idea is one the batch pass ALREADY novelty-gated (see
    # `test_strategist.py::test_serial_draft_improve_and_batch_bind_the_exact_persisted_idea`), so
    # `_prepare_node_idea` deliberately does NOT re-gate it. Here the Researcher re-proposes the exact
    # orphan action, `_plan_native_card` reuses card-7, and the gate fires exactly once on that reuse.
    resumed = _engine(run_dir, idea=idea, developer=_Developer())
    # The `_native_card_payload` prefix minted card-7 with an EMPTY steering context; neutralize the
    # complexity hint so the resumed proposal carries the same empty context and the action matches
    # for reuse (production stamps both the mint and the resume identically, so they match there too).
    monkeypatch.setattr(resumed, "_set_complexity_hint", lambda *a, **k: None)
    observed_refs: list[dict] = []

    def _record_novelty(state, candidate, *, prospective_node_id=None, **kwargs):
        assert candidate.card_id == "card-7"
        proposal_ref = idea_proposal_ref(candidate)
        assert proposal_ref is not None
        observed_refs.append(proposal_ref)
        resumed.store.append(EV_NOVELTY_GRADED, {
            "node_id": prospective_node_id,
            "generation": 0,
            "proposal_ref": proposal_ref,
            "grade": "novel",
            "level": 0,
            "recommendation": "admit",
        })
        return candidate

    monkeypatch.setattr(resumed, "_apply_novelty_gate", _record_novelty)
    resumed._create_node({"kind": "draft"})

    events = resumed.store.read_all()
    assert [event.data["id"] for event in events if event.type == EV_CARD_ADDED] == ["card-7"]
    node = fold(events).nodes[0]
    assert node.idea.card_id == "card-7"
    assert observed_refs == [idea_proposal_ref(node.idea)]
    novelty = next(event for event in events if event.type == EV_NOVELTY_GRADED)
    assert novelty.data["proposal_ref"] == idea_proposal_ref(node.idea)
    assert fold(events).cards["card-7"].novelty_verdict["grade"] == "novel"
