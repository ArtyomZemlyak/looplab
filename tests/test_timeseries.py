"""I2 time-series forecasting TaskAdapter (rolling backtest, MASE)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import load_task
from looplab.adapters.timeseries import TimeSeriesDeveloper, TimeSeriesTask, make_series

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "timeseries_task.json"


def test_make_series_shape_and_seasonality():
    s = make_series(seed=0, n=30, period=5, trend=0.1, noise=0.0)
    assert len(s) == 30
    # zero noise + zero trend would repeat every `period`; with trend it grows monotonically-ish
    s2 = make_series(seed=0, n=30, period=5, trend=0.0, noise=0.0)
    assert s2[0] == s2[5] == s2[10]   # pure seasonal repeats


def test_developer_template_runs_and_prints_mase(tmp_path):
    task = TimeSeriesTask()
    dev = TimeSeriesDeveloper(task._series(), h=task.backtest_h)
    code = dev.implement(Idea(operator="draft", params={"alpha": 0.5, "period": 7.0}))
    # the generated script is self-contained and prints a JSON metric line
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=30.0)
    assert res.exit_code == 0 and res.metric is not None and res.metric >= 0.0


def test_load_task_registers_timeseries():
    task = load_task(TASK)
    assert isinstance(task, TimeSeriesTask) and task.direction == "min"


def test_timeseries_end_to_end(tmp_path):
    task = load_task(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=10))
    state = anyio.run(eng.run)
    assert state.finished and state.best() is not None
    assert state.best().metric is not None
