"""I6 keystone: a real (tiny) autonomous run, plus crash + resume.

These tests drive the full loop on the toy task: draft -> sandbox-run -> evaluate ->
improve -> greedy-select, and verify crash-resume continues from the exact frontier
with no duplicate or lost work.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import anyio

from looplab.eventstore import EventStore
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def _engine(run_dir, max_nodes=8, crash_after=None):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=3, max_nodes=max_nodes),
        max_parallel=4,
    )


def test_full_run_optimizes(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", max_nodes=12).run)
    assert state.finished
    assert len(state.nodes) == 12
    best = state.best()
    assert best is not None
    # The loop should make real progress toward the optimum (loss 0 at x=3,y=-1).
    assert best.metric < 5.0
    # Artifacts written.
    assert (tmp_path / "run" / "events.jsonl").exists()
    assert (tmp_path / "run" / "tree.html").exists()
    assert (tmp_path / "run" / "readmodel.sqlite").exists()


def test_resume_finished_run_is_idempotent(tmp_path):
    """A run that reached its budget is terminal (run_finished). Re-entering the loop
    on it must be a no-op: same nodes, log not rewritten. (Mid-run continuation is
    covered by the crash-resume subprocess test below.)"""
    rd = tmp_path / "run"
    s1 = anyio.run(_engine(rd, max_nodes=6).run)
    assert s1.finished and len(s1.nodes) == 6
    n_events_1 = len(list(EventStore(rd / "events.jsonl").read_all()))

    s2 = anyio.run(_engine(rd, max_nodes=6).run)  # resume a finished run
    assert s2.finished
    assert sorted(s2.nodes) == sorted(s1.nodes) == list(range(6))
    # Append-only log unchanged (no duplicate work).
    assert len(list(EventStore(rd / "events.jsonl").read_all())) == n_events_1


def test_crash_then_resume_subprocess(tmp_path):
    """The real keystone: kill -9 mid-run (hard exit), then resume to completion."""
    rd = tmp_path / "run"
    env = {**_clean_env()}

    # 1) Run with a crash hook -> process hard-exits after 2 evaluations.
    proc = subprocess.run(
        [sys.executable, "-m", "looplab.cli", "run", str(TASK_FILE),
         "--out", str(rd), "--max-nodes", "10", "--crash-after", "2"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    assert proc.returncode != 0  # crashed, did not finish cleanly
    crashed = fold(EventStore(rd / "events.jsonl").read_all())
    assert not crashed.finished
    assert len(crashed.evaluated_nodes()) >= 2

    # 2) Resume -> completes, no duplicates, all 10 nodes present.
    proc2 = subprocess.run(
        [sys.executable, "-m", "looplab.cli", "resume", str(rd),
         "--task-file", str(TASK_FILE), "--max-nodes", "10"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    assert proc2.returncode == 0, proc2.stderr
    final = fold(EventStore(rd / "events.jsonl").read_all())
    assert final.finished
    assert sorted(final.nodes) == list(range(10))
    assert final.best() is not None


def _clean_env():
    import os
    e = dict(os.environ)
    # Make the in-repo package importable for `-m LoopLab.cli`.
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    return e
