from __future__ import annotations

import sys

import anyio

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.engine.eval_attempt_rules import attempt_succeeded
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.search.policy import GreedyTree


def _engine(tmp_path, *, declared=True):
    src = tmp_path / "src"
    src.mkdir()
    (src / "score.py").write_text("print('{\"filtered\": 0.9}')\n", encoding="utf-8")
    task = RepoTask(
        id="retarget-objective", direction="max", editable_path=str(src),
        edit_surface=["*.py"],
        eval=EvalSpec(
            command=[sys.executable, "score.py"],
            metric={"kind": "stdout_json", "key": "task_metric"},
            metrics=({"filtered": {"kind": "stdout_json", "key": "filtered"}}
                    if declared else {}), cwd="."))
    researcher, developer = task.build_roles()
    engine = Engine(
        tmp_path / "run", task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
        auto_install_deps=False, inline_repair=False)
    engine.store.append("run_started", {
        "run_id": "retarget-objective", "run_uid": "uid-retarget", "task_id": task.id,
        "goal": "measure a declared objective", "direction": "max"})
    engine.store.append("metric_retarget", {"key": "filtered"})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "full",
        "idea": {"operator": "full", "params": {}}, "code": "print(1)"})
    return engine


def _evaluate(engine):
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1))


def test_declared_objective_can_succeed_without_task_metric(tmp_path):
    engine = _engine(tmp_path)
    _evaluate(engine)
    state = fold(engine.store.read_all())
    node = state.nodes[0]
    assert node.status.value == "evaluated"
    assert node.metric == 0.9
    assert node.task_metric is None
    terminal = next(e for e in engine.store.read_all() if e.type == "node_evaluated")
    assert "task_metric" in terminal.data and terminal.data["task_metric"] is None
    assert terminal.data["metric"] is None


def test_auto_objective_does_not_replace_a_genuine_declared_metric(tmp_path):
    engine = _engine(tmp_path, declared=False)
    _evaluate(engine)
    node = fold(engine.store.read_all()).nodes[0]
    assert node.status.value == "failed"
    assert node.error_reason == "no_metric"
    assert node.metric is None and node.task_metric is None


def test_an_objective_does_not_acquit_a_crashed_eval():
    result = RunResult(
        exit_code=1, stdout="", stderr="crash", metric=None, timed_out=False,
        extra_metrics={"filtered": 0.9},
        extra_metrics_provenance={"filtered": "declared"})
    assert not attempt_succeeded(result, artifact=False,
                                 objective_key="filtered", run_direction="max")
