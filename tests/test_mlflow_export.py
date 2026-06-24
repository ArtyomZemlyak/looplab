"""G5 MLflow export bridge (optional dep)."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.eventstore import EventStore
from looplab.mlflow_export import available, export_run
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def test_available_is_bool():
    assert isinstance(available(), bool)


def test_export_raises_clear_error_without_mlflow():
    if available():
        pytest.skip("mlflow installed — error path not exercised")
    from looplab.models import RunState
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
