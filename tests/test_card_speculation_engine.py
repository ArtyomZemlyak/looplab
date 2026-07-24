"""Layer-5 engine contracts for request-driven Card speculation.

The pure counterfactual selector has its own focused suite.  These tests pin the execution seam:
the event log is the queue, producer work is event-free, only the main task commits a speculative
Node, and every crash prefix is either resumed or explicitly given up without duplicating work.
"""
from __future__ import annotations

import ast
import inspect
import threading
import textwrap
from pathlib import Path

import anyio
import pytest

import looplab.engine.speculation as speculation_module
import looplab.search.speculation_quality as speculation_quality
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.config import Settings
from looplab.core.models import (
    Card,
    CardIdentityProvenance,
    CardSelectionProvenance,
    Idea,
    Node,
    NodeStatus,
    RunState,
)
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import (
    Engine,
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
)
from looplab.events.replay import fold
from looplab.events.types import (
    EV_BUDGET_EXTEND,
    EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED,
    EV_CARD_RESOURCE_PINNED,
    EV_LLM_COST,
    EV_LLM_USAGE,
    EV_NODE_BUILDING,
    EV_NODE_CREATED,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_PAUSE,
    EV_POLICY_DECISION,
    EV_RESUME,
    EV_RUN_FINISHED,
    EV_RUN_REOPENED,
    EV_RUNG_PROMOTED,
)
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    card_budget_used,
    speculative_card_actions,
)
from looplab.search.policy import GreedyTree
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_SEEDS,
    speculation_runtime_scope_digest,
)


_DIGEST = "card-action:v1:" + "5" * 64


@pytest.fixture(autouse=True)
def _admit_unit_speculation_receipt(monkeypatch):
    """Keep mechanics tests on the public receipt boundary, without real gate evidence."""
    task = ToyTask()

    def _validated(path):
        max_nodes, depth = map(int, Path(path).stem.rsplit("-", 2)[-2:])
        runtime_scope = speculation_runtime_scope_digest({
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": max_nodes,
            "speculation_depth": depth,
            "speculation_gate_receipt": str(path),
        })
        return {
            "self_digest": "sha256:" + "a" * 64,
            "implementation_digest": "sha256:" + "b" * 64,
            "require_gpu": True,
            "gpu_inventory": [{
                "index": 0,
                "uuid": "GPU-11111111-2222-3333-4444-555555555555",
                "pci_bus_id": "00000000:01:00.0",
                "name": "unit-gpu",
                "mem_total_mib": 24_576,
                "driver_version": "595.79",
                "cuda_driver_version": 13000,
            }],
            "policy_scope": "greedy",
            "admitted_depth": depth,
            "admitted_max_nodes": max_nodes,
            "runtime_scope_sha256": runtime_scope,
            "calibration_profile_digest": SPECULATION_CALIBRATION_PROFILE_DIGEST,
            "calibration_seeds": list(SPECULATION_CALIBRATION_SEEDS),
            "workload_scope": "quadratic_toy",
            "task_profile_sha256": speculation_quality.speculation_task_profile_digest(task),
        }

    monkeypatch.setattr(
        speculation_quality, "validated_speculation_gate_receipt", _validated)


class _Researcher:
    def propose(self, *_args, **_kwargs):
        raise AssertionError("an existing durable Card must not be proposed again")


class _RawResearcher:
    def __init__(self):
        self.calls = 0

    def propose(self, _state, _parent):
        self.calls += 1
        return Idea(
            operator="draft",
            params={"x": 0.3 + self.calls / 10, "y": -1.0},
            rationale=f"steady-state proposal {self.calls}",
            hypothesis=f"steady-state hypothesis {self.calls}",
        )


class _RejectingRawResearcher:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def propose(self, _state, _parent):
        self.calls += 1
        return self.result


class _Developer:
    def __init__(self, *, code: str = "print(1)", error: str | None = None):
        self.code = code
        self.error = error
        self.calls = 0
        self.last_files: dict[str, str] = {}
        self.last_deleted: list[str] = []

    def implement(self, _idea: Idea) -> str:
        self.calls += 1
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.code


class _DelayedSecondBuildDeveloper(_Developer):
    """Let one bootstrap build finish, then hold the live prefetch until explicitly released."""

    def __init__(self):
        super().__init__()
        self.second_started = threading.Event()
        self.release_second = threading.Event()

    def implement(self, _idea: Idea) -> str:
        self.calls += 1
        if self.calls == 2:
            self.second_started.set()
            if not self.release_second.wait(timeout=10):
                raise RuntimeError("timed out waiting to release delayed speculative build")
        return self.code


def _engine(
    run_dir,
    *,
    depth: int = 1,
    producer: _Developer | None = None,
    isolated_roles: bool = True,
) -> tuple[Engine, _Developer]:
    task = ToyTask()
    producer = producer or _Developer()
    role_factory = (lambda: (_Researcher(), producer)) if isolated_roles else None
    if depth > 0:
        receipt_path = str(
            Path(run_dir) / f"unit-speculation-receipt-8-{depth}")
        settings = Settings(**{
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": 8,
            "speculation_depth": depth,
            "speculation_gate_receipt": receipt_path,
        })

        def calibrated_roles():
            return (
                ToyResearcher(
                    task.bounds,
                    seed=task.seed,
                    step=task.step,
                    calibration_concepts=True,
                ),
                ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
            )

        engine = Engine(
            run_dir,
            task=task,
            researcher=calibrated_roles()[0],
            developer=calibrated_roles()[1],
            sandbox=SubprocessSandbox(),
            policy=GreedyTree(n_seeds=3, max_nodes=8, debug_depth=1),
            options=EngineOptions.from_settings(settings),
            role_factory=calibrated_roles,
            _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
                settings.masked_snapshot()),
        )
        # Admission is production-exact.  Only after that boundary do these mechanics tests replace
        # the roles/policy with deterministic sentinels for the queue/concurrency behavior at issue.
        engine.researcher = _Researcher()
        engine.developer = _Developer()
        engine.role_factory = role_factory
        engine.policy = GreedyTree(n_seeds=0, max_nodes=8, debug_depth=0)
    else:
        engine = Engine(
            run_dir,
            task=task,
            researcher=_Researcher(),
            developer=_Developer(),
            sandbox=SubprocessSandbox(),
            policy=GreedyTree(n_seeds=0, max_nodes=8, debug_depth=0),
            n_seeds=0,
            max_nodes=8,
            card_driven_selection=True,
            speculation_depth=0,
            role_factory=role_factory,
        )
    engine._novelty_mode = "off"
    # Unit tests exercise admission deterministically on a CPU envelope, irrespective of the host.
    engine._gpu_ids = []
    engine._gpu_physical_ids = {}
    engine._gpu_mem = {}
    engine._free_gpus = []
    return engine, producer


def _start(engine: Engine) -> None:
    payload = {
        "run_id": engine.run_dir.name,
        "task_id": "toy",
        "goal": "g",
        "direction": "min",
        **engine._run_start_pinned_values(),
    }
    engine.store.append("run_started", payload)


def _cross_run_receipt() -> dict:
    segment = {
        "read_complete": True,
        "rows_total": 0,
        "rows_retained": 0,
        "rows_quarantined": 0,
        "malformed_rows": 0,
        "invalid_rows": 0,
    }
    return {
        "v": 2,
        "scope_task": "toy",
        "excluded_run": "prior-run",
        "n_lessons": 0,
        "n_capsules": 1,
        "n_research": 0,
        "concept_scope": {
            "scope_complete": True,
            "scope_unknown_capsules": 0,
            "scope_fingerprint_unknown_capsules": 0,
            "scope_fingerprint_items_omitted": 0,
            "scope_direction_unknown_capsules": 0,
        },
        "claim_source": {
            "v": 1,
            "receipt_known": True,
            "source_complete": True,
            "read_complete": True,
            "research_source_complete": True,
            "lessons": dict(segment),
            "research": dict(segment),
            "snapshot_digest": "a" * 64,
        },
        "corpus_digest": "b" * 64,
        "render_digest": "c" * 64,
    }


def _add_ready_draft(
    engine: Engine,
    card_id: str = "card-7",
    *,
    x: float = 0.25,
    cross_run_receipt=None,
    replay_cross_run_receipt=None,
) -> Idea:
    idea = Idea(
        operator="draft",
        params={"x": x, "y": -1.0},
        rationale=f"use queued proposal {card_id}",
        hypothesis=f"queued proposal {card_id} improves the objective",
        card_id=card_id,
    )
    action = Engine._card_action(
        idea, [], {}, None, None, scored_against_empty=True,
    )
    statement = Engine._card_statement(idea)
    assert statement is not None
    payload = Engine._card_added_payload(
        card_id,
        statement,
        action,
        idea,
        source="researcher",
        at_node=0,
        cross_run_receipt=cross_run_receipt,
    )
    if replay_cross_run_receipt is not None:
        # Model a legacy/forged journal row that did not pass through the current writer boundary.
        payload["cross_run_receipt"] = replay_cross_run_receipt
    engine.store.append("card_added", payload)
    return idea


def _request(engine: Engine) -> dict:
    assert engine._request_card_build() is True
    state = fold(engine.store.read_all())
    request = engine._head_request(state)
    assert request is not None
    return request


def _build_result(engine: Engine, request: dict):
    roles = engine._producer_role_pair()
    assert roles is not None
    return engine._build_requested_card(request, roles)


def _commit_speculative_node(engine: Engine) -> int:
    before = set(fold(engine.store.read_all()).speculative_nodes)
    request = _request(engine)
    result = _build_result(engine, request)
    assert result.success is True
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True
    state = fold(engine.store.read_all())
    created = set(state.speculative_nodes) - before
    assert len(created) == 1
    link = created.pop()
    assert state.nodes[link].status is NodeStatus.pending
    return link


def _without_research(monkeypatch, engine: Engine) -> None:
    monkeypatch.setattr(engine, "_spawn_research", lambda *_args: None)


def test_depth_zero_delegates_to_legacy_dispatcher_and_never_requests(tmp_path, monkeypatch):
    engine, _producer = _engine(tmp_path / "depth-zero", depth=0)
    _start(engine)
    seen = []

    async def _legacy(evals, state, max_es):
        seen.append((evals, state, max_es))

    monkeypatch.setattr(engine, "_dispatch_evals", _legacy)
    initial = fold(engine.store.read_all())

    anyio.run(engine._run_card_session, [], initial, None)

    assert seen == [([], initial, None)]
    assert not [
        event for event in engine.store.read_all()
        if event.type == EV_CARD_BUILD_REQUESTED
    ]


def test_depth_three_counts_exact_pending_backlog_and_never_crosses_cap(tmp_path):
    engine, producer = _engine(tmp_path / "depth-three", depth=3)
    _start(engine)
    for index, x in enumerate((0.2, 0.4, 0.6, 0.8), start=1):
        _add_ready_draft(engine, f"card-{index}", x=x)

    node_ids = [_commit_speculative_node(engine) for _ in range(3)]
    at_cap = fold(engine.store.read_all())
    assert producer.calls == 3
    assert len(set(node_ids)) == 3
    assert engine._speculation_depth_used(at_cap) == 3

    request_count = len(at_cap.card_build_requests)
    assert engine._request_card_build() is False
    assert len(fold(engine.store.read_all()).card_build_requests) == request_count

    # Only an exact eval admission removes one pending attempt from prefetch inventory. A wrong
    # generation cannot create capacity; the exact identity admits one replacement request and the
    # resulting pending+request backlog remains exactly at the configured cap.
    assert engine._request_card_build(consumed_inflight={(node_ids[0], 1)}) is False
    assert engine._request_card_build(consumed_inflight={(node_ids[0], 0)}) is True
    with_replacement = fold(engine.store.read_all())
    assert engine._speculation_depth_used(
        with_replacement,
        consumed_inflight={(node_ids[0], 0)},
    ) == 3
    assert len(with_replacement.card_build_requests) == request_count + 1
    assert engine._request_card_build(
        consumed_inflight={(node_ids[0], 0)},
    ) is False


def test_exact_request_result_commit_writes_one_main_task_lifecycle(tmp_path):
    engine, producer = _engine(tmp_path / "exact")
    _start(engine)
    idea = _add_ready_draft(engine)
    request = _request(engine)
    roles = engine._producer_role_pair()
    assert roles is not None
    assert roles[0] is not engine.researcher and roles[1] is not engine.developer

    prefix = engine.store.read_all()
    result = engine._build_requested_card(request, roles)
    # The worker returns an in-memory result and is never a folded-event writer.
    assert engine.store.read_all() == prefix
    assert result.success is True and result.idea is not None
    assert result.idea.card_id == idea.card_id and producer.calls == 1

    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    events = engine.store.read_all()
    lifecycle = [
        event for event in events
        if event.type in {
            EV_CARD_BUILD_REQUESTED,
            EV_NODE_BUILDING,
            EV_NODE_CREATED,
            EV_CARD_BUILD_DONE,
        }
    ]
    assert [event.type for event in lifecycle] == [
        EV_CARD_BUILD_REQUESTED,
        EV_NODE_BUILDING,
        EV_NODE_CREATED,
        EV_CARD_BUILD_DONE,
    ]
    building = lifecycle[1]
    created = lifecycle[2]
    done = lifecycle[3]
    assert building.data["speculative"] is True
    assert building.data["card_build_generation"] == request["generation"]
    assert created.data["speculative"] is True
    assert created.data["card_build_generation"] == request["generation"]
    assert created.data["idea"]["card_id"] == request["card_id"]
    assert done.data == {
        "card_id": request["card_id"],
        "generation": request["generation"],
        "node_id": created.data["node_id"],
        "speculative": True,
    }
    state = fold(events)
    assert state.card_builds_done == 1
    assert state.speculative_nodes == {
        created.data["node_id"]: {
            "card_id": request["card_id"],
            "generation": request["generation"],
        },
    }


def test_node_building_folds_only_a_complete_speculative_request_identity(tmp_path):
    engine, _producer = _engine(tmp_path / "building-request-identity")
    _start(engine)
    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 0,
        "operator": "draft",
        "parent_ids": [],
        "card_id": "card-exact",
        "speculative": True,
        "card_build_generation": 7,
    })
    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 1,
        "operator": "draft",
        "parent_ids": [],
        "card_id": "card-partial",
        "speculative": True,
    })

    state = fold(engine.store.read_all())
    assert state.buildings[0] == {
        "node_id": 0,
        "operator": "draft",
        "parent_ids": [],
        "started": state.buildings[0]["started"],
        "card_id": "card-exact",
        "speculative": True,
        "card_build_generation": 7,
    }
    assert "speculative" not in state.buildings[1]
    assert "card_build_generation" not in state.buildings[1]


def test_request_reservations_match_physical_owners_one_to_one_and_credit_only_head(
    tmp_path,
):
    engine, _producer = _engine(tmp_path / "request-owner-multiset")
    engine._base_max_nodes = 5
    gen_one = {"card_id": "card-same", "generation": 1}
    gen_two = {"card_id": "card-same", "generation": 2}
    state = RunState(
        card_build_requests=[gen_one, gen_one, gen_two],
        buildings={
            0: {
                "node_id": 0,
                "operator": "draft",
                "parent_ids": [],
                "card_id": "card-same",
                "speculative": True,
                "card_build_generation": 1,
            },
            # An ordinary Card marker is not a speculative request reservation.
            1: {
                "node_id": 1,
                "operator": "draft",
                "parent_ids": [],
                "card_id": "card-same",
            },
        },
        nodes={
            2: Node(
                id=2,
                operator="draft",
                idea=Idea(operator="draft", card_id="card-same"),
                speculative=True,
                card_build_generation=1,
            ),
            3: Node(
                id=3,
                operator="draft",
                idea=Idea(operator="draft", card_id="card-same"),
                speculative=True,
                card_build_generation=2,
            ),
        },
        # Node 3 already closed an earlier positional request and cannot satisfy request index 2.
        speculative_nodes={3: gen_two},
    )

    assert engine._unmaterialized_card_request_indices(state) == {2}

    # The only unmaterialized owner is not the queue head. ``consume_request=True`` therefore cannot
    # borrow its slot while converting the already-materialized head.
    assert engine._node_reservation_slots_remaining(
        state, events=[], consume_request=False,
    ) == 0
    assert engine._node_reservation_slots_remaining(
        state, events=[], consume_request=True,
    ) == 0

    unbuilt = RunState(card_build_requests=[gen_one, gen_two])
    engine._base_max_nodes = 2
    assert engine._unmaterialized_card_request_indices(unbuilt) == {0, 1}
    assert engine._node_reservation_slots_remaining(
        unbuilt, events=[], consume_request=True,
    ) == 1


def test_matching_created_speculation_never_reuses_an_already_linked_node(tmp_path):
    engine, _producer = _engine(tmp_path / "unlinked-created-match")
    request = {"card_id": "card-repeat", "generation": 4}
    state = RunState(
        card_build_requests=[request, request],
        nodes={
            node_id: Node(
                id=node_id,
                operator="draft",
                idea=Idea(operator="draft", card_id="card-repeat"),
                speculative=True,
                card_build_generation=4,
            )
            for node_id in (0, 1)
        },
        speculative_nodes={0: request},
    )

    assert engine._matching_created_speculation(state, request).id == 1
    state.speculative_nodes[1] = dict(request)
    assert engine._matching_created_speculation(state, request) is None


def test_speculative_resume_carries_only_valid_card_registered_cross_run_receipt(tmp_path):
    receipt = _cross_run_receipt()
    engine, _producer = _engine(tmp_path / "valid-provenance")
    _start(engine)
    _add_ready_draft(engine, cross_run_receipt=receipt)
    request = _request(engine)
    result = _build_result(engine, request)

    assert result.success is True
    assert result.cross_run_receipt == receipt
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True
    created = next(
        event for event in engine.store.read_all()
        if event.type == EV_NODE_CREATED
    )
    assert created.data["cross_run_receipt"] == receipt

    forged = {**receipt, "unknown_future_authority": {"api_key": "sk-forged-secret"}}
    forged_engine, _producer = _engine(tmp_path / "forged-provenance")
    _start(forged_engine)
    _add_ready_draft(
        forged_engine,
        replay_cross_run_receipt=forged,
    )
    forged_request = _request(forged_engine)
    forged_result = _build_result(forged_engine, forged_request)

    assert forged_result.success is True
    assert forged_result.cross_run_receipt == {}
    forged_engine._ensure_speculation_state()
    forged_engine._spec_builds[forged_result.key] = forged_result
    assert forged_engine._serve_card_builds() is True
    forged_created = next(
        event for event in forged_engine.store.read_all()
        if event.type == EV_NODE_CREATED
    )
    assert forged_created.data["cross_run_receipt"] == {}


def test_speculative_claim_emits_policy_and_rung_audit_exactly_once(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "claim-audit")
    _start(engine)
    idea = _add_ready_draft(engine)
    selected = {
        "kind": "draft",
        "_card_id": idea.card_id,
        "_scores": {0: 0.75},
        "_chosen": 0,
        "_reason": "test selection",
        "_rung": 0,
        "_promoted": [0],
    }
    monkeypatch.setattr(
        speculation_module,
        "speculative_card_actions",
        lambda *_args, **_kwargs: [dict(selected)],
    )
    # Model an ordinary widened-lane commit that recorded the common halving receipt first. The
    # speculative sibling below must not append an indistinguishable second row.
    assert engine._append_rung_promotion(selected) is True

    request = _request(engine)
    result = _build_result(engine, request)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    events = engine.store.read_all()
    decisions = [event for event in events if event.type == EV_POLICY_DECISION]
    promotions = [event for event in events if event.type == EV_RUNG_PROMOTED]
    assert len(decisions) == 1
    assert decisions[0].data["chosen"] == 0
    assert decisions[0].data["reason"] == "test selection"
    assert list(decisions[0].data["scores"].values()) == [0.75]
    assert len(promotions) == 1
    assert promotions[0].data == {"rung": 0, "survivors": [0]}

    # Dedupe authority is the log, so a new Engine process reaches the same decision after resume.
    resumed, _unused = _engine(tmp_path / "claim-audit")
    assert resumed._append_rung_promotion(selected) is False


def test_speculative_last_slot_request_waits_for_budget_extend_without_rebuild(tmp_path):
    engine, producer = _engine(tmp_path / "request-budget-wait")
    _start(engine)
    _add_ready_draft(engine)
    engine._base_max_nodes = 0
    engine.policy.max_nodes = 0
    state = fold(engine.store.read_all())
    engine.store.append(EV_CARD_BUILD_REQUESTED, {
        "card_id": "card-7", "generation": state.search_epoch,
    })
    request = engine._head_request(fold(engine.store.read_all()))
    assert request is not None
    result = _build_result(engine, request)
    assert result.success is True and producer.calls == 1
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result

    assert engine._serve_card_builds() is False
    blocked = fold(engine.store.read_all())
    assert blocked.card_builds_done == 0
    assert result.key in engine._spec_builds
    assert not [event for event in engine.store.read_all() if event.type == EV_NODE_BUILDING]

    engine.store.append(EV_BUDGET_EXTEND, {"add_nodes": 1})
    assert engine._serve_card_builds() is True
    events = engine.store.read_all()
    assert producer.calls == 1  # commit the paid buffered result; never rebuild it after extension
    assert len([event for event in events if event.type == EV_NODE_BUILDING]) == 1
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 1
    assert fold(events).card_builds_done == 1


def test_recovery_dropped_head_with_no_result_closes_stale_instead_of_wedging(tmp_path):
    # CODEX AGENT (crash-recovery wedge): a kill between node_building and node_created spends the
    # interrupted build's Node id (it keeps counting against the physical ceiling via _node_id_ceiling)
    # AND recovery drops its Card, but the durable request survives at head with no in-memory result.
    # Capacity is then zero, so no producer can be started to close it — `_serve_card_builds` must not
    # return False forever. It recognizes the dropped, producer-less head as permanently unbuildable and
    # closes it `stale`, so the outstanding request clears and the session can exit instead of polling.
    engine, _producer = _engine(tmp_path / "recovery-wedge")
    _start(engine)
    _add_ready_draft(engine)
    request = _request(engine)                 # durable card_build_requested for card-7 at head
    key = engine._request_key(request)
    engine._ensure_speculation_state()
    assert not engine._spec_builds and not engine._spec_build_inflight  # a crash lost every in-memory result

    # An ALIVE head with no result and no in-flight producer must stay open: a producer can still start.
    assert engine._serve_card_builds() is False
    assert fold(engine.store.read_all()).card_builds_done == 0

    # Recovery drops the Card of the interrupted build (its Node id stays spent as a ceiling gap).
    engine._drop_card_once("card-7", reason="build_interrupted")
    dropped = fold(engine.store.read_all())
    assert dropped.cards["card-7"].status == "dropped"
    assert engine._request_key(engine._head_request(dropped)) == key  # request still outstanding at head

    # Now the head is permanently unbuildable: close it stale rather than wedging on an infinite poll.
    assert engine._serve_card_builds() is True
    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data.get("skipped") == "stale"
    assert not [event for event in events if event.type == EV_NODE_BUILDING]  # no phantom reservation
    final = fold(events)
    assert final.card_builds_done == 1 and engine._head_request(final) is None  # outstanding cleared


def test_recovery_merged_head_with_no_result_closes_stale_instead_of_wedging(tmp_path):
    # Sibling of the dropped-head wedge: a durable card_build_requested survives at head with no in-memory
    # result, but its Card was MERGED away (folded into a canonical) rather than dropped. A merged Card is
    # ABSENT from state.cards (recorded only in the canonical's `aliases`; the fold never sets
    # `merged_into`), so the b421d4e close-on-dropped/merged branch must recognize it via ALIAS membership
    # — else the head stays outstanding and the session polls forever, the exact wedge that branch exists
    # to prevent (the `merged_into is not None` half alone never fires, since merged cards are absent).
    engine, _producer = _engine(tmp_path / "recovery-merge-wedge")
    _start(engine)
    _add_ready_draft(engine, "card-7")
    request = _request(engine)                 # durable card_build_requested for card-7 at head
    key = engine._request_key(request)
    engine._ensure_speculation_state()
    assert not engine._spec_builds and not engine._spec_build_inflight

    # An ALIVE head with no result and no in-flight producer must stay open.
    assert engine._serve_card_builds() is False
    assert fold(engine.store.read_all()).card_builds_done == 0

    # Merge card-7 INTO a canonical: the fold collapses card-7 OUT of state.cards and records it only in
    # the canonical's aliases (merged_into is never assigned).
    _add_ready_draft(engine, "card-9")         # the canonical card-7 is folded into
    engine.store.append(
        "card_merged", {"canonical": "card-9", "aliases": ["card-7"], "merged_by": "engine"})
    merged = fold(engine.store.read_all())
    assert "card-7" not in merged.cards                        # merged away -> ABSENT
    assert "card-7" in (merged.cards["card-9"].aliases or [])  # tracked via the canonical's aliases
    assert engine._request_key(engine._head_request(merged)) == key  # request still outstanding at head

    # The merged head is permanently unbuildable: close it stale via alias membership, never wedge.
    assert engine._serve_card_builds() is True
    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data.get("skipped") == "stale"
    final = fold(events)
    assert final.card_builds_done == 1 and engine._head_request(final) is None  # outstanding cleared


def test_recovery_reason_less_dropped_head_closes_stale_instead_of_wedging(tmp_path):
    # Regression: a valid reason-less `card_dropped` folds to status=="dropped" with dropped_reason=None.
    # Keying the crash-recovery close on `dropped_reason is not None` (instead of the folded status) left
    # such a head outstanding forever — the session polls without exit. Key on status=="dropped".
    engine, _producer = _engine(tmp_path / "reasonless-drop-wedge")
    _start(engine)
    _add_ready_draft(engine, "card-7")
    request = _request(engine)
    key = engine._request_key(request)
    engine._ensure_speculation_state()
    assert not engine._spec_builds and not engine._spec_build_inflight

    assert engine._serve_card_builds() is False           # an alive head with no result stays open

    engine._drop_card_once("card-7", reason="")           # REASON-LESS drop -> dropped_reason folds None
    dropped = fold(engine.store.read_all())
    assert dropped.cards["card-7"].status == "dropped"
    assert dropped.cards["card-7"].dropped_reason is None  # the exact gap this regression covers
    assert engine._request_key(engine._head_request(dropped)) == key  # request still at head

    assert engine._serve_card_builds() is True            # closed stale via status, not wedged
    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data.get("skipped") == "stale"
    final = fold(events)
    assert final.card_builds_done == 1 and engine._head_request(final) is None


def test_producer_exception_closes_head_as_skipped_without_live_wedge(
    tmp_path, monkeypatch,
):
    producer = _Developer(error="producer exploded")
    engine, _producer = _engine(tmp_path / "producer-error", producer=producer)
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)

    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data["skipped"] == "producer_failed"
    state = fold(events)
    assert state.card_builds_done == len(state.card_build_requests) == 1
    assert not state.buildings and not state.pending_nodes()
    assert engine._spec_build_inflight == set() and engine._spec_builds == {}
    assert producer.calls == 1


def test_producer_failure_marks_one_card_for_primary_serial_fallback(tmp_path, monkeypatch):
    producer = _Developer(error="isolated producer exploded")
    engine, _producer = _engine(tmp_path / "producer-serial-fallback", producer=producer)
    _start(engine)
    idea = _add_ready_draft(engine)
    _request(engine)
    _without_research(monkeypatch, engine)

    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    failed_prefix = engine.store.read_all()
    done = [event for event in failed_prefix if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1
    assert done[0].data["card_id"] == idea.card_id
    assert done[0].data["skipped"] == "producer_failed"
    assert engine._card_requires_serial_fallback(idea.card_id) is True

    # A producer failure is a durable handoff marker, not permission to elect and pay for the same
    # Card again on the isolated lane. The outer selector may still expose the receipt exactly once
    # to the ordinary primary Developer compatibility path.
    assert engine._request_card_build() is False
    assert len([
        event for event in engine.store.read_all()
        if event.type == EV_CARD_BUILD_REQUESTED
    ]) == 1
    state = fold(engine.store.read_all())
    actions = speculative_card_actions(state, engine.policy, engine.policy.max_nodes)
    assert len(actions) == 1 and actions[0]["_card_id"] == idea.card_id
    reservations = engine._claim_existing_card_builds(actions)
    assert reservations is not None and len(reservations) == 1

    engine._create_node(actions[0], reserved=reservations[0])

    final_events = engine.store.read_all()
    final_state = fold(final_events)
    assert producer.calls == 1
    assert engine.developer.calls == 1
    assert len(final_state.nodes) == 1
    assert final_state.nodes[0].status is NodeStatus.pending
    assert final_state.nodes[0].idea.card_id == idea.card_id
    assert len([event for event in final_events if event.type == EV_NODE_BUILDING]) == 1
    assert len([event for event in final_events if event.type == EV_NODE_CREATED]) == 1
    # Its evidence now owns the Card, so even a direct second primary claim fails closed.
    assert engine._claim_existing_card_builds(actions) is None


def test_orphan_producer_failed_done_cannot_force_serial_fallback(tmp_path):
    engine, _producer = _engine(tmp_path / "orphan-producer-failure")
    _start(engine)
    idea = _add_ready_draft(engine)

    engine.store.append(EV_CARD_BUILD_DONE, {
        "card_id": idea.card_id,
        "generation": 0,
        "skipped": "producer_failed",
    })

    assert fold(engine.store.read_all()).card_build_producer_failed == []
    assert engine._card_requires_serial_fallback(idea.card_id) is False


def test_raw_producer_exception_becomes_consumable_failure_result(tmp_path, monkeypatch):
    engine, producer = _engine(tmp_path / "raw-producer-error")
    _start(engine)
    engine._ensure_speculation_state()
    engine._spec_raw_stage_inflight = True
    events = engine.store.read_all()
    state = fold(events)

    def explode(*_args, **_kwargs):
        raise RuntimeError("raw producer exploded")

    monkeypatch.setattr(engine, "_prepare_raw_card_stage", explode)

    async def scenario():
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await engine._produce_raw_card_stage(
                {"kind": "draft"},
                events,
                state,
                0,
                engine._proposal_cue_fence(state),
                (engine.researcher, producer),
                send,
            )
            assert await receive.receive() == ("raw_proposal", state.search_epoch)

    anyio.run(scenario)

    assert engine._spec_raw_stage_inflight is False
    result = engine._spec_raw_stage_result
    assert result is not None and result.success is False
    assert result.error == "RuntimeError: raw producer exploded"
    assert engine._serve_raw_card_stage() == (True, False)
    assert engine._spec_raw_stage_result is None


def test_request_only_recovery_reruns_producer_without_duplicate_request(
    tmp_path, monkeypatch,
):
    run_dir = tmp_path / "request-only"
    first, _unused = _engine(run_dir)
    _start(first)
    _add_ready_draft(first)
    original_request = _request(first)
    assert not [event for event in first.store.read_all() if event.type == EV_NODE_CREATED]

    recovery_producer = _Developer()
    recovered, _producer = _engine(run_dir, producer=recovery_producer)
    _without_research(monkeypatch, recovered)

    async def _terminal_eval(node_id, _limiter, _max_es):
        node = fold(recovered.store.read_all()).nodes[node_id]
        recovered.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id,
            "generation": node.attempt,
            "metric": 0.0,
            "eval_seconds": 0.0,
        })

    monkeypatch.setattr(recovered, "_evaluate", _terminal_eval)
    anyio.run(
        recovered._run_card_session,
        [],
        fold(recovered.store.read_all()),
        None,
    )

    events = recovered.store.read_all()
    assert recovery_producer.calls == 1
    assert len([event for event in events if event.type == EV_CARD_BUILD_REQUESTED]) == 1
    assert len([event for event in events if event.type == EV_NODE_CREATED]) == 1
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 1
    done = next(event for event in events if event.type == EV_CARD_BUILD_DONE)
    assert done.data["card_id"] == original_request["card_id"]
    assert fold(events).card_builds_done == 1


def test_terminal_gate_explicitly_closes_request_only_crash_prefix(tmp_path):
    engine, _producer = _engine(tmp_path / "terminal-request-prefix")
    _start(engine)
    _add_ready_draft(engine)
    _request(engine)
    engine.store.append("run_abort", {"reason": "operator stop"})

    state = fold(engine.store.read_all())
    assert engine._head_request(state) is not None
    assert engine._close_card_build_before_terminal_gate(state) is True

    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1
    assert done[0].data["skipped"] == "stale"
    assert done[0].seq > next(event.seq for event in events if event.type == "run_abort")
    assert engine._head_request(fold(events)) is None


def test_delayed_producer_after_eval_terminal_closes_stale_without_late_claim(
    tmp_path, monkeypatch,
):
    producer = _DelayedSecondBuildDeveloper()
    engine, _producer = _engine(
        tmp_path / "terminal-before-producer",
        depth=2,
        producer=producer,
    )
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    admitted_node = _commit_speculative_node(engine)
    delayed_request = _request(engine)
    _without_research(monkeypatch, engine)

    scorer_consults = []
    claim_calls = []
    original_scorer = speculation_module.speculative_card_actions
    original_claim = engine._claim_requested_card_build

    def _tracked_scorer(*args, **kwargs):
        scorer_consults.append(True)
        return original_scorer(*args, **kwargs)

    def _tracked_claim(*args, **kwargs):
        claim_calls.append(True)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(speculation_module, "speculative_card_actions", _tracked_scorer)
    monkeypatch.setattr(engine, "_claim_requested_card_build", _tracked_claim)
    eval_recorded = anyio.Event()
    boundary = {}

    async def _terminal_eval(node_id, _limiter, _max_es):
        assert node_id == admitted_node
        assert engine._eval_resource_reservation(node_id, 0) is not None
        while not producer.second_started.is_set():
            await anyio.sleep(0)
        node = fold(engine.store.read_all()).nodes[node_id]
        terminal = engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id,
            "generation": node.attempt,
            "metric": 0.0,
            "eval_seconds": 0.0,
        })
        boundary["seq"] = terminal.seq
        eval_recorded.set()

    monkeypatch.setattr(engine, "_evaluate", _terminal_eval)

    async def _scenario():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                engine._run_card_session,
                [],
                fold(engine.store.read_all()),
                None,
            )
            try:
                with anyio.fail_after(5):
                    await eval_recorded.wait()
                    while engine._eval_resource_reservation(admitted_node, 0) is not None:
                        await anyio.sleep(0)
                assert producer.second_started.is_set()
                assert not [
                    event for event in engine.store.read_all()
                    if event.seq > boundary["seq"]
                    and event.type in {EV_NODE_BUILDING, EV_NODE_CREATED}
                ]
            finally:
                producer.release_second.set()

    anyio.run(_scenario)

    events = engine.store.read_all()
    delayed_done = [
        event for event in events
        if event.type == EV_CARD_BUILD_DONE
        and event.data.get("card_id") == delayed_request["card_id"]
        and event.data.get("generation") == delayed_request["generation"]
    ]
    assert len(delayed_done) == 1
    assert delayed_done[0].data["skipped"] == "stale"
    assert delayed_done[0].seq > boundary["seq"]
    assert scorer_consults == []
    assert claim_calls == []
    assert not [
        event for event in events
        if event.seq > boundary["seq"]
        and event.type in {EV_NODE_BUILDING, EV_NODE_CREATED}
    ]
    assert producer.calls == 2


def test_depth_one_prefetches_next_card_then_returns_at_outer_cadence_boundary(
    tmp_path, monkeypatch,
):
    engine, producer = _engine(tmp_path / "depth-one-overlap", depth=1)
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    _add_ready_draft(engine, "card-3", x=0.6)
    _request(engine)  # durable bootstrap request; not a new election by the session below
    _without_research(monkeypatch, engine)
    eval_started = anyio.Event()
    release_eval = anyio.Event()

    async def _held_eval(node_id, _limiter, _max_es):
        eval_started.set()
        await release_eval.wait()
        node = fold(engine.store.read_all()).nodes[node_id]
        if node.status is NodeStatus.pending:
            engine.store.append(EV_NODE_EVALUATED, {
                "node_id": node_id,
                "generation": node.attempt,
                "metric": float(node_id),
                "eval_seconds": 0.0,
            })

    monkeypatch.setattr(engine, "_evaluate", _held_eval)

    async def _scenario():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                engine._run_card_session,
                [],
                fold(engine.store.read_all()),
                None,
            )
            await eval_started.wait()
            # A request is the durable compute gate, not proof that the newly scheduled worker has
            # received a thread yet.  Under suite-wide thread-pool pressure an observer can see that
            # append before ``start_soon``'s child reaches ``run_sync``.  Hold the eval until the matching
            # done link lands: that is the actual "one next Card is ready" overlap contract.
            for _attempt in range(200):
                current_events = engine.store.read_all()
                requested = [
                    event for event in current_events
                    if event.type == EV_CARD_BUILD_REQUESTED
                ]
                completed = [
                    event for event in current_events
                    if event.type == EV_CARD_BUILD_DONE
                ]
                if len(requested) >= 2 and len(completed) >= 2:
                    break
                await anyio.sleep(0.01)
            assert len(requested) == 2
            assert requested[1].data["card_id"] == "card-2"
            assert len(completed) == 2
            release_eval.set()

    anyio.run(_scenario)

    events = engine.store.read_all()
    # Depth is a live backlog cap: one next Card is ready before the current eval ends. Once that
    # admitted eval reaches terminal, the session deliberately leaves the prebuilt Node pending and
    # returns so outer controls/Strategist/cadences run before another admission.
    assert producer.calls == 2
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 2
    assert sorted(node.status for node in fold(events).nodes.values()) == [
        NodeStatus.evaluated,
        NodeStatus.pending,
    ]


def test_session_stages_raw_policy_fallback_on_isolated_researcher_while_eval_runs(
    tmp_path, monkeypatch,
):
    engine, producer = _engine(tmp_path / "raw-steady-state", depth=1)
    engine._base_max_nodes = 3
    engine.policy.max_nodes = 3
    raw_researcher = _RawResearcher()
    # The raw proposal must use the leased producer pair, never the primary Researcher that may be
    # serving deep research/ordinary proposal state on the outer spine.
    engine._spec_role_pair = (raw_researcher, producer)
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _request(engine)
    _without_research(monkeypatch, engine)
    first_eval_started = anyio.Event()
    release_first_eval = anyio.Event()

    async def _eval(node_id, _limiter, _max_es):
        if node_id == 0:
            first_eval_started.set()
            await release_first_eval.wait()
        node = fold(engine.store.read_all()).nodes[node_id]
        if node.status is NodeStatus.pending:
            engine.store.append(EV_NODE_EVALUATED, {
                "node_id": node_id,
                "generation": node.attempt,
                "metric": float(node_id),
                "eval_seconds": 0.0,
            })

    monkeypatch.setattr(engine, "_evaluate", _eval)

    async def _scenario():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                engine._run_card_session,
                [],
                fold(engine.store.read_all()),
                None,
            )
            await first_eval_started.wait()
            for _attempt in range(200):
                current_events = engine.store.read_all()
                requests = [
                    event for event in current_events
                    if event.type == EV_CARD_BUILD_REQUESTED
                ]
                completed = [
                    event for event in current_events
                    if event.type == EV_CARD_BUILD_DONE
                ]
                if len(requests) >= 2 and len(completed) >= 2:
                    break
                await anyio.sleep(0.01)
            assert len(requests) >= 2
            assert len(completed) >= 2
            # The second Card did not exist before the session; its proposal and exact request both
            # completed while the first GPU child was still deliberately blocked.
            assert len([
                event for event in engine.store.read_all()
                if event.type == "card_added"
            ]) >= 2
            release_first_eval.set()

    anyio.run(_scenario)

    events = engine.store.read_all()
    assert raw_researcher.calls == 1
    assert producer.calls == 2
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 2
    state = fold(events)
    assert len(state.nodes) == 2
    assert state.nodes[0].status is NodeStatus.evaluated
    assert state.nodes[1].status is NodeStatus.pending


@pytest.mark.parametrize("raw_result", [None, "not-an-Idea"], ids=["none", "invalid"])
def test_rejected_raw_proposal_runs_once_then_returns_after_held_eval_boundary(
    tmp_path, monkeypatch, raw_result,
):
    engine, producer = _engine(tmp_path / f"raw-rejected-{raw_result is None}", depth=1)
    engine._base_max_nodes = 2
    engine.policy.max_nodes = 2
    raw_researcher = _RejectingRawResearcher(raw_result)
    engine._spec_role_pair = (raw_researcher, producer)
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _request(engine)
    _without_research(monkeypatch, engine)
    eval_started = anyio.Event()
    release_eval = anyio.Event()
    session_done = anyio.Event()

    async def _eval(node_id, _limiter, _max_es):
        eval_started.set()
        await release_eval.wait()
        node = fold(engine.store.read_all()).nodes[node_id]
        assert node.status is NodeStatus.pending
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id,
            "generation": node.attempt,
            "metric": 0.0,
            "eval_seconds": 0.0,
        })

    monkeypatch.setattr(engine, "_evaluate", _eval)

    async def _run_session():
        await engine._run_card_session(
            [],
            fold(engine.store.read_all()),
            None,
        )
        session_done.set()

    async def _scenario():
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_run_session)
            await eval_started.wait()
            for _attempt in range(200):
                if raw_researcher.calls == 1 and not engine._spec_raw_stage_inflight:
                    break
                await anyio.sleep(0.01)
            assert raw_researcher.calls == 1
            # Cross the session's finite 0.5s notification poll. A rejected paid proposal must have
            # set the outer-yield boundary, so it cannot be re-proposed while this eval remains held.
            await anyio.sleep(0.6)
            assert raw_researcher.calls == 1
            assert session_done.is_set() is False
            release_eval.set()
            with anyio.fail_after(2):
                await session_done.wait()

    anyio.run(_scenario)

    events = engine.store.read_all()
    state = fold(events)
    assert raw_researcher.calls == 1
    assert producer.calls == 1
    assert len([event for event in events if event.type == "card_added"]) == 1
    assert len([event for event in events if event.type == EV_CARD_BUILD_REQUESTED]) == 1
    assert len(state.nodes) == 1
    assert state.nodes[0].status is NodeStatus.evaluated
    assert engine._spec_raw_stage_inflight is False
    assert engine._spec_raw_stage_result is None


def test_raw_stage_authority_allows_llm_telemetry_but_rejects_other_tail_churn(tmp_path):
    engine, _producer = _engine(tmp_path / "raw-authority-tail", depth=1)
    _start(engine)
    engine._ensure_speculation_state()
    idea = Idea(
        operator="draft",
        params={"x": 0.4, "y": -1.0},
        rationale="stage only against the exact raw proposal prefix",
        hypothesis="tail churn invalidates isolated proposal authority",
    )

    def _result(events, state, audit_type, prepared_idea=idea):
        ceiling = engine._node_id_ceiling(events, state)
        return speculation_module.SpecRawStageResult(
            generation=state.search_epoch,
            action={"kind": "draft"},
            proposal_state=state,
            proposal_authority_seq=engine._proposal_authority_seq(events),
            proposal_node_ceiling=ceiling,
            at_node=ceiling,
            source="researcher",
            cue_fence=engine._proposal_cue_fence(state),
            success=True,
            idea=prepared_idea,
            audit_events=((audit_type, {"source": "raw-test"}, None, None),),
        )

    proposal_events = engine.store.read_all()
    proposal_state = fold(proposal_events)
    telemetry_result = _result(
        proposal_events,
        proposal_state,
        "raw_committed_audit_test",
    )
    # The raw worker's own accounting may land while its paid proposal is running. It advances the
    # physical tail but is deliberately excluded from selection authority.
    engine.store.append(EV_LLM_USAGE, {"usage_id": "raw-usage", "calls": 1})
    engine.store.append(EV_LLM_COST, {"cost": 0.01})
    engine._spec_raw_stage_result = telemetry_result

    assert engine._serve_raw_card_stage() == (True, True)
    committed_types = [event.type for event in engine.store.read_all()]
    assert committed_types.index("card_added") < committed_types.index(
        "raw_committed_audit_test"
    )

    stale_events = engine.store.read_all()
    stale_state = fold(stale_events)
    stale_idea = idea.model_copy(update={"params": {"x": 0.6, "y": -1.0}})
    stale_result = _result(
        stale_events,
        stale_state,
        "raw_stale_audit_test",
        stale_idea,
    )
    # This policy record deliberately changes none of the lifecycle/parent/cue fields. Unlike LLM
    # telemetry, it is authority-bearing and must invalidate the isolated RAW result all by itself.
    engine.store.append(EV_POLICY_DECISION, {
        "scores": {},
        "chosen": None,
        "reason": "benign tail churn after raw launch",
    })
    engine._spec_raw_stage_result = stale_result

    assert engine._serve_raw_card_stage() == (True, False)
    stale_types = [event.type for event in engine.store.read_all()]
    assert stale_types.count("card_added") == 1
    assert "raw_stale_audit_test" not in stale_types


def test_node_created_before_done_recovery_appends_only_missing_done(tmp_path):
    run_dir = tmp_path / "created-prefix"
    first, _producer = _engine(run_dir)
    _start(first)
    _add_ready_draft(first)
    request = _request(first)
    result = _build_result(first, request)
    outcome, node_id = first._claim_requested_card_build(request, result)
    assert outcome == "created" and node_id is not None
    assert not [event for event in first.store.read_all() if event.type == EV_CARD_BUILD_DONE]

    recovered, _unused = _engine(run_dir, isolated_roles=False)
    assert recovered._serve_card_builds() is True

    events = recovered.store.read_all()
    assert len([event for event in events if event.type == EV_NODE_BUILDING]) == 1
    assert len([event for event in events if event.type == EV_NODE_CREATED]) == 1
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data["node_id"] == node_id
    assert fold(events).card_builds_done == 1


def test_unlinked_speculative_node_waits_for_done_recovery_before_eval(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "done-cas-race")
    _start(engine)
    _add_ready_draft(engine)
    request = _request(engine)
    result = _build_result(engine, request)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    _without_research(monkeypatch, engine)

    original_done = engine._append_card_build_done
    done_calls = 0

    def _flaky_done(request_value, *, node_id=None, skipped=None):
        nonlocal done_calls
        done_calls += 1
        if done_calls == 1 and node_id is not None:
            return False
        return original_done(request_value, node_id=node_id, skipped=skipped)

    evaluated = []

    async def _linked_eval(node_id, _limiter, _max_es):
        state = fold(engine.store.read_all())
        assert engine._speculative_link_matches(state, state.nodes[node_id])
        evaluated.append(node_id)
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": node_id,
            "generation": state.nodes[node_id].attempt,
            "metric": 0.0,
            "eval_seconds": 0.0,
        })

    monkeypatch.setattr(engine, "_append_card_build_done", _flaky_done)
    monkeypatch.setattr(engine, "_evaluate", _linked_eval)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    state = fold(engine.store.read_all())
    assert done_calls >= 2
    assert evaluated == [0]
    assert state.card_builds_done == 1
    assert state.nodes[0].status is NodeStatus.evaluated


def test_reopened_epoch_is_request_and_marker_generation_not_node_attempt(tmp_path):
    engine, _producer = _engine(tmp_path / "reopened")
    _start(engine)
    _add_ready_draft(engine)
    engine.store.append(EV_RUN_FINISHED, {"reason": "budget"})
    engine.store.append(EV_RUN_REOPENED, {})
    assert fold(engine.store.read_all()).search_epoch == 1

    request = _request(engine)
    assert request["generation"] == 1
    result = _build_result(engine, request)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    state = fold(engine.store.read_all())
    node_id = next(iter(state.speculative_nodes))
    node = state.nodes[node_id]
    assert node.attempt == 0
    assert node.card_build_generation == 1
    assert state.speculative_nodes[node_id] == {
        "card_id": request["card_id"], "generation": 1,
    }


def test_node_building_crash_is_terminalized_then_request_is_explicitly_skipped(tmp_path):
    engine, _producer = _engine(tmp_path / "building-prefix")
    _start(engine)
    idea = _add_ready_draft(engine)
    request = _request(engine)
    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 0,
        "operator": "draft",
        "parent_ids": [],
        "card_id": idea.card_id,
        "speculative": True,
        "card_build_generation": request["generation"],
    })
    crashed = fold(engine.store.read_all())
    assert 0 in crashed.buildings

    assert engine._recover_interrupted_builds(crashed) is True
    recovered = fold(engine.store.read_all())
    assert not recovered.buildings
    assert recovered.cards[idea.card_id].status == "dropped"
    failed = [event for event in engine.store.read_all() if event.type == EV_NODE_FAILED]
    assert len(failed) == 1 and failed[0].data["reason"] == "build_interrupted"

    # Recovery proves that the worker died. Re-running the durable head now fails closed because its
    # immutable Card was dropped, and the give-up receipt advances the queue instead of wedging it.
    result = _build_result(engine, request)
    assert result.success is False
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data["skipped"] == "producer_failed"
    assert not [event for event in events if event.type == EV_NODE_CREATED]
    assert fold(events).card_builds_done == 1


def test_precoded_developer_sentinel_commits_failure_and_pause_in_one_tail_cas(
    tmp_path, monkeypatch,
):
    producer = _Developer(code="(developer error: backend unavailable)")
    engine, _producer = _engine(tmp_path / "sentinel-atomic", producer=producer)
    _start(engine)
    _add_ready_draft(engine)
    request = _request(engine)
    result = _build_result(engine, request)
    assert result.success is True

    terminal_appends = []
    append_many = engine.store.append_many

    def _record_append_many(records, **kwargs):
        if [event_type for event_type, _payload in records] == [EV_NODE_FAILED, EV_PAUSE]:
            terminal_appends.append((records, kwargs.get("expected_last_seq")))
        return append_many(records, **kwargs)

    monkeypatch.setattr(engine.store, "append_many", _record_append_many)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    events = engine.store.read_all()
    created = next(event for event in events if event.type == EV_NODE_CREATED)
    failed = [event for event in events if event.type == EV_NODE_FAILED]
    pauses = [event for event in events if event.type == EV_PAUSE]
    assert len(terminal_appends) == len(failed) == len(pauses) == 1
    assert terminal_appends[0][1] == created.seq
    assert failed[0].seq == created.seq + 1
    assert pauses[0].seq == failed[0].seq + 1
    assert failed[0].data["reason"] == "developer_crash"
    assert pauses[0].data["node_id"] == failed[0].data["node_id"]
    assert pauses[0].data["generation"] == failed[0].data["generation"] == 0
    state = fold(events)
    assert state.nodes[created.data["node_id"]].status is NodeStatus.failed
    assert state.paused is True


def test_terminal_developer_sentinel_recovery_pauses_once_and_not_after_resume(tmp_path):
    engine, _producer = _engine(tmp_path / "sentinel-terminal", isolated_roles=False)
    _start(engine)
    engine.store.append(EV_NODE_CREATED, {
        "node_id": 0,
        "parent_ids": [],
        "operator": "draft",
        "idea": {"operator": "draft", "hypothesis": "developer failed"},
        "code": "(developer error: backend unavailable)",
        "files": {},
    })
    engine.store.append(EV_NODE_FAILED, {
        "node_id": 0,
        "generation": 0,
        "error": "(developer error: backend unavailable)",
        "reason": "developer_crash",
        "eval_seconds": 0.0,
    })
    # A pause for another lifecycle is not the exact durable acknowledgement recovery needs.
    engine.store.append(EV_PAUSE, {
        "node_id": 0,
        "generation": 1,
        "reason": "stale auto-pause",
    })
    assert not fold(engine.store.read_all()).paused

    assert anyio.run(engine._close_developer_sentinel_once) is True
    assert anyio.run(engine._close_developer_sentinel_once) is False
    events = engine.store.read_all()
    exact_pauses = [
        event for event in events
        if event.type == EV_PAUSE
        and event.data.get("node_id") == 0
        and event.data.get("generation") == 0
    ]
    assert len(exact_pauses) == 1
    assert len([event for event in events if event.type == EV_NODE_FAILED]) == 1
    assert fold(events).paused is True

    engine.store.append(EV_RESUME, {})
    assert fold(engine.store.read_all()).paused is False
    assert anyio.run(engine._close_developer_sentinel_once) is False
    assert len([
        event for event in engine.store.read_all()
        if event.type == EV_PAUSE
        and event.data.get("node_id") == 0
        and event.data.get("generation") == 0
    ]) == 1


def test_developer_sentinel_never_reserves_resources_or_reaches_evaluate(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "sentinel", isolated_roles=False)
    _start(engine)
    engine.store.append(EV_NODE_CREATED, {
        "node_id": 0,
        "parent_ids": [],
        "operator": "draft",
        "idea": {"operator": "draft", "hypothesis": "developer failed"},
        "code": "(developer error: backend unavailable)",
        "files": {},
    })
    calls = {"reserve": 0, "evaluate": 0}

    def _forbidden_reserve(_node):
        calls["reserve"] += 1
        raise AssertionError("a Developer sentinel must not reserve GPU resources")

    async def _forbidden_evaluate(*_args):
        calls["evaluate"] += 1
        raise AssertionError("a Developer sentinel must not enter evaluation")

    monkeypatch.setattr(engine, "_try_reserve_node_resources", _forbidden_reserve)
    monkeypatch.setattr(engine, "_evaluate", _forbidden_evaluate)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    state = fold(engine.store.read_all())
    assert calls == {"reserve": 0, "evaluate": 0}
    assert state.nodes[0].status is NodeStatus.failed
    assert state.nodes[0].error_reason == "developer_crash"
    assert state.paused is True


def test_session_quiescence_waits_for_surviving_build_marker(tmp_path, monkeypatch):
    engine, _producer = _engine(tmp_path / "quiescence", isolated_roles=False)
    _start(engine)
    engine.store.append(EV_NODE_BUILDING, {
        "node_id": 0, "operator": "draft", "parent_ids": [],
    })
    calls = 0

    async def _recovery_checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            engine.store.append(EV_NODE_FAILED, {
                "node_id": 0,
                "generation": 0,
                "error": "test recovery closed the reservation",
                "reason": "build_interrupted",
                "eval_seconds": 0.0,
            })
        return True

    # The first progress checkpoint deliberately leaves the marker alive. A quiescence latch that
    # watches only pending Nodes/requests/in-memory producers would return immediately with calls==1.
    monkeypatch.setattr(engine, "_close_developer_sentinel_once", _recovery_checkpoint)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    assert calls == 2
    assert not fold(engine.store.read_all()).buildings


def test_outer_spine_runs_freshness_gate_before_policy_scorer():
    source = inspect.getsource(Engine._run_with_llm_broker)
    scorer = source.index("actions = self._select_actions(state)")
    freshness = source.rfind("await self._drop_stale_speculation()", 0, scorer)
    assert freshness >= 0


def test_stage_prepared_card_id_lock_contains_only_the_tail_cas_append():
    source = textwrap.dedent(inspect.getsource(Engine._stage_prepared_card))
    tree = ast.parse(source)
    lock_blocks = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Attribute)
            and item.context_expr.attr == "_id_lock"
            for item in node.items
        )
    ]

    assert len(lock_blocks) == 1
    locked_calls = [
        node for node in ast.walk(lock_blocks[0])
        if isinstance(node, ast.Call)
    ]
    assert [ast.unparse(call.func) for call in locked_calls] == ["self.store.append"]
    assert any(keyword.arg == "expected_last_seq" for keyword in locked_calls[0].keywords)
    locked_source = ast.get_source_segment(source, lock_blocks[0]) or ""
    assert "self.store.append(" in locked_source
    assert "self.store.read_all(" not in locked_source
    assert "fold(" not in locked_source
    assert "_plan_native_card(" not in locked_source


def test_raw_action_selection_and_worker_share_one_proposal_snapshot():
    source = textwrap.dedent(inspect.getsource(Engine._run_card_session))
    tree = ast.parse(source)
    selections = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "speculative_raw_actions"
    ]
    launches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start_soon"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "_produce_raw_card_stage"
    ]

    assert len(selections) == 1
    assert len(launches) == 1
    selection = selections[0]
    launch = launches[0]
    assert ast.unparse(selection.args[0]) == "proposal_state"
    assert [ast.unparse(arg) for arg in launch.args[2:4]] == [
        "proposal_events",
        "proposal_state",
    ]

    snapshot_assignments = {
        ast.unparse(node.targets[0]): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"proposal_events", "proposal_state"}
        and node.lineno < selection.lineno
    }
    assert ast.unparse(snapshot_assignments["proposal_events"].value) == (
        "self.store.read_all()"
    )
    assert ast.unparse(snapshot_assignments["proposal_state"].value) == (
        "fold(proposal_events)"
    )
    assert snapshot_assignments["proposal_events"].lineno < (
        snapshot_assignments["proposal_state"].lineno
    ) < selection.lineno < launch.lineno

    rereads = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_all"
        and selection.lineno < node.lineno < launch.lineno
    ]
    assert rereads == []


def test_session_rechecks_freshness_after_reservation_and_before_gpu_child(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "pre-gpu")
    _start(engine)
    _add_ready_draft(engine)
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    answers = iter((True, False, False))
    checks = []

    def _fresh(*args, **kwargs):
        checks.append((args, kwargs))
        return next(answers)

    async def _forbidden_evaluate(*_args):
        raise AssertionError("stale speculation reached the GPU child")

    monkeypatch.setattr(speculation_module, "speculative_card_is_fresh", _fresh)
    monkeypatch.setattr(engine, "_evaluate", _forbidden_evaluate)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    state = fold(engine.store.read_all())
    assert len(checks) == 3  # loop-entry, pre-GPU, then the terminalizing fresh fold
    assert state.nodes[node_id].status is NodeStatus.failed
    assert state.nodes[node_id].error_reason == "superseded"
    assert state.nodes[node_id].error == CARD_FRESHNESS_SUPERSEDED_ERROR
    assert state.nodes[node_id].eval_seconds == 0.0


def test_resumed_zero_gpu_engine_reruns_freshness_and_drops_now_stale_pin(tmp_path):
    run_dir = tmp_path / "resume-freshness"
    first, _producer = _engine(run_dir)
    first._gpu_ids = [0]
    first._gpu_physical_ids = {0: "0"}
    first._gpu_mem = {0: 16_000}
    first._free_gpus = [0]
    _start(first)
    _add_ready_draft(first)
    first.store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-7",
        "gpus": 1,
        "gpu_mem_mib": 8_000,
        "source": "operator",
        "pinned": True,
    })
    node_id = _commit_speculative_node(first)

    # The pin is fresh against the original one-GPU envelope.
    assert anyio.run(first._drop_stale_speculation) is False
    first.store.append(EV_PAUSE, {"reason": "operator pause"})
    resumed_at = first.store.append(EV_RESUME, {})
    assert fold(first.store.read_all()).paused is False

    # A fresh process redetects a zero-GPU envelope. The durable positive pin stays positive and is
    # now unavailable, so resume must run freshness again and close the unevaluated speculation.
    resumed, _unused = _engine(run_dir)
    assert resumed._gpu_ids == []
    assert anyio.run(resumed._drop_stale_speculation) is True
    assert anyio.run(resumed._drop_stale_speculation) is False

    events = resumed.store.read_all()
    failed = [
        event for event in events
        if event.type == EV_NODE_FAILED and event.data.get("node_id") == node_id
    ]
    assert len(failed) == 1 and failed[0].seq > resumed_at.seq
    assert failed[0].data["reason"] == "superseded"
    assert failed[0].data["eval_seconds"] == 0.0
    node = fold(events).nodes[node_id]
    assert node.status is NodeStatus.failed
    assert node.error == CARD_FRESHNESS_SUPERSEDED_ERROR


def test_freshness_drop_keeps_physical_slot_spent_until_add_nodes(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "freshness-physical-slot")
    engine._base_max_nodes = 1
    engine.policy.max_nodes = 1
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    dropped_node = _commit_speculative_node(engine)

    monkeypatch.setattr(
        speculation_module,
        "speculative_card_is_fresh",
        lambda *_args, **_kwargs: False,
    )
    assert anyio.run(engine._drop_stale_speculation) is True
    dropped = fold(engine.store.read_all())
    assert dropped.nodes[dropped_node].status is NodeStatus.failed
    assert dropped.nodes[dropped_node].error_reason == "superseded"

    # The exact freshness failure is absent from the Card policy count, but its historical node id
    # still spends the only physical reservation slot. Selection therefore cannot mint a replacement
    # until the operator explicitly extends the hard ceiling.
    engine._refresh_speculation_budget(dropped)
    assert card_budget_used(dropped) == 0
    assert engine._node_reservation_slots_remaining(dropped) == 0
    request_count = len(dropped.card_build_requests)
    assert engine._request_card_build() is False
    assert len(fold(engine.store.read_all()).card_build_requests) == request_count

    engine.store.append(EV_BUDGET_EXTEND, {"add_nodes": 1})
    extended = fold(engine.store.read_all())
    assert engine._node_reservation_slots_remaining(extended) == 1
    assert engine._request_card_build() is True
    requested = fold(engine.store.read_all())
    assert len(requested.card_build_requests) == request_count + 1
    assert engine._speculation_depth_used(requested) == 1


def _mark_producer_failed(engine: Engine, card_id: str, *, x: float) -> None:
    """Register one durable producer give-up via the exact request→producer_failed done handoff.

    The card must be the sole election candidate at call time so the request head is deterministic.
    """
    _add_ready_draft(engine, card_id, x=x)
    request = _request(engine)
    assert request["card_id"] == card_id
    engine.store.append(EV_CARD_BUILD_DONE, {
        "card_id": card_id,
        "generation": request["generation"],
        "skipped": "producer_failed",
    })
    assert engine._card_requires_serial_fallback(card_id) is True


def test_drop_stale_speculation_excludes_producer_failed_from_freshness_set(
    tmp_path, monkeypatch,
):
    """A durable producer-failed card is serial-fallback-only; it must never compete inside the
    freshness counterfactual. If it did, it would outrank the healthy committed speculative node
    and drop it as ``superseded`` — the exact lane-collapse this exclusion prevents. The election
    (`_request_card_build`) already unions producer-failed ids; this revalidation must match it."""
    engine, _producer = _engine(tmp_path / "drop-stale-producer-failed")
    _start(engine)
    _mark_producer_failed(engine, "card-pf", x=0.15)
    _add_ready_draft(engine, "card-live", x=0.25)
    _commit_speculative_node(engine)

    captured: dict[str, set[str]] = {}

    def _capture(*_args, excluded_card_ids, **_kwargs):
        captured["excluded"] = set(excluded_card_ids)
        return True  # keep the node alive; we only inspect the election set it was checked against

    monkeypatch.setattr(speculation_module, "speculative_card_is_fresh", _capture)

    assert anyio.run(engine._drop_stale_speculation) is False
    # The committed speculative card was always excluded; the producer-failed id must be too.
    assert captured["excluded"] == {"card-live", "card-pf"}


def test_claim_requested_card_build_excludes_producer_failed_but_keeps_the_claimed_card(
    tmp_path, monkeypatch,
):
    """The claim revalidation unions producer-failed ids like the election, but must discard the
    exact card being committed now — its head result is landing, so a prior speculative give-up on
    that same id cannot exclude it from its own claim."""
    engine, _producer = _engine(tmp_path / "claim-producer-failed")
    _start(engine)
    _mark_producer_failed(engine, "card-pf", x=0.15)
    _add_ready_draft(engine, "card-live", x=0.25)
    request = _request(engine)
    assert request["card_id"] == "card-live"
    result = _build_result(engine, request)

    captured: dict[str, set[str]] = {}
    real_actions = speculation_module.speculative_card_actions

    def _capture(*args, excluded_card_ids, **kwargs):
        captured["excluded"] = set(excluded_card_ids)
        return real_actions(*args, excluded_card_ids=excluded_card_ids, **kwargs)

    monkeypatch.setattr(speculation_module, "speculative_card_actions", _capture)

    outcome, node_id = engine._claim_requested_card_build(request, result)
    assert outcome == "created" and node_id is not None
    assert "card-pf" in captured["excluded"]       # serial-fallback-only card stays excluded
    assert "card-live" not in captured["excluded"]  # ...but never the card being claimed now


def test_run_card_session_pre_gpu_recheck_unions_producer_failed_but_raw_lane_does_not():
    """Source-parity tripwire for the two `_run_card_session` counterfactual consults that cannot be
    reached without a live GPU dispatch: the pre-GPU freshness recheck shares the election set
    (producer-failed excluded), while the raw-proposal lane deliberately keeps producer-failed cards
    IN — a producer-failed card legitimately owns that counterfactual and must fall through to the
    serial builder rather than restage as an unbuildable raw action."""
    source = textwrap.dedent(inspect.getsource(Engine._run_card_session))
    tree = ast.parse(source)

    def _excluded_src(callee: str) -> str | None:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == callee
            ):
                for keyword in node.keywords:
                    if keyword.arg == "excluded_card_ids":
                        return ast.unparse(keyword.value)
        return None

    fresh_src = _excluded_src("speculative_card_is_fresh")
    raw_src = _excluded_src("speculative_raw_actions")
    assert fresh_src is not None and "_producer_failed_card_ids" in fresh_src
    assert raw_src is not None and "_producer_failed_card_ids" not in raw_src


def test_speculative_admission_releases_old_pin_and_rescans_current_pin(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "speculative-pin-race")
    engine._gpu_ids = [0]
    engine._gpu_physical_ids = {0: "0"}
    engine._gpu_mem = {0: 16_000}
    engine._free_gpus = [0]
    _start(engine)
    _add_ready_draft(engine)
    engine.store.append(EV_CARD_RESOURCE_PINNED, {
        "id": "card-7",
        "gpus": 1,
        "gpu_mem_mib": 8_000,
        "source": "operator",
        "pinned": True,
    })
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)

    original_reserve = engine._try_reserve_node_resources
    original_release = engine._release_gpus
    reserve_pins = []
    releases = []
    admitted = []

    def _racing_reserve(node, *, resource_pin=None):
        reserve_pins.append(dict(resource_pin or {}))
        reservation = original_reserve(node, resource_pin=resource_pin)
        if len(reserve_pins) == 1:
            assert reservation is not None and reservation["gpu_ids"] == [0]
            engine.store.append(EV_CARD_RESOURCE_PINNED, {
                "id": "card-7",
                "gpus": 0,
                "source": "operator",
                "pinned": True,
            })
        return reservation

    def _tracked_release(gpu_ids):
        releases.append(list(gpu_ids or []))
        original_release(gpu_ids)

    async def _terminal_eval(admitted_id, _limiter, _max_es):
        reservation = engine._eval_resource_reservation(admitted_id, 0)
        admitted.append((admitted_id, reservation, list(engine._free_gpus)))
        node = fold(engine.store.read_all()).nodes[admitted_id]
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": admitted_id,
            "generation": node.attempt,
            "metric": 0.0,
            "eval_seconds": 0.0,
        })

    monkeypatch.setattr(engine, "_try_reserve_node_resources", _racing_reserve)
    monkeypatch.setattr(engine, "_release_gpus", _tracked_release)
    monkeypatch.setattr(engine, "_evaluate", _terminal_eval)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    assert [pin["gpus"] for pin in reserve_pins] == [1, 0]
    assert releases[0] == [0]
    assert admitted and admitted[0][0] == node_id
    current_reservation = admitted[0][1]
    assert current_reservation is not None
    assert current_reservation["count"] == 0
    assert current_reservation["cpu_only"] is True
    assert current_reservation["gpu_ids"] == []
    assert admitted[0][2] == [0]
    assert fold(engine.store.read_all()).cards["card-7"].resource_pin == {
        "gpus": 0,
        "pinned_by": "operator",
    }


def _model_node(
    node_id: int,
    *,
    parents: tuple[int, ...] = (),
    status: NodeStatus = NodeStatus.evaluated,
    metric: float | None = 0.5,
    card_id: str | None = None,
) -> Node:
    return Node(
        id=node_id,
        parent_ids=list(parents),
        operator="draft" if not parents else "improve",
        idea=Idea(
            operator="draft" if not parents else "improve",
            hypothesis=f"hypothesis {card_id or node_id}",
            card_id=card_id,
        ),
        status=status,
        metric=metric,
    )


def _model_card(card_id: str, *, owned_by: int | None = None) -> Card:
    card = Card(
        id=card_id,
        statement=f"proposal {card_id}",
        seed_statement=f"proposal {card_id}",
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
        scored_against_empty=False,
    )
    if owned_by is None:
        return card
    return card.model_copy(deep=True, update={
        "status": "running",
        "verdict": "testing",
        "evidence": [owned_by],
        "selection_provenance": card.selection_provenance.model_copy(
            update={"owner_state": "in_flight"},
        ),
        "selection_blockers": ["work_in_flight"],
        "selection_ready": False,
    })


class _PopulationPolicy:
    n_seeds = 0
    debug_depth = 0
    card_select_k = 2
    max_nodes = 5

    def next_actions(self, _state):
        return [{"kind": "improve", "parent_id": 0}]

    def card_score(self, _state, card, *, scoring):
        del scoring
        return 0, (2.0 if card.id == "rank-one" else 1.0,)


def test_population_n_minus_one_member_remains_fresh_at_engine_gate(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "population")
    state = RunState(
        direction="max",
        nodes={
            0: _model_node(0, metric=0.9),
            2: _model_node(
                2,
                parents=(0,),
                status=NodeStatus.pending,
                metric=None,
                card_id="subject",
            ),
        },
        best_node_id=0,
        cards={
            "subject": _model_card("subject", owned_by=2),
            "rank-one": _model_card("rank-one"),
        },
        speculative_nodes={2: {"card_id": "subject", "generation": 7}},
    )
    state.nodes[2].speculative = True
    state.nodes[2].card_build_generation = 7
    engine.policy = _PopulationPolicy()
    engine._base_max_nodes = 5
    before = len(engine.store.read_all())
    monkeypatch.setattr(speculation_module, "fold", lambda _events: state)

    assert anyio.run(engine._drop_stale_speculation) is False
    assert len(engine.store.read_all()) == before
    assert state.nodes[2].status is NodeStatus.pending


def test_running_speculative_eval_is_never_freshness_dropped_and_burns_terminal(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "burn-terminal")
    _start(engine)
    _add_ready_draft(engine)
    node_id = _commit_speculative_node(engine)

    def _forbidden_freshness(*_args, **_kwargs):
        raise AssertionError("freshness must not reconsider an already-running eval")

    monkeypatch.setattr(
        speculation_module,
        "speculative_card_is_fresh",
        _forbidden_freshness,
    )

    async def _drop_while_running():
        return await engine._drop_stale_speculation(eval_inflight={(node_id, 0)})

    assert anyio.run(_drop_while_running) is False
    assert not [event for event in engine.store.read_all() if event.type == EV_NODE_FAILED]

    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": node_id,
        "generation": 0,
        "metric": 0.1,
        "eval_seconds": 1.0,
    })
    node = fold(engine.store.read_all()).nodes[node_id]
    assert node.status is NodeStatus.evaluated and node.eval_seconds == 1.0


def test_no_isolated_pair_prevents_election_and_gives_up_replayed_head(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "no-pair", isolated_roles=False)
    _start(engine)
    idea = _add_ready_draft(engine)

    assert engine._request_card_build() is False
    assert not [
        event for event in engine.store.read_all()
        if event.type == EV_CARD_BUILD_REQUESTED
    ]

    # A request may have been written by an earlier process whose isolated role pool is unavailable
    # after restart. The main task must make that durable head terminal instead of waiting forever.
    engine.store.append(EV_CARD_BUILD_REQUESTED, {
        "card_id": idea.card_id,
        "generation": fold(engine.store.read_all()).search_epoch,
    })
    _without_research(monkeypatch, engine)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data["skipped"] == "producer_failed"
    assert fold(events).card_builds_done == 1
    assert not [event for event in events if event.type == EV_NODE_CREATED]


def test_request_and_claim_tail_cas_retries_do_not_duplicate_lifecycle(
    tmp_path, monkeypatch,
):
    engine, _producer = _engine(tmp_path / "cas")
    _start(engine)
    _add_ready_draft(engine)
    original_append = engine.store.append
    raced = {"request": False, "building": False}

    def _racing_append(event_type, data=None, **kwargs):
        if event_type == EV_CARD_BUILD_REQUESTED and not raced["request"]:
            raced["request"] = True
            original_append("test_tail_moved", {"at": "request"})
        elif event_type == EV_NODE_BUILDING and not raced["building"]:
            raced["building"] = True
            original_append("test_tail_moved", {"at": "claim"})
        return original_append(event_type, data, **kwargs)

    monkeypatch.setattr(engine.store, "append", _racing_append)
    assert engine._request_card_build() is False
    request = _request(engine)
    result = _build_result(engine, request)
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result

    assert engine._serve_card_builds() is False  # node_building lost its first tail CAS
    assert result.key in engine._spec_builds
    assert engine._serve_card_builds() is True

    events = engine.store.read_all()
    assert raced == {"request": True, "building": True}
    assert len([event for event in events if event.type == EV_CARD_BUILD_REQUESTED]) == 1
    assert len([event for event in events if event.type == EV_NODE_BUILDING]) == 1
    assert len([event for event in events if event.type == EV_NODE_CREATED]) == 1
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 1
    assert fold(events).card_builds_done == 1
