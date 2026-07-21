"""I12 integration: the orchestrator-wired multi-seed confirmation phase."""
from __future__ import annotations

import sys
import threading

import anyio
import pytest

from looplab.events.eventstore import EventStore
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.adapters.toytask import ToyTask

_M = {"kind": "stdout_json", "key": "metric"}
_LAT = {"kind": "stdout_json", "key": "latency"}


def _noisy_engine(run_dir, *, confirm_top_k, confirm_seeds, max_nodes=10):
    task = ToyTask(id="toy_noisy", goal="noisy quadratic", direction="min",
                   bounds={"x": (-10.0, 10.0), "y": (-10.0, 10.0)},
                   seed=3, step=1.5, noise=0.8)
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    developer = ToyObjectiveDeveloper(noise=task.noise)
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=3, max_nodes=max_nodes),
                  confirm_top_k=confirm_top_k, confirm_seeds=confirm_seeds)


def test_confirmation_phase_runs_and_picks_robust_best(tmp_path):
    rd = tmp_path / "run"
    state = anyio.run(_noisy_engine(rd, confirm_top_k=3, confirm_seeds=6).run)
    assert state.finished

    confirmed = [n for n in state.nodes.values() if n.confirmed_mean is not None]
    assert 1 <= len(confirmed) <= 3                      # only top-k get confirmed

    # node_confirmed events were actually written to the log.
    events = list(EventStore(rd / "events.jsonl").read_all())
    assert sum(1 for e in events if e.type == "node_confirmed") == len(confirmed)
    for e in (e for e in events if e.type in ("confirm_eval", "node_confirmed")):
        assert e.data["generation"] == state.nodes[e.data["node_id"]].attempt
    completed = [e for e in events if e.type == "best_confirmed"]
    assert completed and completed[-1].data["generations"]

    # The final best is chosen from the confirmed pool by its robust mean.
    best = state.best()
    assert best is not None and best.confirmed_mean is not None
    assert best.confirmed_std is not None


def test_no_confirmation_by_default_is_unchanged(tmp_path):
    """confirm disabled (default) -> no node_confirmed events, best ranks by metric."""
    state = anyio.run(_noisy_engine(tmp_path / "run", confirm_top_k=0, confirm_seeds=0).run)
    assert state.finished
    assert all(n.confirmed_mean is None for n in state.nodes.values())
    best = state.best()
    assert best is not None and best.confirmed_mean is None


def test_confirmation_survives_replay(tmp_path):
    """Re-folding the log reproduces the confirmed best exactly (determinism)."""
    rd = tmp_path / "run"
    s1 = anyio.run(_noisy_engine(rd, confirm_top_k=2, confirm_seeds=5).run)
    s2 = fold(EventStore(rd / "events.jsonl").read_all())
    assert s2.best_node_id == s1.best_node_id
    assert s2.model_dump() == s1.model_dump()


# --- feasibility + confirm-phase resume (feasibility/NaN review round, deep audit) ----------------

# #1/#2 — an infeasible node must NOT become best even via the confirm phase
def test_infeasible_node_not_promoted_by_confirm(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    # metric is great but latency violates the constraint -> infeasible
    (repo / "run.py").write_text(
        'import json; print(json.dumps({"metric": 100.0, "latency": 999}))\n', encoding="utf-8")
    t = RepoTask(id="c", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M,
                               constraints=[{**_LAT, "name": "latency", "max": 100}]))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=3),
                 confirm_top_k=2, confirm_seeds=2)
    state = anyio.run(eng.run)
    assert state.finished
    assert all(not n.feasible for n in state.evaluated_nodes())
    assert state.best() is None                       # confirm cannot promote an infeasible node


def test_feasible_nodes_helper():
    st = RunState(direction="max")
    a = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
             status=NodeStatus.evaluated, feasible=True)
    b = Node(id=1, operator="draft", idea=Idea(operator="draft"), metric=9.0,
             status=NodeStatus.evaluated, feasible=False)
    st.nodes = {0: a, 1: b}
    assert [n.id for n in st.feasible_nodes()] == [0]


# #0/#36 — confirm resumes mid-node: seeds already recorded are NOT re-run
def test_confirm_phase_skips_already_run_seeds(tmp_path):
    from looplab.adapters.toytask import ToyTask
    task = ToyTask.load(__import__("pathlib").Path("examples/toy_task.json"))
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 confirm_top_k=1, confirm_seeds=3)
    ran: list[int] = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None):
        ran.append(int((env or {}).get("LOOPLAB_EVAL_SEED", -1)))
        return RunResult(exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)

    eng._run_eval = fake_run_eval
    eng.store.append("run_started", {
        "run_id": "run", "task_id": "toy", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": Idea(operator="draft").model_dump(mode="json"), "code": ""})
    eng.store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0})
    # Seeds 1,2 already done in a prior attempt. (Confirm seeds are 1..3 by default now —
    # confirm_seed_base=1 keeps them disjoint from the search's implicit seed 0, D1.)
    eng.store.append("confirm_eval", {
        "node_id": 0, "generation": 0, "seed": 1,
        "metric": 1.0, "eval_seconds": 0.0})
    eng.store.append("confirm_eval", {
        "node_id": 0, "generation": 0, "seed": 2,
        "metric": 1.0, "eval_seconds": 0.0})
    st = fold(eng.store.read_all())
    anyio.run(eng._confirm_phase, st)
    assert ran == [3]                                  # only the missing seed re-runs


def test_confirm_seed_reserves_and_releases_through_shared_gpu_pool(tmp_path):
    eng = _noisy_engine(tmp_path / "confirm-pool", confirm_top_k=1, confirm_seeds=1,
                        max_nodes=1)
    eng.store.append("run_started", {
        "run_id": "confirm-pool", "task_id": "toy", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": Idea(operator="draft", footprint={"gpus": 1}).model_dump(mode="json"),
        "code": ""})
    eng.store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0})
    node = fold(eng.store.read_all()).nodes[0]
    calls = []

    async def reserve(nd):
        calls.append(("reserve", nd.id))
        return {"gpu_ids": [7], "cpu_only": False, "pin": True}

    def release(ids):
        calls.append(("release", list(ids)))

    def fake_run_eval(nd, workdir, env=None, profile=None, cancel=None):
        calls.append(("run", env.get("CUDA_VISIBLE_DEVICES"), profile))
        return RunResult(exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)

    eng._wait_reserve_node_resources = reserve
    eng._release_gpus = release
    eng._run_eval = fake_run_eval
    anyio.run(eng._run_confirm_seed, node, eng.confirm_seed_base)
    assert calls == [("reserve", 0), ("run", "7", "full"), ("release", [7])]


@pytest.mark.parametrize("intervention,data", [
    ("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"}),
    ("node_abort", {"node_id": 0, "generation": 0}),
    ("node_tombstoned", {"node_ids": [0]}),
])
def test_confirmation_seed_is_cancelled_when_node_lifecycle_changes(
        tmp_path, intervention, data):
    eng = _noisy_engine(tmp_path / intervention, confirm_top_k=1, confirm_seeds=3, max_nodes=1)
    eng.store.append("run_started", {
        "run_id": intervention, "task_id": "toy", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": Idea(operator="draft").model_dump(mode="json"), "code": ""})
    eng.store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0})
    node = fold(eng.store.read_all()).nodes[0]
    started = threading.Event()
    cancelled = threading.Event()
    calls: list[int] = []

    def blocking_eval(_node, _workdir, env=None, profile=None, cancel=None):
        calls.append(int((env or {})["LOOPLAB_EVAL_SEED"]))
        started.set()
        if cancel is not None and cancel.wait(2.0):
            cancelled.set()
        return RunResult(exit_code=1, stdout="", stderr="cancelled", metric=None, timed_out=False)

    eng._run_eval = blocking_eval

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(eng._confirm_node, node)
            assert await anyio.to_thread.run_sync(started.wait, 1.0)
            eng.store.append(intervention, data)

    anyio.run(scenario)
    events = eng.store.read_all()
    state = fold(events)
    assert cancelled.is_set() and calls == [eng.confirm_seed_base]
    assert not any(event.type == "confirm_done" for event in events)
    assert 0 not in state.confirm_seed_results
