"""G5 MLflow export bridge (optional dep)."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.events.eventstore import EventStore
from looplab.events.mlflow_export import available, export_run
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def test_available_is_bool():
    assert isinstance(available(), bool)


def test_export_raises_clear_error_without_mlflow():
    if available():
        pytest.skip("mlflow installed — error path not exercised")
    from looplab.core.models import RunState
    with pytest.raises(RuntimeError, match="mlflow"):
        export_run(RunState(run_id="r", task_id="t"))


@pytest.mark.skipif(not available(), reason="mlflow not installed")
def test_export_logs_champion(tmp_path):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    anyio.run(eng.run)
    state = fold(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    rid = export_run(state, tracking_uri=f"file:{tmp_path / 'mlruns'}", experiment="looplab-test")
    assert isinstance(rid, str) and rid


def test_export_run_dir_logs_champion_not_best(tmp_path, monkeypatch):
    # Regression: when a pinned champion differs from the metric-best node, export_run_dir must log
    # the CHAMPION's params + code together (threaded via node=champ) — not best()'s — so the exported
    # solution.py and the logged hyperparameters describe ONE node. Uses a fake mlflow (no real dep).
    import sys
    import types

    import looplab.events.mlflow_export as mod

    rd = tmp_path / "run"
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 0.0}, "rationale": ""}, "code": "# n0 best"})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0})   # metric-best (min direction)
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"x": 9.0}, "rationale": ""}, "code": "# n1 champ"})
    s.append("node_evaluated", {"node_id": 1, "metric": 5.0})   # worse metric
    s.append("promote", {"node_id": 1})                          # pin champion to the NON-best node
    state = fold(s.read_all())
    assert state.best_node_id == 0 and state.champion == 1       # champion != best

    logged = {"params": {}, "texts": {}}
    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = lambda *a, **k: None
    fake.set_experiment = lambda *a, **k: None
    fake.set_tags = lambda *a, **k: None
    fake.log_param = lambda k, v: logged["params"].__setitem__(str(k), v)
    fake.log_metric = lambda *a, **k: None
    fake.log_text = lambda t, p: logged["texts"].__setitem__(p, t)

    class _Run:
        class info:
            run_id = "fake-1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake.start_run = lambda *a, **k: _Run()
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    rid = mod.export_run_dir(rd)
    assert rid == "fake-1"
    assert logged["params"] == {"x": 9.0}                       # champion's params, NOT best()'s {"x": 0.0}
    assert logged["texts"]["solution.py"] == "# n1 champ"       # champion's code, not best's
