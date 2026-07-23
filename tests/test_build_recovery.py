"""Crash/re-entry closure for durable ``node_building`` reservations."""
from __future__ import annotations

from pathlib import Path

import anyio

import looplab.engine.orchestrator as orchestrator
from looplab.adapters.toytask import ToyTask
from looplab.engine.orchestrator import Engine
from looplab.core.models import card_ownership_receipt
from looplab.events.replay import fold
from looplab.events.types import (
    EV_CARD_ADDED,
    EV_CARD_DROPPED,
    EV_NODE_BUILDING,
    EV_NODE_CREATED,
    EV_NODE_FAILED,
    EV_NODE_RESET,
    EV_RUN_FINISHED,
    EV_RUN_STARTED,
)
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"
_IDEA = {"operator": "draft", "params": {}, "rationale": "recovery test"}


def _engine(run_dir: Path) -> Engine:
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=1),
        n_seeds=1,
        max_nodes=1,
    )


def test_reentry_terminalizes_all_interrupted_builds_before_setup(tmp_path, monkeypatch):
    eng = _engine(tmp_path / "run")
    eng.store.append(EV_RUN_STARTED, {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "min",
    })
    eng.store.append(EV_NODE_CREATED, {
        "node_id": 0, "generation": 0, "parent_ids": [], "operator": "draft", "idea": _IDEA,
    })
    eng.store.append(EV_NODE_RESET, {
        "node_id": 0, "generation": 0, "from_stage": "implement",
    })
    eng.store.append(EV_NODE_BUILDING, {
        "node_id": 0, "generation": 1, "operator": "draft", "parent_ids": [],
    })
    card_action = {
        "operator": "draft", "params": {}, "space": {}, "eval_profile": None,
        "parent_id": None, "parent_ids": [], "scored_against": None, "footprint": None,
    }
    eng.store.append(EV_CARD_ADDED, {
        "id": "card-5", "statement": "interrupted native build", "source": "researcher",
        "idea": {"operator": "draft", "params": {}, "space": {}, "eval_profile": None},
        "parent_id": None, "parent_ids": [], "scored_against": None, "footprint": None,
        "ownership_receipt": card_ownership_receipt(
            "card-5", "interrupted native build", card_action),
    })
    eng.store.append(EV_NODE_BUILDING, {
        "node_id": 5, "operator": "draft", "parent_ids": [], "card_id": "card-5",
    })

    setup_observation = {}

    def _setup(state):
        setup_observation["buildings"] = dict(state.buildings)
        setup_observation["status"] = state.nodes[0].status.value
        setup_observation["reason"] = state.nodes[0].error_reason
        eng.store.append(EV_RUN_FINISHED, {"reason": "aborted"})

    monkeypatch.setattr(eng, "_setup_phase", _setup)
    monkeypatch.setattr(
        orchestrator,
        "finalize_run",
        lambda engine, **_kwargs: fold(engine.store.read_all()),
    )

    final = anyio.run(eng.run)
    failures = [event for event in eng.store.read_all()
                if event.type == EV_NODE_FAILED and event.data.get("reason") == "build_interrupted"]

    # CODEX AGENT: setup is the first ordinary re-entry side effect, so observing no marker here proves
    # the crash residues were closed before any fresh policy/proposal/build work could resurrect them.
    assert setup_observation == {
        "buildings": {}, "status": "failed", "reason": "build_interrupted",
    }
    assert [(event.data["node_id"], event.data["generation"]) for event in failures] == [
        (0, 1), (5, 0),
    ]
    assert final.buildings == {}
    assert 5 not in final.nodes                 # a bare reservation does not fabricate a candidate
    dropped = [event for event in eng.store.read_all()
               if event.type == EV_CARD_DROPPED and event.data.get("id") == "card-5"]
    # CLAUDE REVIEW: FAILING ON MASTER since 16f941f split the card drop contract: engine lifecycle
    # drops now emit EV_CARD_AUTO_DROPPED (orchestrator._drop_card_once); card_dropped is reserved for
    # explicit operator intent. That commit migrated 5 other test files but missed this one — recovery
    # DOES drop card-5, just under the auto event. Filter EV_CARD_AUTO_DROPPED here (and assert
    # dropped_by == "engine") to match the new contract.
    assert len(dropped) == 1 and dropped[0].data["reason"] == "build_interrupted"
    assert final.cards["card-5"].status == "dropped"
    assert eng._recover_interrupted_builds(fold(eng.store.read_all())) is False  # idempotent re-entry
