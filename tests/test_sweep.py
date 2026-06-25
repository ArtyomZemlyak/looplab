"""Intra-node sweep: a single node runs a grid of trials in one process and reports them all on
ONE node_evaluated event. node.metric is the best feasible trial (under the task direction); the
full trial list survives a re-fold unchanged (idempotency). Plus a direct check of the
looplab.sweep helper's contract.
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import anyio

from looplab.eventstore import EventStore
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox, _json_line_trials
from looplab.sweep import enumerate_grid, run_sweep
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


class _SweepResearcher:
    """Always proposes a one-axis grid (an intra-node sweep)."""
    def propose(self, state, parent):
        return Idea(operator="draft", params={}, space={"x": [1.0, 2.0, 3.0]},
                    rationale="sweep over x")


class _SweepDeveloper:
    """Emits a self-contained script that evaluates the grid and prints the trials line (no
    dependency on looplab being importable inside the sandbox subprocess — the helper's own
    behavior is unit-tested separately)."""
    def implement(self, idea: Idea) -> str:
        xs = list(idea.space.get("x", []))
        return (
            "import json\n"
            f"xs = {xs}\n"
            "trials = [{'params': {'x': x}, 'metric': (x - 2.0) ** 2, 'seconds': 0.01,\n"
            "           'extra_metrics': {}, 'error': ''} for x in xs]\n"
            "print(json.dumps({'trials': trials}))\n"
        )


def _engine(run_dir, direction="min"):
    task = ToyTask.load(TASK_FILE)
    task.direction = direction
    return Engine(
        run_dir,
        task=task,
        researcher=_SweepResearcher(),
        developer=_SweepDeveloper(),
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=1),   # exactly one (sweep) node
    )


def test_sweep_node_collapses_to_best_min(tmp_path):
    rd = tmp_path / "run"
    state = anyio.run(_engine(rd, "min").run)
    node = state.nodes[0]
    assert len(node.trials) == 3                      # whole grid reported
    assert node.metric == 0.0                          # best (min) trial = (2-2)^2
    # Exactly ONE node_evaluated event for the whole sweep (atomic eval, one budget charge).
    evs = list(EventStore(rd / "events.jsonl").read_all())
    assert sum(1 for e in evs if e.type == "node_evaluated") == 1
    # Re-fold reproduces the trials identically (idempotency).
    refold = fold(evs)
    assert [t.model_dump() for t in refold.nodes[0].trials] == [t.model_dump() for t in node.trials]
    assert refold.nodes[0].metric == node.metric


def test_sweep_node_best_max(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", "max").run)
    node = state.nodes[0]
    assert node.metric == 1.0                           # best (max) trial = (1-2)^2 == (3-2)^2 == 1
    assert state.best_node_id == 0


def test_helper_grid_order_and_emit():
    # Deterministic cartesian product over SORTED keys.
    assert enumerate_grid({"b": [1, 2], "a": [10]}) == [{"a": 10, "b": 1}, {"a": 10, "b": 2}]
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_sweep({"lr": [0.1, 0.01]}, lambda p, s: p["lr"], direction="max")
    parsed = _json_line_trials(buf.getvalue())
    assert parsed is not None and len(parsed) == 2


def test_helper_seed_honors_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_EVAL_SEED", "5")
    seen = []
    run_sweep({"x": [0.0, 0.0]}, lambda p, s: seen.append(s) or 0.0, direction="min", emit=False)
    # base=5 -> trial seeds 5*1_000_003 + i
    assert seen == [5 * 1_000_003 + 0, 5 * 1_000_003 + 1]


def test_helper_failed_trial_isolated():
    def tf(p, s):
        if p["x"] == 2:
            raise ValueError("boom")
        return float(p["x"])
    trials = run_sweep({"x": [1, 2, 3]}, tf, direction="min", emit=False)
    metrics = [t["metric"] for t in trials]
    assert metrics == [1.0, None, 3.0]
    assert "boom" in trials[1]["error"]
