"""Layer-3 orchestrator wiring: precedence and exact existing-Card claims."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import looplab.engine.orchestrator as orchestrator_module
import looplab.search.speculation_quality as speculation_quality
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.config import Settings
from looplab.core.models import Idea, RunState
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import (
    Engine,
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
)
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED, EV_CARD_DROPPED, EV_NODE_BUILDING, EV_NODE_CREATED
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.card_selection import META_CARD_ID
from looplab.search.policy import EvolutionaryPolicy, GreedyTree
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_SEEDS,
    speculation_runtime_scope_digest,
)


@pytest.fixture(autouse=True)
def _admit_unit_speculation_receipt(monkeypatch):
    """Exercise positive-depth mechanics through the public receipt seam."""
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


class _NoResearcher:
    def __init__(self):
        self.calls = 0

    def propose(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("an existing Card claim must not call the Researcher")


class _CaptureDeveloper:
    def __init__(self):
        self.ideas: list[Idea] = []

    def implement(self, idea: Idea):
        self.ideas.append(idea.model_copy(deep=True))
        return "print(1)"


def _engine(run_dir, *, unified=False, agent_drives=False, policy=None, max_nodes=4,
            card_driven=True, speculation_depth=0) -> Engine:
    task = ToyTask()
    researcher = _NoResearcher()
    developer = _CaptureDeveloper()
    if card_driven and speculation_depth > 0:
        receipt_path = str(
            Path(run_dir)
            / f"unit-speculation-receipt-{max_nodes}-{speculation_depth}"
        )
        settings = Settings(**{
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": max_nodes,
            "speculation_depth": speculation_depth,
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
            policy=GreedyTree(n_seeds=3, max_nodes=max_nodes, debug_depth=1),
            options=EngineOptions.from_settings(settings),
            role_factory=calibrated_roles,
            _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
                settings.masked_snapshot()),
        )
        engine.researcher = researcher
        engine.developer = developer
        engine.role_factory = None
        engine.policy = policy or GreedyTree(
            n_seeds=0, max_nodes=max_nodes, debug_depth=0)
    else:
        engine = Engine(
            run_dir,
            task=task,
            researcher=researcher,
            developer=developer,
            sandbox=SubprocessSandbox(),
            policy=policy or GreedyTree(n_seeds=0, max_nodes=max_nodes, debug_depth=0),
            n_seeds=0,
            max_nodes=max_nodes,
            unified_agent=unified,
            agent_drives_actions=agent_drives,
            card_driven_selection=card_driven,
            speculation_depth=speculation_depth,
        )
    engine._novelty_mode = "off"
    return engine


def _start(
    engine: Engine,
    *,
    card_driven_selection: bool | None = None,
) -> None:
    payload = {
        "run_id": engine.run_dir.name,
        "task_id": "toy",
        "goal": "g",
        "direction": "min",
        **engine._run_start_pinned_values(),
    }
    if card_driven_selection is not None:
        payload["card_driven_selection"] = card_driven_selection
    engine.store.append("run_started", payload)


def _add_ready_draft(engine: Engine, card_id="card-7", *, x=0.25) -> Idea:
    idea = Idea(
        operator="draft",
        params={"x": x, "y": -1.0},
        rationale=f"use queued proposal {card_id}",
        hypothesis=f"queued proposal {card_id} improves the objective",
        card_id=card_id,
    )
    action = Engine._card_action(
        idea, [], {}, None, None, scored_against_empty=True)
    statement = Engine._card_statement(idea)
    assert statement is not None
    engine.store.append(EV_CARD_ADDED, Engine._card_added_payload(
        card_id, statement, action, idea,
        source="researcher", at_node=0,
    ))
    return idea


def test_both_selector_flags_give_card_authority_precedence(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "precedence", unified=True, agent_drives=True)
    treatment = {"stance": "explore", "novelty_weight": 0.8, "coverage_weight": 0.2}
    engine._card_scoring = treatment
    seen = {}

    def _cards(state, policy, max_nodes, *, scoring=None):
        seen.update(state=state, policy=policy, max_nodes=max_nodes, scoring=scoring)
        return [{"kind": "draft", META_CARD_ID: "card-x"}]

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("lower-precedence selector was called")

    monkeypatch.setattr("looplab.engine.orchestrator.card_next_actions", _cards)
    monkeypatch.setattr(engine, "_agent_next_actions", _forbidden)
    monkeypatch.setattr(engine.policy, "next_actions", _forbidden)

    state = RunState()
    assert engine._select_actions(state) == [{"kind": "draft", META_CARD_ID: "card-x"}]
    assert seen == {
        "state": state, "policy": engine.policy,
        "max_nodes": engine.policy.max_nodes, "scoring": treatment,
    }


def test_positive_speculation_depth_is_inert_when_card_selector_is_off(
    tmp_path, monkeypatch,
):
    engine = _engine(
        tmp_path / "depth-without-card-mode",
        card_driven=False,
        speculation_depth=3,
    )
    state = RunState()
    observed = {}

    async def _serial(evals, selected_state, max_es):
        observed.update(evals=evals, state=selected_state, max_es=max_es)

    monkeypatch.setattr(engine, "_dispatch_evals", _serial)

    assert engine._speculation_enabled() is False
    anyio.run(engine._run_card_session, ["sentinel"], state, 12.5)
    assert observed == {"evals": ["sentinel"], "state": state, "max_es": 12.5}
    assert not [
        event for event in engine.store.read_all()
        if event.type in {"card_build_requested", "card_build_done"}
    ]


def test_atomic_claim_reuses_card_without_reregister_or_researcher_call(tmp_path):
    engine = _engine(tmp_path / "claim")
    _start(engine)
    original = _add_ready_draft(engine)
    before = fold(engine.store.read_all())
    assert before.cards[original.card_id].selection_ready is True

    actions = engine._select_actions(before)
    assert actions == [{"kind": "draft", META_CARD_ID: original.card_id}]
    reservation = engine._claim_existing_card_build(actions[0])
    assert reservation is not None and reservation.card_id == original.card_id

    prefix = engine.store.read_all()
    assert len([event for event in prefix if event.type == EV_CARD_ADDED]) == 1
    building = [event for event in prefix if event.type == EV_NODE_BUILDING]
    assert len(building) == 1 and building[0].data["card_id"] == original.card_id
    assert engine.researcher.calls == 0

    engine._create_node(actions[0], reserved=reservation)
    events = engine.store.read_all()
    assert len([event for event in events if event.type == EV_CARD_ADDED]) == 1
    created = [event for event in events if event.type == EV_NODE_CREATED]
    assert len(created) == 1 and created[0].data["idea"]["card_id"] == original.card_id
    assert engine.researcher.calls == 0
    assert engine.developer.ideas[0].card_id == original.card_id


def test_claim_fails_closed_when_card_drops_after_selection(tmp_path):
    engine = _engine(tmp_path / "drop-race")
    _start(engine)
    original = _add_ready_draft(engine)
    action = engine._select_actions(fold(engine.store.read_all()))[0]
    engine.store.append(EV_CARD_DROPPED, {
        "id": original.card_id, "reason": "operator withdrew it", "dropped_by": "operator",
    })

    assert engine._claim_existing_card_build(action) is None
    assert not [event for event in engine.store.read_all() if event.type == EV_NODE_BUILDING]


def test_claim_tail_cas_rejects_control_event_winning_after_refold(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "cas-race")
    _start(engine)
    original = _add_ready_draft(engine)
    action = engine._select_actions(fold(engine.store.read_all()))[0]
    append = engine.store.append
    append_many = engine.store.append_many
    raced = False

    def _racing_append_many(records, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            append(EV_CARD_DROPPED, {
                "id": original.card_id, "reason": "won the CAS", "dropped_by": "operator",
            })
        return append_many(records, **kwargs)

    monkeypatch.setattr(engine.store, "append_many", _racing_append_many)
    assert engine._claim_existing_card_build(action) is None
    assert raced is True
    assert not [event for event in engine.store.read_all() if event.type == EV_NODE_BUILDING]


def test_staged_proposal_is_ready_inventory_without_a_node_owner(tmp_path):
    engine = _engine(tmp_path / "staged-inventory")
    _start(engine)
    engine._gpu_ids = [0]
    engine._gpu_mem = {0: 8_192}
    events = engine.store.read_all()
    state = fold(events)
    idea = Idea(
        operator="draft",
        params={"x": 0.4},
        hypothesis="stage a bounded seed before implementation",
        rationale="let the request-driven producer consume this durable proposal",
        footprint={"gpus": 9, "gpu_mem_mib": 32_768},
    )

    card_id = engine._stage_prepared_card(
        {"kind": "draft"},
        idea,
        proposal_state=state,
        proposal_node_ceiling=0,
        at_node=0,
        source="researcher",
    )

    assert card_id == "card-0"
    after_events = engine.store.read_all()
    assert [event.type for event in after_events].count(EV_CARD_ADDED) == 1
    assert not [event for event in after_events if event.type == EV_NODE_BUILDING]
    after = fold(after_events)
    assert after.cards[card_id].selection_ready is True
    assert after.cards[card_id].footprint == {
        "gpus": 1, "gpu_mem_mib": 8_192, "proposed_by": "researcher",
    }

    # Crash-prefix retry sees the exact ready receipt and reuses it without duplicate registration.
    assert engine._stage_prepared_card(
        {"kind": "draft"},
        idea,
        proposal_state=state,
        proposal_node_ceiling=0,
        at_node=0,
        source="researcher",
    ) == card_id
    assert [event.type for event in engine.store.read_all()].count(EV_CARD_ADDED) == 1


def test_positive_depth_without_isolated_pair_falls_back_to_serial_card_claim(tmp_path):
    engine = _engine(
        tmp_path / "no-isolated-pair",
        max_nodes=1,
        policy=GreedyTree(n_seeds=0, max_nodes=1, debug_depth=0),
        speculation_depth=2,
    )
    _start(engine)
    original = _add_ready_draft(engine)

    anyio.run(engine.run)

    events = engine.store.read_all()
    assert not [event for event in events if event.type == "card_build_requested"]
    assert [event.data["card_id"] for event in events
            if event.type == EV_NODE_BUILDING] == [original.card_id]
    assert [event.data["idea"]["card_id"] for event in events
            if event.type == EV_NODE_CREATED] == [original.card_id]
    assert engine.researcher.calls == 0


def test_staged_card_bootstrap_forwards_the_invocation_wall_deadline(
    tmp_path, monkeypatch,
):
    engine = _engine(
        tmp_path / "bootstrap-deadline",
        max_nodes=1,
        policy=GreedyTree(n_seeds=0, max_nodes=1, debug_depth=0),
        speculation_depth=1,
    )
    _start(engine)
    _add_ready_draft(engine)
    engine.max_seconds = 30.0
    captured = {}

    monkeypatch.setattr(engine, "_setup_phase", lambda _state: None)
    monkeypatch.setattr(engine, "_producer_role_pair", lambda: (object(), object()))
    monkeypatch.setattr(engine, "_request_card_build", lambda: True)

    async def _session(evals, state, max_es, wall_deadline=None):
        captured.update(
            evals=evals,
            state=state,
            max_es=max_es,
            wall_deadline=wall_deadline,
        )
        engine.store.append("run_finished", {"reason": "test session observed"})

    monkeypatch.setattr(engine, "_run_card_session", _session)
    monkeypatch.setattr(orchestrator_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(
        orchestrator_module,
        "finalize_run",
        lambda current, **_kwargs: fold(current.store.read_all()),
    )

    anyio.run(engine.run)

    assert captured["evals"] == []
    assert captured["max_es"] is None
    assert captured["wall_deadline"] == 130.0


def test_population_lane_claims_complete_card_batch_before_first_build(tmp_path):
    policy = EvolutionaryPolicy(pop=0, max_nodes=2, elite=2, debug_depth=0)
    engine = _engine(tmp_path / "population-batch", policy=policy, max_nodes=2)
    _start(engine)
    first = _add_ready_draft(engine, "card-1", x=0.2)
    second = _add_ready_draft(engine, "card-2", x=0.8)

    anyio.run(engine.run)

    events = engine.store.read_all()
    building = [event for event in events if event.type == EV_NODE_BUILDING]
    created = [event for event in events if event.type == EV_NODE_CREATED]
    assert [event.data["card_id"] for event in building] == [first.card_id, second.card_id]
    assert [event.data["idea"]["card_id"] for event in created] == [first.card_id, second.card_id]
    assert engine.researcher.calls == 0


def test_resume_restores_card_mode_before_reapplying_recorded_scoring(tmp_path):
    engine = _engine(tmp_path / "resume-scoring", card_driven=False)
    _start(engine, card_driven_selection=True)
    treatment = {"stance": "explore", "novelty_weight": 0.8, "coverage_weight": 0.2}
    engine.store.append("strategy_decision", {
        "strategy": {"card_scoring": treatment, "source": "rule"},
        "at_node": 0,
        "ctx": None,
    })

    assert engine.card_driven_selection is False
    engine._reentry_repin()
    assert engine.card_driven_selection is True
    assert engine._card_scoring == treatment
