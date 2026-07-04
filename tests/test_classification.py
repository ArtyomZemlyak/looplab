"""D3 tabular classification adapter (maximize CV accuracy)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.adapters.classification import ClassificationDeveloper, ClassificationTask, make_blobs
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import load_task

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "classification_task.json"


def test_make_blobs_balanced():
    X, Y = make_blobs(seed=0, n=80, sep=1.5)
    assert len(X) == len(Y) == 80
    assert 30 <= sum(Y) <= 50   # roughly balanced 2-class


def test_developer_runs_and_reports_accuracy(tmp_path):
    task = ClassificationTask()
    X, Y = task._data()
    code = ClassificationDeveloper(X, Y).implement(
        Idea(operator="draft", params={"lr": 0.1, "l2": 0.0, "iters": 100.0}))
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=30.0)
    assert res.exit_code == 0 and res.metric is not None and 0.0 <= res.metric <= 1.0


def test_load_task_registers_classification():
    task = load_task(TASK)
    assert isinstance(task, ClassificationTask) and task.direction == "max"


def test_classification_end_to_end_maximizes(tmp_path):
    task = load_task(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8))
    state = anyio.run(eng.run)
    assert state.finished and state.best() is not None
    # separable-ish blobs -> a competent learner should clear chance (0.5)
    assert state.best().metric >= 0.6
