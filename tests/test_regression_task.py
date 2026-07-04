"""A real ML task through the engine: polynomial model selection via CV (ADR-2)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.eventstore import EventStore
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.regression import RegressionTask
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.tasks import load_task

ROOT = Path(__file__).resolve().parents[1]
REG_FILE = ROOT / "examples" / "regression_task.json"


def test_task_loader_dispatches_on_kind():
    quad = load_task(ROOT / "examples" / "toy_task.json")
    reg = load_task(REG_FILE)
    assert quad.id == "toy_quadratic"
    assert isinstance(reg, RegressionTask) and reg.true_degree == 2


def test_generated_solution_runs_and_cv_prefers_true_degree(tmp_path):
    """The generated ridge-CV script runs in the sandbox; the true degree (2) should
    generalize better (lower CV MSE) than a degree-0 underfit."""
    task = RegressionTask(seed=1, n=40, true_degree=2, noise=1.0)
    X, Y = task._data()
    from looplab.regression import RegressionDeveloper
    dev = RegressionDeveloper(X, Y, k=5)
    sb = SubprocessSandbox()

    r0 = sb.run(dev.implement(Idea(operator="x", params={"degree": 0.0, "lam": 0.0})),
                str(tmp_path / "d0"), timeout=30.0)
    r2 = sb.run(dev.implement(Idea(operator="x", params={"degree": 2.0, "lam": 0.0})),
                str(tmp_path / "d2"), timeout=30.0)
    assert r0.metric is not None and r2.metric is not None
    assert r2.metric < r0.metric  # degree 2 generalizes better than constant


def test_regression_run_end_to_end(tmp_path):
    task = load_task(REG_FILE)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=14))
    state = anyio.run(eng.run)
    assert state.finished and len(state.nodes) == 14
    best = state.best()
    assert best is not None and best.metric is not None
    # Found a sensible-complexity model (not wildly overfit/underfit).
    assert 1 <= int(round(best.idea.params["degree"])) <= 4

    # Grounding pre-phase ran: the dataset was profiled into the event log + state.
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert any(e.type == "data_profiled" for e in events)
    assert state.data_profile is not None and "x" in state.data_profile
    assert state.data_profile["x"]["dtype"] == "numeric"


def test_regression_survives_replay(tmp_path):
    task = load_task(REG_FILE)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=10))
    s1 = anyio.run(eng.run)
    s2 = fold(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert s2.model_dump() == s1.model_dump()
