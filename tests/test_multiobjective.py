"""Item #5: multi-objective via extra reported metrics + hard constraints. A node that
violates a constraint stays measured but is excluded from best-selection — "optimize the
metric subject to latency <= bound"."""
from __future__ import annotations

import sys

import anyio

from autornd.command_eval import run_command_eval
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.repo_task import EvalSpec, RepoTask
from autornd.sandbox import SubprocessSandbox

_M = {"kind": "stdout_json", "key": "metric"}
_LAT = {"kind": "stdout_json", "key": "latency"}


def _prog(tmp_path, metric, latency):
    (tmp_path / "p.py").write_text(
        f'import json; print(json.dumps({{"metric": {metric}, "latency": {latency}}}))\n',
        encoding="utf-8")
    return [sys.executable, "p.py"]


def test_extra_metrics_reported(tmp_path):
    cmd = _prog(tmp_path, 1.0, 50)
    res = run_command_eval(cmd, str(tmp_path), 60, _M, metrics={"latency": _LAT})
    assert res.metric == 1.0 and res.extra_metrics == {"latency": 50.0}
    assert res.violations is None


def test_constraint_satisfied(tmp_path):
    cmd = _prog(tmp_path, 1.0, 50)
    res = run_command_eval(cmd, str(tmp_path), 60, _M,
                           constraints=[{**_LAT, "name": "latency", "max": 100}])
    assert res.metric == 1.0 and res.violations is None


def test_constraint_violated(tmp_path):
    cmd = _prog(tmp_path, 1.0, 200)
    res = run_command_eval(cmd, str(tmp_path), 60, _M,
                           constraints=[{**_LAT, "name": "latency", "max": 100}])
    # metric still read (the gate is in selection, not here), but the violation is recorded.
    assert res.metric == 1.0
    assert res.violations == [{"name": "latency", "value": 200.0, "max": 100, "min": None}]


def test_unreadable_constraint_is_a_violation(tmp_path):
    cmd = _prog(tmp_path, 1.0, 50)
    res = run_command_eval(cmd, str(tmp_path), 60, _M,
                           constraints=[{"kind": "stdout_json", "key": "absent",
                                         "name": "mem", "max": 100}])
    assert res.violations and res.violations[0]["value"] is None


def _repo(tmp_path, metric, latency):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text(
        f'import json; print(json.dumps({{"metric": {metric}, "latency": {latency}}}))\n',
        encoding="utf-8")
    return repo


def _engine(tmp_path, repo, constraints):
    t = RepoTask(id="mo", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M,
                               metrics={"latency": _LAT}, constraints=constraints))
    r, d = t.build_roles()
    return Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2))


def test_engine_excludes_infeasible_from_best(tmp_path):
    repo = _repo(tmp_path, 1.0, 200)                       # violates latency <= 100
    state = anyio.run(_engine(tmp_path, repo, [{**_LAT, "name": "latency", "max": 100}]).run)
    assert state.finished
    # the node is measured + recorded infeasible, but is NOT selectable as best
    n0 = state.nodes[0]
    assert n0.metric == 1.0 and n0.feasible is False and n0.violations
    assert n0.extra_metrics == {"latency": 200.0}
    assert state.best() is None                            # no feasible solution found


def test_engine_feasible_node_wins(tmp_path):
    repo = _repo(tmp_path, 1.0, 50)                        # satisfies latency <= 100
    state = anyio.run(_engine(tmp_path, repo, [{**_LAT, "name": "latency", "max": 100}]).run)
    assert state.best() is not None and state.best().feasible is True
