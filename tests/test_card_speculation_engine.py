"""Layer-5 engine contracts for request-driven Card speculation.

The pure counterfactual selector has its own focused suite.  These tests pin the execution seam:
the event log is the queue, producer work is event-free, only the main task commits a speculative
Node, and every crash prefix is either resumed or explicitly given up without duplicating work.
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import inspect
import threading
import textwrap
import time
from pathlib import Path

import anyio
import pytest

import looplab.engine.speculation as speculation_module
import looplab.search.speculation_quality as speculation_quality
from tests._source_scan import called_names, function_tree, names_read
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
    card_ownership_receipt,
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
    EV_NODE_EVAL_STARTED,
    EV_NODE_FAILED,
    EV_PAUSE,
    EV_POLICY_DECISION,
    EV_RESUME,
    EV_RUN_FINISHED,
    EV_RUN_REOPENED,
    EV_RUNG_PROMOTED,
    EV_SPECULATION_DEPTH_SETTLED,
)
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    META_CARD_ID,
    SpeculativeSelectionContext,
    card_budget_used,
    card_lane_width,
    speculative_card_actions,
    speculative_card_is_fresh,
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
    """The depth arithmetic: exact `(id, attempt)` admission, and no capacity from a wrong one.

    The lane is declared THREE Cards wide here, and that declaration is load-bearing rather than
    scaffolding. `_speculative_prefetch_ceiling` is `min(depth, card_lane_width)`, because a
    prefetch beyond the width of the set `speculative_card_is_fresh` proves membership in is bought
    and then discarded — measured on the default `greedy` width of 1, this exact fixture at depth 3
    committed three Nodes of which the gate reported ONE fresh and two born stale, i.e. two paid
    Developer calls for nothing. Widening the lane is what makes depth 3 a treatment the run can
    actually spend, and it leaves this test's real subject — the counting — untouched.
    """

    engine, producer = _engine(tmp_path / "depth-three", depth=3)
    engine.policy.card_select_k = 3
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


def test_recovery_head_with_an_unreconciled_attempt_is_quarantined_not_reissued(tmp_path):
    """The durable request identifies LOGICAL work; it cannot say whether a provider already accepted
    and billed a call for it. A head carrying an attempt receipt that no live producer owns is
    therefore ambiguous: restarting a producer would buy the same Developer/Researcher work twice with
    nothing in the log to show for the first purchase."""
    from looplab.events.types import EV_CARD_BUILD_ATTEMPTED

    engine, producer = _engine(tmp_path / "attempt-quarantine")
    _start(engine)
    _add_ready_draft(engine)
    request = _request(engine)
    key = engine._request_key(request)
    engine._ensure_speculation_state()

    # Without an attempt the ALIVE head stays open — a producer may still be started for it.
    assert engine._serve_card_builds() is False
    assert fold(engine.store.read_all()).card_builds_done == 0

    # The dead process got as far as starting a producer: its receipt is at this head's position.
    state = fold(engine.store.read_all())
    engine.store.append(EV_CARD_BUILD_ATTEMPTED, {
        "card_id": key[0], "generation": key[1], "index": state.card_builds_done,
    })

    assert engine._serve_card_builds() is True
    events = engine.store.read_all()
    done = [event for event in events if event.type == EV_CARD_BUILD_DONE]
    assert len(done) == 1 and done[0].data.get("skipped") == "producer_failed"
    assert producer.calls == 0, "quarantine must never re-issue the possibly-charged provider work"
    assert not [event for event in events if event.type == EV_NODE_BUILDING]
    final = fold(events)
    assert final.card_builds_done == 1 and engine._head_request(final) is None
    # Serial-fallback-only from here: the Card is still buildable, just never speculatively re-elected.
    assert engine._card_requires_serial_fallback(key[0]) is True


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
    # This scenario needs TWO prefetches live at once, so the lane has to be two Cards wide:
    # `_speculative_prefetch_ceiling` is `min(depth, card_lane_width)`, and at greedy's width of 1
    # the second election is refused rather than bought-and-discarded. Nothing about the delayed
    # producer / stale-close contract below changes with the width.
    engine.policy.card_select_k = 2
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
    # Depth is a live backlog cap: one next Card is ready before the current eval ends.
    #
    # THE BOUNDARY THIS PINS IS THE PAID ONE, and until 2026-08-24 the assertion below claimed a
    # different boundary that never existed. It read "the session deliberately leaves the prebuilt
    # Node pending and returns so outer controls/Strategist/cadences run before another admission",
    # and the prebuilt node really did stay pending — but not because any rule said so.
    # `CardSessionLanes.open_for_production` closes PAID producer work on the first terminal (a
    # provider call started after it would hold the session open for its whole duration), while
    # `open_for_admission` closes only on `stopping`. What actually stopped the already-built node
    # from being dispatched was `core/cards.py::card_score_fence_state`'s empty-authority clause
    # marking its card stale the moment the first node scored — a phantom staleness that also
    # killed NINE of the ten nodes ever terminalized `superseded` across `runs/`.
    #
    # With that clause gone the prefetch does what a prefetch is for: work already built and paid
    # for gets dispatched instead of idling until the next outer turn. The boundary that matters is
    # unchanged and the two assertions above are what say so — the producer was NOT called a third
    # time and no third `card_build_done` was written, i.e. the session started no new paid work
    # after the terminal. Only the fate of the node it had already built moved.
    assert producer.calls == 2
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 2
    assert sorted(node.status for node in fold(events).nodes.values()) == [
        NodeStatus.evaluated,
        NodeStatus.evaluated,
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
    # Node 1 was `pending` here until 2026-08-24 for the same reason it was in the prefetch test
    # above — the empty-authority freshness clause, not a session rule. The three assertions this
    # test really makes are the ones that did not move: ONE raw proposal, TWO producer calls, TWO
    # build-done rows. No paid work crossed the boundary; a node already built simply ran.
    assert state.nodes[1].status is NodeStatus.evaluated


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


def test_a_stopping_session_commits_its_paid_raw_stage_and_buys_no_new_producer(tmp_path, monkeypatch):
    """The raw-stage phase's two halves answer to different gates, and only one of them is new work.

    COMMITTING the prepared proposal is ungated on purpose: it is already paid for, and
    `_spec_raw_stage_result` counts in `_card_phase_decide_exit`'s `memory_pending`, so a stopping
    session that declined to drain it could not leave. ELECTING a Card and starting its head producer
    is new producer work, and `gates.stopping` — a terminal intent, an exhausted eval budget, a
    pending outer rebuild — has to close it exactly as it closes `_card_phase_request_build` two
    phases below. The budget is the stopping condition driven here because it moves no log tail.
    """

    def _staged(run_dir):
        engine, _producer = _engine(run_dir, depth=1)
        _start(engine)
        engine._ensure_speculation_state()
        events = engine.store.read_all()
        state = fold(events)
        ceiling = engine._node_id_ceiling(events, state)
        engine._spec_raw_stage_result = speculation_module.SpecRawStageResult(
            generation=state.search_epoch,
            action={"kind": "draft"},
            proposal_state=state,
            proposal_authority_seq=engine._proposal_authority_seq(events),
            proposal_node_ceiling=ceiling,
            at_node=ceiling,
            source="researcher",
            cue_fence=engine._proposal_cue_fence(state),
            success=True,
            idea=Idea(operator="draft", params={"x": 0.4, "y": -1.0},
                      rationale="a proposal this run has already paid for",
                      hypothesis="a stopping session still commits it"),
            audit_events=(),
        )
        elections: list[dict] = []
        monkeypatch.setattr(engine, "_request_card_build",
                            lambda **kw: (elections.append(kw), False)[1])
        return engine, elections

    stopping, stopping_elections = _staged(tmp_path / "raw-stage-stopping")
    session = speculation_module.CardSession(max_eval_seconds=0.0, wall_deadline=None)
    assert engine_gates_stopping(stopping, session) is True
    stopping._card_phase_serve_raw_stage(session)
    assert [event.type for event in stopping.store.read_all()].count("card_added") == 1, (
        "the paid proposal was dropped instead of committed")
    assert stopping_elections == [], "a stopping session bought a Card build"
    assert session.progressed is True and session.yield_outer is True

    # The CONTROL, same phase, same staged result: nothing about this is a refusal to serve a raw
    # stage — only the budget differs.
    running, running_elections = _staged(tmp_path / "raw-stage-running")
    open_session = speculation_module.CardSession(max_eval_seconds=None, wall_deadline=None)
    assert engine_gates_stopping(running, open_session) is False
    running._card_phase_serve_raw_stage(open_session)
    assert [event.type for event in running.store.read_all()].count("card_added") == 1
    assert len(running_elections) == 1, "an open session stopped electing the Card it just staged"


def engine_gates_stopping(engine, session) -> bool:
    """The phase's own gate input, read the way the phase reads it."""

    return engine._session_gates(engine._session_state(), session).stopping


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


def test_admission_records_the_eval_start_boundary_before_it_starts_the_worker(
    tmp_path, monkeypatch,
):
    """The durable half of `eval_inflight` is written at the dispatch decision, by the MAIN task.

    DRIVEN, then pinned. Until doc 25 EC-02 this was three ordered `source.index()` lookups over
    `_run_card_session`, and CLAUDE.md records exactly how little they bought: a
    `pass  # self._record_eval_start_boundary(chosen)` satisfies all three, in order, while the
    boundary event is never written — the defect that turned depth-1 speculation serial. So the
    property is now EXERCISED (the eval child observes its own `eval_started` already durable), and
    the ordering pin that survives is AST-based, where a comment is not a node.
    """
    engine, _producer = _engine(tmp_path / "boundary-order")
    _start(engine)
    _add_ready_draft(engine)
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    observed = []

    async def _observing_eval(admitted_id, _limiter, _max_es):
        node = fold(engine.store.read_all()).nodes[admitted_id]
        observed.append((admitted_id, node.eval_started))
        engine.store.append(EV_NODE_EVALUATED, {
            "node_id": admitted_id,
            "generation": node.attempt,
            "metric": 0.0,
            "eval_seconds": 0.0,
        })

    monkeypatch.setattr(engine, "_evaluate", _observing_eval)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )
    assert observed == [(node_id, True)], (
        "the eval child ran before its `node_eval_started` row was durable")

    calls = called_names(Engine._card_phase_admit_evals)
    boundary = calls.index("self._record_eval_start_boundary")
    admitted = calls.index("session.eval_inflight.add")
    # `eval_task_group`, not `task_group`: since backlog F1f the eval child belongs to the
    # RUN-scoped group `Engine.run` owns, so it survives the session that admitted it. The
    # ORDER pinned here is unchanged and is the invariant-#1 half — the boundary row is still
    # written by the MAIN task at the dispatch decision, before the child exists.
    started = calls.index("session.eval_task_group.start_soon")
    assert boundary < admitted < started
    launches = [
        node for node in ast.walk(function_tree(Engine._card_phase_admit_evals))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start_soon"
    ]
    assert len(launches) == 1
    assert ast.unparse(launches[0].args[0]) == "self._card_eval_one"


def test_a_worker_written_eval_start_boundary_would_defeat_the_election(tmp_path, monkeypatch):
    """Why the boundary is not appended from the eval worker.

    `_request_card_build` reads the log, consults the Card scorer, then appends the request with
    `expected_last_seq`. ANY row appended inside that window makes the election lose its CAS — and
    since the session then has nothing to report as progress, it just waits for the eval to finish.
    Appending the boundary from `_evaluate` did exactly that once per eval and silently turned
    depth-1 speculation SERIAL: measured on the calibration workload, the treatment lane went from
    17 builds / 5 discards with real producer/consumer overlap to 12 / 0 with none.
    """
    engine, _producer = _engine(tmp_path / "cas-window")
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    node_id = _commit_speculative_node(engine)
    node = fold(engine.store.read_all()).nodes[node_id]

    # PRODUCTION ORDER: main task, at admission, strictly before the election. The overlap survives.
    assert engine._record_eval_start_boundary(node) is True
    assert fold(engine.store.read_all()).nodes[node_id].eval_started is True
    assert engine._request_card_build(consumed_inflight={(node_id, 0)}) is True
    # ...and it is written at most once, so a re-dispatched still-pending node appends nothing.
    assert engine._record_eval_start_boundary(
        fold(engine.store.read_all()).nodes[node_id]) is False

    # THE HAZARD, reproduced exactly: the same row landing inside the election window.
    racing, _unused = _engine(tmp_path / "cas-window-racing")
    _start(racing)
    _add_ready_draft(racing, "card-1", x=0.2)
    _add_ready_draft(racing, "card-2", x=0.8)
    racing_node = _commit_speculative_node(racing)
    original = speculation_module.speculative_card_actions

    def _scorer_with_a_concurrent_worker(*args, **kwargs):
        racing.store.append(
            EV_NODE_EVAL_STARTED, {"node_id": racing_node, "generation": 0})
        return original(*args, **kwargs)

    monkeypatch.setattr(
        speculation_module, "speculative_card_actions", _scorer_with_a_concurrent_worker)
    assert racing._request_card_build(consumed_inflight={(racing_node, 0)}) is False


def test_outer_spine_runs_freshness_gate_before_policy_scorer():
    source = inspect.getsource(Engine._run_with_llm_broker)
    scorer = source.index("actions = self._select_actions(state)")
    # The call now carries `eval_inflight=self._eval_inflight` (backlog F1f: the outer loop
    # turns while adopted evaluations run, so this drain must not terminalize a node whose
    # sandbox is burning right now). Pin the CALL and its position, not the empty arg list.
    freshness = source.rfind("await self._drop_stale_speculation(", 0, scorer)
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
    # The raw lane moved into `_card_phase_request_build` with doc 25 EC-02's decomposition; the
    # property is unchanged, and this snapshot pair is deliberately NOT routed through the session's
    # per-tail `_fold_current` memo — it is the proposal's own authority snapshot.
    source = textwrap.dedent(inspect.getsource(Engine._card_phase_request_build))
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


def test_freshness_drop_refunds_its_physical_slot_and_add_nodes_still_extends(
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
    assert dropped.nodes[dropped_node].never_evaluated is True

    # The build never reached a sandbox, so it spends nothing: it is absent from the Card policy
    # count AND its physical reservation is refunded, so selection may immediately mint a
    # replacement. Without the second half the refund was inert — the id allocator kept the slot.
    engine._refresh_speculation_budget(dropped)
    assert card_budget_used(dropped) == 0
    assert engine._node_reservation_slots_remaining(dropped) == 1
    request_count = len(dropped.card_build_requests)
    assert engine._request_card_build() is True
    requested = fold(engine.store.read_all())
    assert len(requested.card_build_requests) == request_count + 1
    assert engine._speculation_depth_used(requested) == 1

    # The refund is one slot per discarded build, not a blank cheque: the outstanding request now
    # owns the refunded slot, and only an explicit operator extension adds another.
    assert engine._node_reservation_slots_remaining(requested) == 0
    engine.store.append(EV_BUDGET_EXTEND, {"add_nodes": 1})
    extended = fold(engine.store.read_all())
    assert engine._node_reservation_slots_remaining(extended) == 1


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

    def _capture(*_args, context, **_kwargs):
        captured["excluded"] = set(context.excluded_card_ids)
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

    def _capture(*args, context, **kwargs):
        captured["excluded"] = set(context.excluded_card_ids)
        return real_actions(*args, context=context, **kwargs)

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
    def _excluded_src(func, callee: str) -> str | None:
        """The `excluded_card_ids` SOURCE this callee is consulted with, read out of the session
        object it is bundled into (doc 25 SE-14). Reading it from anywhere else in the function
        would let the two lanes silently converge on one set again, which is what this pins.

        The two consults live in two different phase methods since doc 25 EC-02 split the turn body;
        each is still scanned inside the ONE phase that owns it, so a lane that quietly grew a second
        consult somewhere else is still invisible to this scan and still a failure below."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == callee
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg != "context":
                    continue
                context = keyword.value
                assert (isinstance(context, ast.Call)
                        and isinstance(context.func, ast.Name)
                        and context.func.id == "SpeculativeSelectionContext"), (
                    f"{callee} no longer builds its session inline; this scan cannot see the set")
                for inner in context.keywords:
                    if inner.arg == "excluded_card_ids":
                        return ast.unparse(inner.value)
                return ""       # a session that names no exclusions is not a missing session
        return None

    fresh_src = _excluded_src(Engine._card_phase_admit_evals, "speculative_card_is_fresh")
    raw_src = _excluded_src(Engine._card_phase_request_build, "speculative_raw_actions")
    assert fresh_src is not None and "_producer_failed_card_ids" in fresh_src
    assert raw_src is not None and "_producer_failed_card_ids" not in raw_src


# --- doc 25 EC-02: one exit-gate predicate, one fold per observed tail ----------------------------
#
# The turn body used to spell its three fold-derived stop conditions out FOUR times per iteration,
# each copy preceded by its own full `fold(store.read_all())`, and combine them with the two live
# session flags in five more places. Neither half fails loudly when it drifts: a gate that
# disagrees with the copy two lines below silently starts speculative work another phase has
# already given up on, and a snapshot taken before an append silently answers a later phase with
# the pre-append world. These four tests hold both halves.


def test_open_for_new_work_is_the_one_exit_gate_predicate():
    """The rule, stated — now as the TWO gates backlog F1f split it into.

    There used to be one predicate, `open_for_new_work`, and both live session flags closed it for
    both lanes. `consumer_completed` (now `boundary_owed`) is set in the `finally` of EVERY eval
    child, so the FIRST terminal shut eval ADMISSION for every remaining slot while
    `_card_phase_decide_exit` still refused to return until the LAST eval drained: 115.6 GPU-h of
    idle second slot across the six width-2 runs on this box. The flags never said anything about
    the consumer. They say the outer boundary is owed a turn, and the answer to that is to RETURN.
    """
    gates_open = speculation_module.CardSessionGates(
        terminal_gate=False, budget_exhausted=False, outer_rebuild=False)
    assert gates_open.stopping is False

    def _session(**overrides):
        session = speculation_module.CardSession(
            max_eval_seconds=None, wall_deadline=None)
        for name, value in overrides.items():
            setattr(session, name, value)
        return session

    # The single predicate is GONE, not renamed. A reverting patch that restores the old spelling
    # has to delete this line to go green.
    assert not hasattr(_session(), "open_for_new_work")

    assert _session().open_for_admission(gates_open) is True
    assert _session().open_for_production(gates_open) is True
    # Each FOLD-derived condition closes BOTH gates on its own...
    for name in ("terminal_gate", "budget_exhausted", "outer_rebuild"):
        closed = dataclasses.replace(gates_open, **{name: True})
        assert closed.stopping is True
        assert _session().open_for_admission(closed) is False
        assert _session().open_for_production(closed) is False
    # ...and each LIVE session flag closes PRODUCTION only. They are read separately and not frozen
    # into the snapshot because `boundary_owed` is transferred from an eval child at any checkpoint.
    # THE ASYMMETRY IS THE FIX: a freed slot is refilled by the very turn that observed the
    # terminal, and the debt is paid by returning, not by going sterile.
    for flag in ("boundary_owed", "yield_outer"):
        assert _session(**{flag: True}).open_for_production(gates_open) is False
        assert _session(**{flag: True}).open_for_admission(gates_open) is True

    # The eval-seconds and wall-clock halves of `budget_exhausted`, which no call site can reach.
    state = RunState()
    state.total_eval_seconds = 10.0
    assert _session(max_eval_seconds=None).budget_exhausted(state) is False
    assert _session(max_eval_seconds=10.5).budget_exhausted(state) is False
    assert _session(max_eval_seconds=10.0).budget_exhausted(state) is True
    assert _session(wall_deadline=time.time() + 600).budget_exhausted(state) is False
    assert _session(wall_deadline=time.time() - 1).budget_exhausted(state) is True

    # `slots=True` is load-bearing: every one of these fields used to be a `nonlocal`, and a
    # misspelled assignment would bind a NEW name and leave the real gate open for the whole run.
    with pytest.raises(AttributeError):
        _session().yeild_outer = True


def test_no_session_phase_re_derives_a_stop_condition_by_hand():
    """One home for the gate tuple, and one home for combining it with the live session flags.

    The second half pins WHICH GATE EACH PHASE ASKS, which is the whole of the F1f fix. Admission
    asks `open_for_admission` (the fold-derived stop conditions, and nothing else); every producer
    site asks `open_for_production` (those plus the two live flags). Swapping one for the other in a
    single phase is exactly how the 115.6 GPU-h barrier comes back, and it is a one-word edit.

    `_card_phase_admit_evals` also still consults `gates.stopping` directly inside an admitted
    batch: re-reading a live flag there would let the first sibling to terminate truncate the batch
    its own siblings are still being admitted into — the same defect one scope smaller, and the one
    this subsystem has already paid for once (depth-1 speculation silently going serial).
    """
    tree = ast.parse(
        Path(speculation_module.__file__).read_text(encoding="utf-8-sig", errors="replace"))
    built = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CardSessionGates"
    ]
    assert len(built) == 1, "the gate tuple is built somewhere other than `_session_gates`"

    def _owner(target: ast.AST) -> str:
        """The nearest enclosing def of *target*."""
        best = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if inner is target:
                        best = node.name
        return best

    # Reading a live session flag is how a second exit-gate predicate grows. Only the production
    # predicate itself may. `open_for_admission` is deliberately absent from this set: a live flag
    # reaching the ADMISSION gate is the F1f defect, restated.
    #
    # `_card_phase_serve_raw_stage` used to be here too, and that was the defect: it spelled out two
    # of `open_for_production`'s three conjuncts by hand and dropped `gates.stopping`, so a session
    # with a terminal intent, an exhausted budget or a pending outer rebuild still elected a Card and
    # bought a paid build. Its COMMIT of the already-paid raw stage is still ungated, deliberately —
    # that is how a stopping run finishes cleanly — and what it may no longer do is start new work.
    flag_readers = {
        _owner(node) for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"boundary_owed", "yield_outer"}
        and isinstance(node.ctx, ast.Load)
    }
    # `_card_phase_decide_exit` reads them to tell the two closing reasons APART — a terminal hands
    # back unconditionally, a RECURRING producer yield is rate-limited on the log tail so the outer
    # loop and a fresh session cannot ping-pong for the length of an evaluation. That distinction
    # cannot be made from `open_for_production`, which folds both into one boolean.
    assert flag_readers == {
        "open_for_production", "_card_phase_decide_exit",
    }, flag_readers

    # WHICH GATE EACH PHASE ASKS. One entry per phase-owned call site; changing a row here is
    # changing the barrier. `_card_phase_admit_evals` is the only consumer site and the only one
    # that may ask `open_for_admission`.
    gate_calls = collections.Counter(
        (_owner(node), node.func.attr) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"open_for_admission", "open_for_production"}
    )
    assert gate_calls == collections.Counter({
        ("_card_phase_admit_evals", "open_for_admission"): 1,
        # the stale drain, the build-result commit and the head producer
        ("_card_phase_drop_stale", "open_for_production"): 1,
        ("_card_phase_serve_head", "open_for_production"): 2,
        # the election + head producer the raw-stage phase starts AFTER committing its own paid
        # proposal; the commit itself is above the gate and stays there
        ("_card_phase_serve_raw_stage", "open_for_production"): 1,
        # the freshness miss inside admission defers its DISCARD (not its refusal to start)
        ("_card_phase_admit_evals", "open_for_production"): 1,
        ("_card_phase_request_build", "open_for_production"): 1,
        ("_card_phase_decide_exit", "open_for_production"): 1,
    }), gate_calls

    # COUNTS, not just the owner set: the admission phase reads `.stopping` at BOTH of its
    # gates — the batch fill and the pre-GPU re-check — and reverting only one of them back to
    # `open_for_new_work` leaves the owner set unchanged. (Verified: that partial reversion passed
    # an earlier set-only version of this assertion.)
    stopping_readers = collections.Counter(
        _owner(node) for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "stopping"
    )
    assert stopping_readers == collections.Counter({
        "open_for_admission": 1, "open_for_production": 1, "_card_phase_admit_evals": 2,
        # …and the exit phase, whose rate limit on a REPEAT producer yield must never suppress a
        # fold-derived stop: an operator pause, the eval-second budget and the wall deadline all
        # arrive without moving the log tail, so the clause has to exclude them explicitly.
        "_card_phase_decide_exit": 1,
    }), stopping_readers
    # ...and the phase asks its own gate exactly once: the batch boundary, on entry.
    assert called_names(
        Engine._card_phase_admit_evals).count("session.open_for_admission") == 1

    phases = [
        getattr(Engine, name) for name in dir(Engine)
        if name.startswith("_card_phase_")
    ]
    assert len(phases) >= 5
    for phase in phases:
        calls = set(called_names(phase))
        names = names_read(phase)
        assert "self._terminal_intent" not in calls, phase.__name__
        assert "session.budget_exhausted" not in calls, phase.__name__
        assert "needs_outer_rebuild" not in names | calls, phase.__name__


def test_the_turn_snapshot_folds_once_per_observed_tail(tmp_path, monkeypatch):
    """`_fold_current` memoizes the PURE function `fold` on an unchanged log prefix — nothing else.

    Engine invariant 4 forbids caching derived state across loop iterations WITHOUT re-folding; the
    log is still read on every call here, and any append by any writer moves the tail and forces a
    real rebuild on the very next one.
    """
    engine, _producer = _engine(tmp_path / "fold-memo")
    _start(engine)
    folded: list[int] = []
    real_fold = speculation_module.fold

    def _counting(events):
        folded.append(len(events))
        return real_fold(events)

    monkeypatch.setattr(speculation_module, "fold", _counting)

    events, first = engine._fold_current()
    assert len(folded) == 1 and len(events) == folded[0]
    _again_events, again = engine._fold_current()
    assert again is first and len(folded) == 1        # same tail: no rebuild

    engine.store.append(EV_PAUSE, {"reason": "the tail moved"})
    _after_events, after = engine._fold_current()
    assert len(folded) == 2 and after is not first
    assert after.paused is True                       # ...and the new fold carries the append

    # The `fold` module global is a patch seam. A memo that outlived a swap would answer the new
    # function's caller with the old one's state — a test that still runs and no longer measures.
    sentinel = RunState()
    monkeypatch.setattr(speculation_module, "fold", lambda _events: sentinel)
    assert engine._fold_current()[1] is sentinel


def test_a_turn_that_appends_refolds_before_every_later_phase_reads(tmp_path, monkeypatch):
    """The turn-scope half of invariant 4, DRIVEN rather than pinned.

    Phase one (`_close_developer_sentinel_once`) appends an operator-visible pause. Every phase
    below it in the SAME turn must therefore observe a paused run: the freshness drain must not be
    consulted, the pending Node must not be admitted, and the session must close after one turn. A
    decomposition that handed the phases a snapshot taken before the append would drain and admit
    against the pre-pause world and none of the three assertions below would hold.
    """
    engine, _producer = _engine(tmp_path / "refold-after-append")
    _start(engine)
    _add_ready_draft(engine)
    node_id = _commit_speculative_node(engine)
    _without_research(monkeypatch, engine)
    turns = {"n": 0}
    drains: list[bool] = []
    admitted: list[int] = []

    async def _pausing_recovery():
        turns["n"] += 1
        if turns["n"] == 1:
            engine.store.append(EV_PAUSE, {"reason": "a phase that appends"})
            return True
        return False

    real_drop = engine._drop_stale_speculation

    async def _recording_drop(**kwargs):
        drains.append(True)
        return await real_drop(**kwargs)

    async def _recording_eval(admitted_id, _limiter, _max_es):
        admitted.append(admitted_id)

    monkeypatch.setattr(engine, "_close_developer_sentinel_once", _pausing_recovery)
    monkeypatch.setattr(engine, "_drop_stale_speculation", _recording_drop)
    monkeypatch.setattr(engine, "_evaluate", _recording_eval)
    anyio.run(
        engine._run_card_session,
        [],
        fold(engine.store.read_all()),
        None,
    )

    assert turns["n"] == 1, "the pause appended in phase one did not close the session's own turn"
    assert drains == [], "the freshness drain ran against the pre-pause snapshot"
    assert admitted == [], "a Node was admitted against the pre-pause snapshot"
    assert fold(engine.store.read_all()).nodes[node_id].status is NodeStatus.pending


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


def test_every_speculative_spawn_rolls_back_its_inflight_marker_on_a_failed_start():
    """Both speculative spawns set an inflight marker BEFORE `start_soon`, and the marker is only
    cleared by the spawned coroutine's own `finally`. If `start_soon` raises — the task group is
    already closing — that `finally` never runs, `_ensure_speculation_state` only initializes MISSING
    attrs, and the stuck marker keeps every session-exit gate counting it in `memory_pending`: the
    next `_run_card_session` can never reach a break condition and polls forever. Structural pin,
    because reaching a raising `start_soon` through a live card session is not unit-drivable."""
    import ast
    from pathlib import Path

    import looplab.engine.speculation as speculation

    tree = ast.parse(Path(speculation.__file__).read_text(encoding="utf-8"))
    spawns = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "start_soon"]
    assert len(spawns) >= 2, len(spawns)

    def _guarded(call: ast.Call) -> bool:
        """True when `call` sits inside a `try` whose handler catches BaseException."""
        for candidate in ast.walk(tree):
            if not isinstance(candidate, ast.Try):
                continue
            if not any(child is call for child in ast.walk(candidate)):
                continue
            for handler in candidate.handlers:
                name = getattr(handler.type, "id", None)
                if name in ("BaseException", "Exception"):
                    return True
        return False

    unguarded = [call.lineno for call in spawns if not _guarded(call)]
    assert not unguarded, (
        "speculative start_soon with no BaseException rollback of its inflight marker at "
        f"line(s) {unguarded} — a failed spawn would strand the marker and wedge the next session")


def test_a_paid_result_survives_a_close_that_exhausts_its_cas_retries(tmp_path, monkeypatch):
    """The in-memory result must outlive a FAILED close, or the paid producer work is lost.

    `_append_card_build_done` gives up after 64 CAS attempts against a tail that keeps moving; it
    returns False and the durable head stays OPEN. If the result were discarded before that close,
    the next service turn would find a head with an unreconciled attempt row and no owning
    producer, and close it as "producer_failed" — permanently barring a Card whose producer had in
    fact SUCCEEDED. Keeping the result until the close is durable lets the retry commit the exact
    paid work instead.
    """
    engine, _producer = _engine(tmp_path / "cas-exhausted")
    _start(engine)
    _add_ready_draft(engine)
    request = _request(engine)
    result = _build_result(engine, request)
    assert result.success is True
    engine._ensure_speculation_state()
    engine._spec_builds[result.key] = result

    original_append = engine.store.append

    def _always_race_the_close(event_type, data=None, **kwargs):
        # Move the tail immediately before every DONE append so its `expected_last_seq` is stale.
        if event_type == EV_CARD_BUILD_DONE:
            original_append("test_tail_moved", {"at": "done"})
        return original_append(event_type, data, **kwargs)

    monkeypatch.setattr(engine.store, "append", _always_race_the_close)

    assert engine._serve_card_builds() is False          # the close never landed
    state = fold(engine.store.read_all())
    assert state.card_builds_done < len(state.card_build_requests)   # head still open
    assert result.key in engine._spec_builds, (
        "the paid producer result was discarded even though its close failed — the next service "
        "turn would quarantine a head whose producer had succeeded")

    # With the tail settled, the retry commits the SAME result: one node, one close, no rebuild.
    monkeypatch.setattr(engine.store, "append", original_append)
    assert engine._serve_card_builds() is True
    assert result.key not in engine._spec_builds
    events = engine.store.read_all()
    assert len([event for event in events if event.type == EV_CARD_BUILD_DONE]) == 1
    assert len([event for event in events if event.type == EV_NODE_CREATED]) == 1
    assert fold(events).card_builds_done == 1


# --------------------------------------------------------------------------- #
# Defect A: a producer_failed Card whose serial claim also refuses must not free-spin the run.
# Live evidence: /tmp/ll-s1/spec — 74 loop turns inside one second, then "stuck: 1 action(s)
# planned for 75 consecutive loop turns without creating a node" at 2 of 8 nodes, where the same
# command with `-s speculation_depth=0` reached 8 of 8.
# --------------------------------------------------------------------------- #


def _unclaimable_card(engine: Engine, card_id: str) -> None:
    """Register one durable Card whose stored action is NOT a fixed point of `Idea`'s validators.

    Reproduces exactly what the live run wrote: a `params` value its own `space` grid forbids, which
    `Idea` snaps back on every reconstruction, so `_prepare_existing_card_claim` compares the rebuilt
    action against the receipt, disagrees, and refuses — identically, forever.

    THE SHIPPED WRITER CAN NO LONGER PRODUCE THIS. `_card_added_payload` now proves the round trip at
    the mint and refuses, which is the fix for the two producers that used to reach it. What these
    tests own is the other half — the retirement ladder that has to work on a durable log a PRE-FIX
    writer already wrote — so forge that log directly: the receipt is genuinely recomputed over the
    skewed action, exactly as the old writer's `card_ownership_receipt(…)` call did, so the fold's own
    digest check still passes and only the Idea REBUILD disagrees. Minting a healthy Card and then
    editing the durable bytes would fail the digest check instead, at a different guard.
    """
    idea = Idea(operator="draft", params={"x": 0.25, "y": -1.0},
                rationale=f"unclaimable {card_id}",
                hypothesis=f"unclaimable {card_id} improves the objective",
                card_id=card_id)
    action = Engine._card_action(idea, [], {}, None, None, scored_against_empty=True)
    statement = Engine._card_statement(idea)
    assert statement is not None
    payload = Engine._card_added_payload(
        card_id, statement, action, idea, source="researcher", at_node=0)
    # The grid the old writer stored beside those params. `x=0.25` is outside `[0.8, 0.9]`, so every
    # reconstruction of this Card snaps it to 0.8 and the rebuilt action stops matching the receipt.
    skewed = {**action, "space": {"x": [0.8, 0.9]}}
    payload["idea"]["space"] = skewed["space"]
    payload["ownership_receipt"] = card_ownership_receipt(card_id, statement, skewed)
    assert payload["ownership_receipt"] is not None
    engine.store.append("card_added", payload)


def test_serial_claim_names_a_permanent_refusal_instead_of_returning_a_bare_none(tmp_path):
    engine, _producer = _engine(tmp_path / "claim-refusal-named", depth=0)
    _start(engine)
    _unclaimable_card(engine, "card-bad")
    state = fold(engine.store.read_all())
    actions = engine._select_actions(state)
    assert [action.get("_card_id") for action in actions] == ["card-bad"]

    assert engine._claim_existing_card_builds(actions) is None
    assert "card-bad" in (engine._card_claim_refusal or "")
    assert "cannot be rebuilt" in engine._card_claim_refusal
    # It is DETERMINISTIC: re-scoring the same lane cannot change the answer, which is why the loop
    # spun. Prove the permanence rather than assuming it.
    assert engine._claim_existing_card_builds(engine._select_actions(state)) is None


def test_a_permanently_unclaimable_card_lane_retires_instead_of_spinning(tmp_path):
    engine, _producer = _engine(tmp_path / "claim-refusal-retires", depth=0)
    _start(engine)
    _unclaimable_card(engine, "card-bad")
    state = fold(engine.store.read_all())
    actions = engine._select_actions(state)
    ids = [action["_card_id"] for action in actions]

    for turn in range(1, Engine._CARD_CLAIM_RETIRE_AFTER):
        assert engine._claim_existing_card_builds(actions) is None
        # A refusal is legitimately transient (a lost tail CAS, a moved selection); the first ones
        # must NOT throw away a real work item.
        assert engine._note_card_claim_refusal(ids) is False, f"retired too eagerly on turn {turn}"
        assert fold(engine.store.read_all()).cards["card-bad"].status != "dropped"

    assert engine._claim_existing_card_builds(actions) is None
    assert engine._note_card_claim_refusal(ids) is True
    retired = fold(engine.store.read_all())
    assert retired.cards["card-bad"].status == "dropped"
    assert "unclaimable after" in (retired.cards["card-bad"].dropped_reason or "")
    # …and the board has moved on: selection now plans fresh RAW work instead of re-electing it.
    follow_on = engine._select_actions(retired)
    assert follow_on and all("_card_id" not in action for action in follow_on)


def test_a_successful_claim_clears_the_refusal_ledger(tmp_path):
    """The ledger counts CONSECUTIVE refusals of one lane. A claim that lands is progress, so a
    later unrelated refusal must start from zero — otherwise a long healthy run would eventually
    retire a Card for refusals spread across hours."""
    engine, _producer = _engine(tmp_path / "claim-refusal-resets", depth=0)
    _start(engine)
    _unclaimable_card(engine, "card-bad")
    state = fold(engine.store.read_all())
    assert engine._note_card_claim_refusal(["card-bad"]) is False
    assert engine._note_card_claim_refusal(["card-bad"]) is False
    engine._card_claim_refusal_lane = None      # what the create branch does after a claim lands
    engine._card_claim_refusal_turns = 0
    assert engine._note_card_claim_refusal(["card-bad"]) is False
    assert fold(engine.store.read_all()).cards["card-bad"].status != "dropped"


def test_the_stuck_terminal_names_the_producer_give_up_the_budget_already_recorded(tmp_path):
    """`budget.speculation` recorded `producer_failed: 1` in the very log whose terminal said only
    "no node was created". The terminal must name the cause the engine already knew."""
    engine, _producer = _engine(tmp_path / "stall-diagnosis")
    _start(engine)
    _mark_producer_failed(engine, "card-pf", x=0.15)
    state = fold(engine.store.read_all())
    engine._card_claim_refusal = "card-pf is no longer a live selectable Card"

    why = engine._create_stall_diagnosis([{META_CARD_ID: "card-pf", "kind": "draft"}], state)
    assert "producer_failed" in why and "card-pf" in why
    assert "no longer a live selectable Card" in why


def test_a_ratcheted_run_resumes_through_the_real_pin_check(tmp_path, monkeypatch):
    """AUTO's own adaptation must not read as an OPERATOR disagreement and fail closed.

    `_require_pinned_speculation_receipt` refuses a resume whose depth differs from the one the log
    pinned — that is invariant #6 and it is what keeps a config edit from changing a live run's search
    treatment. A depth that MOVES has to pass through the same check, and it does, because the check
    reads the run's two depth facts SEPARATELY: an AUTO re-entry adopts the effective depth
    (`run_started` ∧ every settle row) and a SPELLED one is measured against the launch pin
    (`speculation_depth_pinned`). Reading only the folded effective depth for both is what made this
    run refuse its own printed resume advice. This drives the real method rather than asserting the
    arithmetic, because the failure it guards against is exactly "the check and the value drifted
    apart".
    """
    engine, _producer = _engine(tmp_path / "ratchet-resume")
    _start(engine)
    assert engine._speculation_enabled() is True
    engine._speculation_depth_auto = True          # AUTO is what run_started's depth 1 came from

    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 0, "previous": 1})
    entry = fold(engine.store.read_all())
    assert entry.speculation_depth == 0

    # A second process over the same run dir: AUTO re-resolves 1 off this box, and must adopt 0.
    resumed, _second = _engine(tmp_path / "ratchet-resume")
    resumed._speculation_depth_auto = True
    assert resumed.speculation_depth == 1
    resumed._require_pinned_speculation_receipt(entry)      # must NOT raise
    assert resumed.speculation_depth == 0
    assert resumed._speculation_enabled() is False


def test_a_diagnostic_row_cannot_discard_a_paid_proposal():
    """The proposal fence must ignore EVERY diagnostic event, not just the two LLM accounting rows.

    `_proposal_authority_seq` is captured BEFORE the slow paid `_prepare_node_idea` and compared for
    EQUALITY at commit, so any row appended in that window discards a proposal the run has already
    paid a Developer call for — and reports it as "a control/research/lifecycle event won the CAS",
    which is the one thing it was not. `train_monitor_alert` and the two ASHA rows are ON by default
    and fire on a TIMER from concurrent evals, so they land in that window as a matter of course.

    This also pins the retraction of a claim that was written into `types.py` and CLAUDE.md: a
    fold-ignored event is NOT splice-neutral "by construction", because the fold is not the only
    reader. It was true of the fold and false of this fence.
    """
    from looplab.engine.speculation import SpeculationMixin
    from looplab.events.eventstore import Event
    from looplab.events.types import DIAGNOSTIC_EVENTS, SETUP_THREAD_APPENDABLE

    def ev(kind, seq):
        return Event(v=1, seq=seq, ts=0.0, type=kind, data={})

    base = [ev("node_created", 1)]
    assert SpeculationMixin._proposal_authority_seq(base) == 1

    # EVERY diagnostic type, from the registry rather than a hand-copied list — a diagnostic event
    # added later must inherit this property without anyone remembering to add it here.
    for kind in sorted(DIAGNOSTIC_EVENTS):
        assert SpeculationMixin._proposal_authority_seq(base + [ev(kind, 2)]) == 1, (
            f"{kind} moved the proposal fence; a concurrent watchdog tick now discards a paid "
            "Developer call and blames the CAS")

    # The ONE folded pair that is also excluded, and the only one that may be. It is written from an
    # eval WORKER THREAD (`_ensure_run_setup`), so since backlog F1f made the outer loop turn while
    # adopted evaluations run it is the only authority-bearing row that can land inside a main-task
    # reservation's CAS window — every other concurrent writer is an anyio task on the loop the
    # reservation is blocking. It is admissible because it is the only folded pair whose
    # splice-position neutrality this repo has PROVEN (`tests/test_setup_thread_appendable.py`): the
    # fold keys both rows purely by command, so neither can change which action the policy chooses.
    for kind in sorted(SETUP_THREAD_APPENDABLE):
        assert SpeculationMixin._proposal_authority_seq(base + [ev(kind, 2)]) == 1, (
            f"{kind} moved the proposal fence; the first eval's setup thread now discards a paid "
            "Developer call from a concurrent outer turn")

    # …and the fence still moves for anything that really does carry selection authority, or it
    # would stop protecting the thing it exists for. `node_evaluated` is the load-bearing member:
    # a node terminal moves `best`, the parent snapshot and every Card score, so the exclusion above
    # is NOT a precedent for it.
    for kind in ("node_evaluated", "node_failed", "pause", "policy_decision", "card_added"):
        assert SpeculationMixin._proposal_authority_seq(base + [ev(kind, 2)]) == 2, (
            f"{kind} no longer moves the fence — a real selection change would be committed over")


def test_the_card_build_span_names_its_request_and_the_node_names_that_trace(tmp_path):
    """THE BUILD IS PART OF THE NODE (F7) — the two writer facts a reader cannot infer.

    Measured on `runs/rubertlite-dr-unified-v7` (2026-08-14): the three `card_build` traces are 1,312
    of the run's 2,637 spans — the Developer's whole construction, `plan` and `stages` included — and
    every one of their roots carried `attributes={}`. So the most expensive trace in the run was
    addressable by NO key at all, and the node's own trace showed evaluation and repair with no
    build. Node 2's trace was two spans.

    `card_build` STAYS run-scoped. It runs on a producer worker before any node id is reserved; the
    id it could compute is `_node_id_ceiling`, a prediction `_claim_requested_card_build` re-derives
    after this span has closed, and the build may be refused and mint no node at all. What is true at
    open is the request it serves, so it carries that — and the NODE, which learns the exact trace
    when it commits, names it on `materialize_node`. The reading half is
    `events/traceview.py::claimed_build_traces`; `tests/test_node_build_trace_claim.py` drives it.
    """
    import json

    engine, _producer = _engine(tmp_path / "claim")
    _start(engine)
    _add_ready_draft(engine)
    node_id = _commit_speculative_node(engine)

    spans_path = Path(tmp_path / "claim") / "spans.jsonl"
    rows = [json.loads(line) for line in
            spans_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    builds = [row for row in rows if row["name"] == "card_build"]
    materialized = [row for row in rows if row["name"] == "materialize_node"]
    assert len(builds) == 1 and len(materialized) == 1

    # RUN-SCOPED — and no longer anonymous: the request key it is serving.
    assert builds[0]["attributes"].get("node_id") is None
    assert builds[0]["attributes"]["card_id"] == "card-7"
    assert builds[0]["attributes"]["card_build_generation"] == 0
    # The node names it. A DIFFERENT trace, recorded after the fact, never a prediction.
    assert materialized[0]["attributes"]["node_id"] == node_id
    assert materialized[0]["attributes"]["build_trace"] == builds[0]["trace_id"]
    assert materialized[0]["trace_id"] != builds[0]["trace_id"]

    # …and the claim is what puts the build inside the node's own trace window.
    from looplab.events.span_index import SpanIndex
    index = SpanIndex(spans_path)
    index._rebuild(spans_path.stat().st_size)
    assert index.node_build_traces(node_id, generation=0) == {builds[0]["trace_id"]: (node_id, 0)}
    reached = {span["span_id"] for span in index.light_spans_for_node(node_id, None, generation=0)}
    assert builds[0]["span_id"] in reached


def _fresh(engine, node_id: int, *, inflight=frozenset()) -> bool:
    """Ask the production freshness gate about one committed prefetch, the way the drain does."""

    state = fold(engine.store.read_all())
    excluded = engine._election_excluded_card_ids(state)
    return speculative_card_is_fresh(
        state,
        engine.policy,
        engine._speculative_selection_node_limit(state),
        card_id=state.nodes[node_id].idea.card_id,
        node_id=node_id,
        context=SpeculativeSelectionContext(
            scoring=getattr(engine, "_card_scoring", None),
            excluded_card_ids=excluded,
            ignored_pending_node_ids=engine._acknowledged_pending_ids(state),
            resource_envelope=engine._resource_envelope(),
            consumed_inflight=inflight,
        ),
    )


def test_the_election_stops_at_the_width_the_freshness_gate_will_actually_keep(
        tmp_path, monkeypatch):
    """A prefetch the gate is obliged to discard must never be BOUGHT (backlog: v7 nodes 3 and 4).

    Two ceilings, different numbers, and only one of them decided anything. AUTO `speculation_depth`
    is the settled eval width — "one speculative prefetch per concurrent evaluation lane"
    (`_resolve_speculation_depth`) — while what `_drop_stale_speculation` KEEPS is membership in
    `speculative_card_selection_set`, which is `card_lane_width` wide, i.e. 1 under `greedy`, the
    only policy `speculation_gate.py` admits speculation for at all. So on a two-lane box the
    election bought two prefetches and the gate kept one, and the loser was terminalized
    `superseded by Card freshness gate` 0.06 s after its own `card_build_done` — one full Developer
    call (~1 h of wall clock on `rubertlite-dr-unified-v7`), never evaluated, its Card retired.

    Driven end to end rather than pinned, because the property is exactly the one a source pin
    cannot see: that the build the old ceiling licensed really does die unevaluated. The second half
    restores the old ceiling and watches it happen.
    """

    engine, _producer = _engine(tmp_path / "prefetch-ceiling", depth=2)
    engine._eval_parallel = 2                     # depth 2 = the eval width, lane width still 1
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    _add_ready_draft(engine, "card-3", x=0.5)
    _without_research(monkeypatch, engine)

    held = _commit_speculative_node(engine)       # the one prefetch a width-1 lane can retain
    engine._ensure_speculation_state()

    assert card_lane_width(engine.policy) == 1
    assert engine.speculation_depth == 2, "the pinned treatment is untouched by the ceiling"
    assert engine._speculative_prefetch_ceiling() == 1
    assert _fresh(engine, held) is True, "the gate keeps this one, so it was worth buying"

    # THE FIX: the second election is refused, and refused SILENTLY — no request row, no head, so
    # nothing downstream can pay for it either.
    before = engine.store.read_all()[-1].seq
    assert engine._request_card_build() is False
    assert engine.store.read_all()[-1].seq == before
    assert engine._head_request(fold(engine.store.read_all())) is None

    # …and the refusal is right. At the OLD ceiling the same board buys the build, and the gate it
    # has to pass on arrival throws it away without ever dispatching it.
    monkeypatch.setattr(
        engine, "_speculative_prefetch_ceiling", lambda: engine.speculation_depth)
    second = _commit_speculative_node(engine)
    assert second != held
    assert _fresh(engine, second) is False, "born stale: the width-1 lane already holds one"

    assert anyio.run(engine._drop_stale_speculation) is True
    state = fold(engine.store.read_all())
    dead = state.nodes[second]
    assert dead.status is NodeStatus.failed
    assert dead.error == CARD_FRESHNESS_SUPERSEDED_ERROR
    assert dead.error_reason == "superseded"
    assert dead.never_evaluated is True and dead.eval_started is not True
    assert state.nodes[held].status is NodeStatus.pending, "the lane kept exactly one"


def test_the_raw_proposal_lane_shares_the_prefetch_ceiling(tmp_path):
    """A refused durable election must not divert the same spend into the raw lane.

    `_card_phase_request_build` falls through to a Researcher proposal + Card staging whenever
    `_request_card_build` declines. Both of its gates therefore have to read the SAME ceiling, or
    "do not buy a prefetch the gate must discard" becomes "buy a proposal instead" — the identical
    paid call one lane over. A negative pin, because what must not come back is the bare depth.
    """

    source = inspect.getsource(speculation_module.SpeculationMixin._card_phase_request_build)
    assert "self.speculation_depth" not in source
    assert source.count("self._speculative_prefetch_ceiling()") == 2
    election = inspect.getsource(speculation_module.SpeculationMixin._request_card_build)
    assert "self.speculation_depth" not in election
    assert "self._speculative_prefetch_ceiling()" in election
