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
from looplab.core.models import Idea, NodeStatus, RunState
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
    # STRANDED BY `0f5c565a`, which gave the outer election a `consumed_inflight` keyword: this
    # stub raised `TypeError` inside the loop and the wall-deadline assertion below never ran.
    monkeypatch.setattr(engine, "_request_card_build", lambda **_kwargs: True)

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


# --- the whole defect, end to end (2026-08-05) ----------------------------------------------------


class _ClientMarker:
    """Only has to exist: `_build_calls_an_llm` reads `client is not None`, which is what makes the
    isolated producer role pair eligible and puts the run on the speculative build path."""


class _CrashFirstDeveloper(ToyObjectiveDeveloper):
    """A task whose FIRST node fails to build. That is all it takes — on a real repo task it is
    routine, and it is what every run that hit this defect had in common."""

    client = _ClientMarker()

    def __init__(self):
        super().__init__()
        self.made = 0

    def implement(self, idea):
        code = super().implement(idea)
        self.made += 1
        if self.made == 1:
            return "raise SystemExit('injected build failure')\n"
        return code


class _SpeculationResearcher(ToyResearcher):
    client = _ClientMarker()


@pytest.mark.parametrize("depth", [0, 1])
def test_a_run_whose_first_node_crashes_finishes_at_every_speculation_depth(tmp_path, depth):
    """The regression the whole change exists for.

    At depth 1 this used to die after TWO nodes of a twelve-node budget with
    `stuck: node creation not converging`: node 0 crashed, the `debug` Card authored to repair it
    owned a node, that node was its own parent's child, so the anchor died, the freshness gate saw a
    blocker set beyond `{work_in_flight}` and superseded every prefetch on sight — and the lane
    authored a fresh permanently-unselectable Card every loop turn (88 of them) until the runaway
    guard tripped. Speculation OFF is the control: it completes the same budget.
    """
    task = ToyTask()

    def _roles():
        base, _ = ToyTask().build_roles()
        return (_SpeculationResearcher(base.bounds, seed=base.seed, step=base.step),
                _CrashFirstDeveloper())

    researcher, developer = _roles()
    engine = Engine(
        tmp_path / f"crash-first-{depth}", task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=12), policy_name="greedy",
        role_factory=_roles, card_driven_selection=True, speculation_depth=depth,
        eval_parallel=1, llm_parallel=1, max_nodes=12, n_seeds=1, inline_repair=False,
    )

    async def _bounded():
        with anyio.move_on_after(180):
            await engine.run()

    anyio.run(_bounded)

    state = fold(engine.store.read_all())
    reasons = [event.data.get("reason", "") for event in engine.store.read_all()
               if event.type == "run_finished"]
    assert reasons and not any("stuck" in str(reason) for reason in reasons), reasons
    assert state.finished is True

    from looplab.search.card_selection import card_budget_used
    evaluated = [node for node in state.nodes.values() if node.status is NodeStatus.evaluated]
    assert len(evaluated) >= 10, f"depth={depth} evaluated only {len(evaluated)}"
    assert state.best_node_id is not None
    # NOTHING BREEDS FROM NODE 0 ANY MORE, and that is the F5 decision, not a regression. This line
    # used to read "the crash was repaired rather than abandoned: something bred from node 0" — the
    # `debug` Card opening a fresh node to have another go at the experiment that just failed. That
    # node is deleted; a crash is repaired IN PLACE (and this run sets `inline_repair=False`, so
    # here it is simply abandoned). The regression this test actually exists for is above and still
    # holds at both depths: no `stuck`, the run finishes, ten of twelve nodes evaluated, and the
    # budget denominator matches the speculation-off control exactly.
    assert not any(0 in node.parent_ids for node in state.nodes.values()), (
        "no node may be created to retry a failed experiment (F5)")
    # Every discarded prefetch was refunded, so the budget denominator matches the off-run exactly.
    assert card_budget_used(state) == 12, sorted(
        (node.id, node.status.value, node.error_reason) for node in state.nodes.values())


# --- Card INVENTORY is not the prefetch lane's to own (2026-08-07) --------------------------------


def _splits_a_batch(run_dir, seq: int) -> bool:
    """Whether folding the log UP TO (not including) `seq` would tear an `append_many` envelope.

    Read from the physical JSONL, because that is the only place the atomicity is recorded: a batch
    is ONE line carrying `first_seq`/`last_seq`, and the engine can only ever observe the log before
    that line or after it. Deriving this from event adjacency instead is wrong in the direction that
    hides the property under test — two SINGLETON appends land adjacent all the time, and the
    boundary between them is exactly where a staged Card sits alone in inventory.
    """
    import json

    for line in (run_dir / "events.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        data = row.get("data") or {}
        if data.get("schema") != "looplab.event-batch/v1":
            continue
        if data["first_seq"] < seq <= data["last_seq"]:
            return True
    return False


@pytest.mark.parametrize("depth", [0, 1])
def test_card_inventory_is_minted_whenever_card_selection_is_on(tmp_path, depth):
    """`card_driven_selection` alone must produce a Card QUEUE the run then selects from.

    MEASURED before this change, over the shipped corpus by folding every observable prefix
    boundary: the seven runs with `card_driven_selection=true` and no positive depth produced **0**
    `selection_ready` cards across 27 nodes, while the four speculation runs produced 24. The cause
    was structural — `_stage_card_creates`, the only writer of a `card_added` with no `node_building`
    beside it, was reachable only inside `if self._speculation_enabled():`. With prefetch off every
    card was minted by `_reserve_node_build` in the same crash-atomic batch as the claim, so it was
    work-owned the instant it existed and `card_next_actions` fell through to `policy.next_actions`.

    The property is observed the way the corpus was: a Card that is `selection_ready` at some
    boundary the ENGINE could observe — never inside an `append_many` batch, which is exactly the
    prefix a run can never fold, and which is read from the PHYSICAL log rather than guessed at from
    adjacency (two singleton appends can be adjacent and are perfectly observable between).
    `depth=1` is the control; it is what already worked.
    """
    task = ToyTask()

    def _roles():
        base, _ = ToyTask().build_roles()
        return (_SpeculationResearcher(base.bounds, seed=base.seed, step=base.step),
                ToyObjectiveDeveloper(noise=0.0))

    researcher, developer = _roles()
    engine = Engine(
        tmp_path / f"inventory-{depth}", task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=4), policy_name="greedy",
        role_factory=_roles, card_driven_selection=True, speculation_depth=depth,
        eval_parallel=1, llm_parallel=1, max_nodes=4, n_seeds=1, inline_repair=False,
    )
    assert engine._card_inventory_enabled() is True
    assert engine._speculation_enabled() is (depth > 0)

    async def _bounded():
        with anyio.move_on_after(180):
            await engine.run()

    anyio.run(_bounded)

    events = engine.store.read_all()
    ready = set()
    for cut in range(1, len(events) + 1):
        if cut < len(events) and _splits_a_batch(engine.run_dir, events[cut].seq):
            continue                       # inside an append_many envelope — never observable
        ready |= {card.id for card in fold(events[:cut]).cards.values()
                  if card.selection_ready is True}

    assert ready, (
        "card_driven_selection produced no selectable Card inventory at any observable boundary "
        f"(depth={depth})")
    state = fold(events)
    assert len(state.nodes) >= 2, "the run has to actually build from that inventory"
    # …and the queue really is what chose the work: every Node carries the Card it was selected from.
    assert all(node.idea.card_id for node in state.nodes.values())


def _claim_batch_widths(run_dir) -> list[int]:
    """How many `node_building` rows each atomic `append_many` envelope carried.

    `_claim_existing_card_builds` claims the whole selected lane in ONE tail-CAS group, so this is
    the lane width the queue actually delivered — readable only from the physical log, since
    `read_all` expands a batch into its members and the atomicity disappears.
    """
    import json

    widths = []
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        data = (json.loads(line).get("data") or {})
        if data.get("schema") != "looplab.event-batch/v1":
            continue
        claims = [e for e in data["events"] if e.get("type") == EV_NODE_BUILDING]
        if claims:
            widths.append(len(claims))
    return widths


def test_the_prefetch_off_queue_does_not_serialize_a_wide_seed_lane(tmp_path):
    """A queue that flattens the run's batch width has changed the search, not just the selector.

    With prefetch ON, staging one work item per turn is right: the isolated steady-state proposer
    fills the depth, and a wide unreserved batch goes stale if the first fast eval moves `best`.
    With prefetch OFF neither is true — nothing is in flight at the create branch — and truncating
    would serialize the rung-0/seed width and the population lane of every non-greedy policy, which
    never reached this code before (AUTO settles the depth to 0 for a non-greedy policy, and a
    spelled one is refused there). So the whole lane is staged, and `_claim_existing_card_builds`
    claims it in ONE tail-CAS group.
    """
    task = ToyTask()

    def _roles():
        base, _ = ToyTask().build_roles()
        return (_SpeculationResearcher(base.bounds, seed=base.seed, step=base.step),
                ToyObjectiveDeveloper(noise=0.0))

    researcher, developer = _roles()
    engine = Engine(
        tmp_path / "wide-seed-lane", task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=3, max_nodes=6), policy_name="greedy",
        role_factory=_roles, card_driven_selection=True, speculation_depth=0,
        eval_parallel=1, llm_parallel=1, max_nodes=6, n_seeds=3, inline_repair=False,
    )
    engine._novelty_mode = "off"
    assert engine._speculation_enabled() is False

    async def _bounded():
        with anyio.move_on_after(180):
            await engine.run()

    anyio.run(_bounded)

    events = engine.store.read_all()
    added = [event for event in events if event.type == EV_CARD_ADDED]
    assert len(added) >= 3, "the seed lane never reached durable inventory"
    # The three seed Cards are staged as INVENTORY — standalone appends, no claim beside them — and
    # are therefore all selectable at the same observable boundary.
    widest = max(
        (len({card.id for card in fold(events[:cut]).cards.values() if card.selection_ready is True})
         for cut in range(1, len(events) + 1)
         if cut >= len(events) or not _splits_a_batch(engine.run_dir, events[cut].seq)),
        default=0)
    assert widest >= 3, f"the queue was never more than {widest} card(s) deep — the lane was serialized"
    # …and the run then claimed that whole lane atomically rather than one node per turn.
    assert max(_claim_batch_widths(engine.run_dir), default=0) >= 3, _claim_batch_widths(
        engine.run_dir)
