"""Item #2: per-eval time accounting + a hard eval-compute budget that stops the silent
long sweep (distinct from the wall-clock max_seconds — this counts only time inside evals)."""
from __future__ import annotations

import sys

import anyio

from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.repo_task import EvalSpec, RepoTask
from autornd.sandbox import SubprocessSandbox

_M = {"kind": "stdout_json", "key": "metric"}


def _repo(tmp_path, body: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text(body, encoding="utf-8")
    return repo


def test_eval_seconds_accounted(tmp_path):
    repo = _repo(tmp_path, 'import json; print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="b", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2))
    state = anyio.run(eng.run)
    assert state.total_eval_seconds > 0.0
    assert state.best() is not None and state.best().eval_seconds is not None


def test_eval_budget_stops_the_sweep(tmp_path):
    # Each eval sleeps ~0.15s; a 0.1s eval-compute budget halts after the first one.
    repo = _repo(tmp_path,
                 'import time, json; time.sleep(0.15); print(json.dumps({"metric": 1.0}))\n')
    t = RepoTask(id="b", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=10),
                 max_eval_seconds=0.1)
    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "eval_budget"
    assert 1 <= len(state.evaluated_nodes()) < 10        # stopped well before the node budget
