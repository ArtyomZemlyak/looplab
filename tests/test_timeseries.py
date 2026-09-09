"""I2 time-series forecasting TaskAdapter (rolling backtest, MASE)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import anyio

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import load_task
from looplab.adapters.timeseries import (BASELINE_ASSET, HARNESS_ASSET, SERIES_ASSET,
                                         TimeSeriesBaselineDeveloper, TimeSeriesTask, make_series)

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "timeseries_task.json"


def _stage(task: TimeSeriesTask, workdir: Path) -> None:
    """What the engine does before an eval: write `assets()` into the node's workdir."""
    for name, content in task.assets().items():
        (workdir / name).write_text(content, encoding="utf-8")


def _harness(task: TimeSeriesTask) -> dict:
    """The shipped harness, executed as a module — the same bytes the sandbox imports."""
    ns: dict = {}
    exec(compile(task.assets()[HARNESS_ASSET], HARNESS_ASSET, "exec"), ns)
    return ns


def test_make_series_shape_and_seasonality():
    s = make_series(seed=0, n=30, period=5, trend=0.1, noise=0.0)
    assert len(s) == 30
    # zero noise + zero trend would repeat every `period`; with trend it grows monotonically-ish
    s2 = make_series(seed=0, n=30, period=5, trend=0.0, noise=0.0)
    assert s2[0] == s2[5] == s2[10]   # pure seasonal repeats


def test_the_task_ships_the_data_and_the_metric_not_a_model(tmp_path):
    """The adapter's half of the contract (docs/BACKLOG.md §14): data + metric + a DECLARED
    baseline. It used to ship the forecaster itself, spliced into the solution as `_TS_TEMPLATE`."""
    task = TimeSeriesTask()
    assets = task.assets()
    assert set(assets) == {SERIES_ASSET, HARNESS_ASSET, BASELINE_ASSET}
    spec = json.loads(assets[SERIES_ASSET])
    assert spec["y"] == task._series() and spec["h"] == task.backtest_h
    ns = _harness(task)
    assert callable(ns["score"]) and callable(ns["load_series"])
    # The baseline is a FACTORY the candidate may import — not the solution.
    bns: dict = {}
    exec(compile(assets[BASELINE_ASSET], BASELINE_ASSET, "exec"), bns)
    f = bns["seasonal_blend"](alpha=0.0, period=3)
    assert f([1.0, 2.0, 3.0, 9.0]) == 2.0            # alpha=0 -> purely the value one period back


def test_the_llm_path_hands_the_forecaster_to_the_candidate():
    """The whole point of the change: with a model on the wire the Developer WRITES the forecaster
    (an `LLMDeveloper` with a brief), instead of filling the adapter's own template from two floats."""
    from looplab.agents.roles import LLMDeveloper, LLMResearcher

    r, d = TimeSeriesTask().llm_roles(client=object())
    assert isinstance(r, LLMResearcher) and isinstance(d, LLMDeveloper)
    assert getattr(d, "honors_idea_space", False) and getattr(d, "is_code_generating", False)
    for clause in (SERIES_ASSET, BASELINE_ASSET, "import backtest", "backtest.score", '{"metric"'):
        assert clause in d.brief, clause


def test_the_backtest_window_does_not_move_with_the_candidates_period():
    """The metric's origins are fixed by the series and `h` alone. The inline template scored from
    `max(period + 1, n - h)`, so a candidate that raised its own seasonal period was scored over a
    SHORTER window than its sibling and the two MASEs were not comparable numbers.

    Driven, not asserted from the source: the same forecaster is scored twice while it records which
    prefixes it was handed, once from a 3-long and once from a 40-long seasonal memory."""
    ns = _harness(TimeSeriesTask())
    y = make_series(seed=1, n=60, period=7, trend=0.02, noise=0.3)

    def watcher(period, seen):
        def forecast(history):
            seen.append(len(history))
            p = min(period, len(history))
            return history[-p]
        return forecast

    short, long = [], []
    ns["score"](watcher(3, short), y, 20)
    ns["score"](watcher(40, long), y, 20)
    assert short == long == list(range(40, 60))       # 20 origins, the last 20, either way
    assert ns["origins"](60, 20) == list(range(40, 60))


def test_the_offline_developer_runs_the_declared_baseline_and_writes_no_forecaster(tmp_path):
    """`backend=toy` still completes end to end — by IMPORTING the two shipped assets. The emitted
    solution carries no forecasting arithmetic of its own, which is what keeps the only forecasting
    code in this task's tree the declared baseline and whatever a candidate writes."""
    task = TimeSeriesTask()
    code = TimeSeriesBaselineDeveloper().implement(
        Idea(operator="draft", params={"alpha": 0.5, "period": 7.0}, rationale=""))
    assert "import backtest" in code and "import baseline" in code
    # No forecaster is DEFINED here — the model and the metric are both imported. Asked of the AST
    # rather than of the text, so a template that grew a `def forecast` back cannot hide in a string.
    tree = ast.parse(code)
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Lambda))]
    _stage(task, tmp_path)
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=30.0)
    assert res.exit_code == 0 and res.metric is not None and res.metric >= 0.0
    # It really is the harness's number: recompute MASE in-process over the same origins.
    ns = _harness(task)
    bns: dict = {}
    exec(compile(task.assets()[BASELINE_ASSET], BASELINE_ASSET, "exec"), bns)
    y, h = json.loads(task.assets()[SERIES_ASSET])["y"], task.backtest_h
    expected = ns["score"](bns["seasonal_blend"](0.5, 7), [float(v) for v in y], h)
    assert abs(res.metric - expected) < 1e-9


def test_a_solution_that_cannot_import_the_harness_fails_rather_than_scoring_itself(tmp_path):
    """The metric is a FILE the task ships and the eval protects. Without it staged, the baseline
    solution crashes — it has no backtest of its own to fall back on, by construction."""
    code = TimeSeriesBaselineDeveloper().implement(
        Idea(operator="draft", params={"alpha": 0.5, "period": 7.0}, rationale=""))
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=30.0)   # nothing staged
    assert res.exit_code != 0 and res.metric is None


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
