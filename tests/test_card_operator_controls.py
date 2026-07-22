"""Layer-6 Card operator controls: authority, replay overlays, and resource safety."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

from looplab.core.models import Idea, Node, effective_card_footprint  # noqa: E402
from looplab.engine.resources import ResourceSchedulingMixin  # noqa: E402
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.events.types import (  # noqa: E402
    EV_CARD_DROPPED, EV_CARD_EDITED, EV_CARD_MERGED, EV_CARD_REPRIORITIZED,
    EV_CARD_RESOURCE_PINNED,
)
from looplab.serve.protocol import COLLABORATION_EVENTS, CONTROL_EVENTS  # noqa: E402
from looplab.serve.run_commands import (  # noqa: E402
    CONTROL_DATA_FIELDS, CONTROL_SPECS, EnginePolicy, RunCommandService,
    normalize_control, run_generation_token,
)


CARD_CONTROLS = {
    EV_CARD_REPRIORITIZED, EV_CARD_EDITED, EV_CARD_RESOURCE_PINNED, EV_CARD_DROPPED,
}


def _seed(tmp_path: Path) -> tuple[Path, EventStore]:
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "run", "task_id": "cards", "goal": "g", "direction": "max",
    })
    store.append("card_added", {
        "id": "card-1", "statement": "immutable seed", "source": "researcher",
        "idea": {"operator": "draft", "params": {}, "space": {}},
        "footprint": {"gpus": 1, "gpu_mem_mib": 8_000},
    })
    return rd, store


class _Srv:
    def __init__(self, root: Path | None = None):
        self.root = root

    def state(self, rd: Path):
        return fold(EventStore(rd / "events.jsonl").read_all())


def test_all_four_card_controls_are_command_only_no_spawn_with_closed_payloads():
    assert CARD_CONTROLS <= CONTROL_EVENTS
    assert CARD_CONTROLS <= COLLABORATION_EVENTS
    for event_type in CARD_CONTROLS:
        spec = CONTROL_SPECS[event_type]
        assert spec.engine_policy is EnginePolicy.NO_SPAWN
        assert spec.postcondition == "folded_intent"
        assert {"source", "dropped_by", "pinned", "pinned_by"}.isdisjoint(
            CONTROL_DATA_FIELDS[event_type])
    assert CONTROL_DATA_FIELDS[EV_CARD_REPRIORITIZED] == frozenset({"id", "priority"})
    assert CONTROL_DATA_FIELDS[EV_CARD_EDITED] == frozenset({"id", "statement"})
    assert CONTROL_DATA_FIELDS[EV_CARD_RESOURCE_PINNED] == frozenset(
        {"id", "gpus", "gpu_mem_mib"})
    assert CONTROL_DATA_FIELDS[EV_CARD_DROPPED] == frozenset({"id", "reason"})


def test_normalize_control_validates_card_and_server_stamps_authority(tmp_path, monkeypatch):
    rd, _store = _seed(tmp_path)
    monkeypatch.setattr(
        "looplab.serve.run_commands._card_resource_envelope",
        lambda: (2, (16_000, 12_000)),
    )
    srv = _Srv()

    assert normalize_control(srv, rd, EV_CARD_REPRIORITIZED, {
        "id": "card-1", "priority": 4,
    }) == {
        "id": "card-1", "priority": 4, "source": "operator", "pinned": True,
    }
    assert normalize_control(srv, rd, EV_CARD_EDITED, {
        "id": "card-1", "statement": "operator display",
    }) == {"id": "card-1", "statement": "operator display", "source": "operator"}
    assert normalize_control(srv, rd, EV_CARD_RESOURCE_PINNED, {
        "id": "card-1", "gpus": 2, "gpu_mem_mib": 12_000,
    }) == {
        "id": "card-1", "gpus": 2, "gpu_mem_mib": 12_000,
        "source": "operator", "pinned": True,
    }
    assert normalize_control(srv, rd, EV_CARD_DROPPED, {
        "id": "card-1", "reason": "owner decision",
    }) == {"id": "card-1", "reason": "owner decision", "dropped_by": "operator"}

    forged = [
        (EV_CARD_REPRIORITIZED, {"id": "card-1", "priority": 1, "source": "novelty"}),
        (EV_CARD_EDITED, {"id": "card-1", "statement": "x", "source": "engine"}),
        (EV_CARD_RESOURCE_PINNED, {"id": "card-1", "gpus": 1, "pinned": False}),
        (EV_CARD_DROPPED, {"id": "card-1", "reason": "x", "dropped_by": "novelty"}),
    ]
    for event_type, payload in forged:
        with pytest.raises(HTTPException) as exc:
            normalize_control(srv, rd, event_type, payload)
        assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as missing:
        normalize_control(srv, rd, EV_CARD_EDITED, {
            "id": "card-missing", "statement": "x",
        })
    assert missing.value.status_code == 404


@pytest.mark.parametrize("payload", [
    {"id": "card-1", "gpus": 3},
    {"id": "card-1", "gpus": 2, "gpu_mem_mib": 12_001},
    {"id": "card-1", "gpus": 0, "gpu_mem_mib": 1},
    {"id": "card-1", "gpus": -1},
])
def test_resource_pin_rejects_out_of_envelope_without_folding(
        tmp_path, monkeypatch, payload):
    rd, store = _seed(tmp_path)
    monkeypatch.setattr(
        "looplab.serve.run_commands._card_resource_envelope",
        lambda: (2, (16_000, 12_000)),
    )
    before = store.read_all()
    with pytest.raises(HTTPException) as exc:
        normalize_control(_Srv(), rd, EV_CARD_RESOURCE_PINNED, payload)
    assert exc.value.status_code == 400
    assert store.read_all() == before


def test_append_time_recheck_rejects_card_that_disappears_after_intake(
        tmp_path, monkeypatch):
    rd, store = _seed(tmp_path)
    srv = _Srv(rd.parent)
    normalized = normalize_control(srv, rd, EV_CARD_EDITED, {
        "id": "card-1", "statement": "operator display",
    })
    generation = run_generation_token(store.read_all())
    original_append = EventStore.append
    raced = False

    def append(self, event_type, data, **kwargs):
        nonlocal raced
        if (self.path == store.path and event_type == EV_CARD_EDITED
                and kwargs.get("require_lock") and not raced):
            raced = True
            # A conflicting durable registration makes this id unrepresentable and therefore removes
            # it from the derived Card projection. The lost tail CAS must refold before appending.
            concurrent = original_append(self, "card_added", {
                "id": "card-1", "statement": "conflicting seed", "source": "researcher",
            })
            raise EventStoreConcurrencyError(
                self.path, int(kwargs["expected_last_seq"]), concurrent.seq)
        return original_append(self, event_type, data, **kwargs)

    monkeypatch.setattr(EventStore, "append", append)
    intent, _baseline, error = RunCommandService(srv)._append_collaboration_intent(
        rd,
        {"event_type": EV_CARD_EDITED, "run_generation": generation},
        normalized,
    )

    assert raced is True
    assert intent is None
    assert error["code"] == "card_not_found"
    assert all(event.type != EV_CARD_EDITED for event in store.read_all())


def test_append_time_recheck_rejects_resource_pin_after_envelope_shrinks(
        tmp_path, monkeypatch):
    rd, store = _seed(tmp_path)
    srv = _Srv(rd.parent)
    envelopes = iter([(2, (16_000, 12_000)), (1, (16_000,))])
    monkeypatch.setattr(
        "looplab.serve.run_commands._card_resource_envelope",
        lambda: next(envelopes),
    )
    normalized = normalize_control(srv, rd, EV_CARD_RESOURCE_PINNED, {
        "id": "card-1", "gpus": 2, "gpu_mem_mib": 12_000,
    })

    intent, _baseline, error = RunCommandService(srv)._append_collaboration_intent(
        rd,
        {
            "event_type": EV_CARD_RESOURCE_PINNED,
            "run_generation": run_generation_token(store.read_all()),
        },
        normalized,
    )

    assert intent is None
    assert error["code"] == "card_resource_envelope_changed"
    assert all(event.type != EV_CARD_RESOURCE_PINNED for event in store.read_all())


def test_terminal_card_rejects_mutation_but_allows_redrop(tmp_path):
    """A dropped Card is closed to operator MUTATION (edit/reprioritize/pin) at the durable append-time
    guard, so a stale client / direct API caller cannot append contradictory history onto an
    already-terminal Card. EV_CARD_DROPPED stays allowed (idempotent re-drop / override of an engine
    drop reason). The React client hides these controls, but the guard is the all-clients contract.
    (A merged alias is collapsed out of state.cards, so it fails the earlier card_not_found guard; the
    `merged_into` arm of the lifecycle check is a defensive/forward-compat backstop.)"""
    rd, store = _seed(tmp_path)
    store.append(EV_CARD_DROPPED,
                 {"id": "card-1", "reason": "owner decision", "dropped_by": "operator"})
    state = fold(store.read_all())
    assert state.cards["card-1"].status == "dropped"

    for event_type, data in (
        (EV_CARD_EDITED, {"id": "card-1", "statement": "x", "source": "operator"}),
        (EV_CARD_REPRIORITIZED,
         {"id": "card-1", "priority": 3, "source": "operator", "pinned": True}),
        (EV_CARD_RESOURCE_PINNED,
         {"id": "card-1", "gpus": 1, "source": "operator", "pinned_by": "operator"}),
    ):
        error = RunCommandService._collaboration_precondition(state, event_type, data)
        assert error is not None and error["code"] == "card_lifecycle_closed", event_type
    # A re-drop of the already-terminal Card is deliberately NOT blocked by the lifecycle guard.
    assert RunCommandService._collaboration_precondition(
        state, EV_CARD_DROPPED,
        {"id": "card-1", "reason": "again", "dropped_by": "operator"}) is None


def test_operator_overlays_win_without_mutating_seed_or_action_footprint(tmp_path):
    _rd, store = _seed(tmp_path)
    # Controls deliberately arrive before later engine/Strategist projections. The final phase must
    # still win structurally rather than relying on arrival order.
    store.append(EV_CARD_REPRIORITIZED, {
        "id": "card-1", "priority": 7, "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_EDITED, {
        "id": "card-1", "statement": "display paraphrase", "source": "operator",
    })
    store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-1", "gpus": 1, "gpu_mem_mib": 4_000,
        "source": "operator", "pinned": True,
    })
    store.append("card_ranked", {"order": ["card-1"], "confidence": 0.9})
    store.append("card_enriched", {
        "id": "card-1", "footprint": {"gpus": 2, "gpu_mem_mib": 12_000},
    })
    # A hash-joined legacy node remains attached through the immutable seed, not the display edit.
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "hypothesis": "immutable seed"}, "code": "pass",
    })
    state = fold(store.read_all())
    card = state.cards["card-1"]

    assert card.seed_statement == "immutable seed"
    assert card.statement == "display paraphrase"
    assert card.priority == 7 and card.pinned is True
    assert card.footprint == {"gpus": 2, "gpu_mem_mib": 12_000}
    assert card.resource_pin == {
        "gpus": 1, "gpu_mem_mib": 4_000, "pinned_by": "operator",
    }
    assert 0 in card.evidence
    assert state.card_operator_edits["card-1"]["statement"] == "display paraphrase"
    assert state.card_priority_pins["card-1"] == 7
    assert state.card_resource_pins["card-1"]["gpus"] == 1


def test_operator_overlays_keep_global_last_write_after_alias_merge(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "run", "task_id": "cards", "goal": "g", "direction": "max",
    })
    store.append("card_added", {"id": "card-a", "statement": "alias seed"})
    store.append("card_added", {"id": "card-c", "statement": "canonical seed"})

    # A is first inserted into every LWW map, then C is written, then A is written again. A plain
    # dict assignment would retain A's original insertion position and incorrectly let C win when
    # both raw ids later resolve to the same canonical Card.
    store.append(EV_CARD_REPRIORITIZED, {
        "id": "card-a", "priority": 1, "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_EDITED, {
        "id": "card-a", "statement": "early alias display", "source": "operator",
    })
    store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-a", "gpus": 1, "gpu_mem_mib": 4_000,
        "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_REPRIORITIZED, {
        "id": "card-c", "priority": 2, "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_EDITED, {
        "id": "card-c", "statement": "canonical display", "source": "operator",
    })
    store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-c", "gpus": 2, "gpu_mem_mib": 8_000,
        "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_REPRIORITIZED, {
        "id": "card-a", "priority": 23, "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_EDITED, {
        "id": "card-a", "statement": "latest alias display", "source": "operator",
    })
    store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-a", "gpus": 1, "gpu_mem_mib": 3_000,
        "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_MERGED, {"canonical": "card-c", "aliases": ["card-a"]})

    state = fold(store.read_all())
    assert set(state.cards) == {"card-c"}
    card = state.cards["card-c"]
    assert "card-a" in card.aliases
    assert card.seed_statement == "canonical seed"
    assert card.statement == "latest alias display"
    assert card.priority == 23 and card.pinned is True
    assert card.resource_pin == {
        "gpus": 1, "gpu_mem_mib": 3_000, "pinned_by": "operator",
    }


def test_replay_rejects_forged_operator_stamps(tmp_path):
    _rd, store = _seed(tmp_path)
    store.append(EV_CARD_REPRIORITIZED, {
        "id": "card-1", "priority": 1, "source": "novelty", "pinned": True,
    })
    store.append(EV_CARD_EDITED, {
        "id": "card-1", "statement": "forged", "source": "engine",
    })
    store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-1", "gpus": 0, "source": "operator", "pinned": False,
    })
    card = fold(store.read_all()).cards["card-1"]
    assert card.statement == "immutable seed"
    assert card.pinned is False
    assert card.resource_pin is None


def test_effective_resource_pin_reclamps_on_resume_and_scheduler_uses_it():
    assert effective_card_footprint(
        {"gpus": 1, "gpu_mem_mib": 8_000}, {"gpus": 0},
    ) == {"gpus": 0}
    assert effective_card_footprint(
        {"gpus": 1, "gpu_mem_mib": 8_000},
        {"gpus": 4, "gpu_mem_mib": 24_000, "pinned_by": "operator"},
        gpu_count=2,
        gpu_memory_mib=(16_000, 12_000),
    ) == {"gpus": 2, "gpu_mem_mib": 12_000}

    scheduler = ResourceSchedulingMixin()
    scheduler._gpu_ids = [0, 1]
    scheduler._gpu_mem = {0: 16_000, 1: 12_000}
    scheduler._eval_parallel = 2
    node = Node(
        id=0, operator="draft",
        idea=Idea(operator="draft", card_id="card-1", footprint={"gpus": 1}),
    )
    request = scheduler._resource_request_for_node(
        node, resource_pin={"gpus": 2, "gpu_mem_mib": 20_000, "pinned_by": "operator"})
    assert request["count"] == 2
    assert request["gpu_mem_mib"] == 12_000
    assert request["footprint"] == {"gpus": 2, "gpu_mem_mib": 12_000}
    assert node.idea.footprint == {"gpus": 1}  # receipt-owned base is unchanged


def test_scheduler_resolves_canonical_pin_for_pending_node_with_merged_alias(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "run", "task_id": "cards", "goal": "g", "direction": "max",
    })
    store.append("card_added", {"id": "card-a", "statement": "alias seed"})
    store.append("card_added", {"id": "card-c", "statement": "canonical seed"})
    store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-c", "gpus": 2, "gpu_mem_mib": 12_000,
        "source": "operator", "pinned": True,
    })
    store.append(EV_CARD_MERGED, {"canonical": "card-c", "aliases": ["card-a"]})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft", "code": "pass",
        "idea": {
            "operator": "draft", "card_id": "card-a",
            "footprint": {"gpus": 1, "gpu_mem_mib": 4_000},
        },
    })

    state = fold(store.read_all())
    assert set(state.cards) == {"card-c"}
    assert "card-a" not in state.cards
    node = state.nodes[0]
    assert node.status.value == "pending"
    assert node.idea.card_id == "card-a"

    scheduler = ResourceSchedulingMixin()
    scheduler._gpu_ids = [0, 1]
    scheduler._gpu_mem = {0: 16_000, 1: 12_000}
    scheduler._eval_parallel = 2
    pin = scheduler._card_resource_pin_for_node(state, node)
    assert pin == {"gpus": 2, "gpu_mem_mib": 12_000, "pinned_by": "operator"}

    admitted = scheduler._resource_request_for_node(node, resource_pin=pin)
    reservation = {**admitted, "gpu_ids": [0, 1]}
    assert admitted["count"] == 2
    assert admitted["gpu_mem_mib"] == 12_000
    assert scheduler._node_resource_reservation_is_current(state, node, reservation) is True
