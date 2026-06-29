"""DatasetTask (kind="dataset"): the fully-generative "point at data, write the whole solution,
optimize a metric" task. Runs offline via the deterministic data-reading baseline developer."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.dataset_task import DatasetBaselineDeveloper, DatasetTask
from looplab.eventstore import EventStore
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.sandbox import SubprocessSandbox
from looplab.tasks import kinds, load_task, validate_task

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "dataset_task.json"
DATA_CSV = ROOT / "examples" / "dataset_example" / "data.csv"   # 10 data rows + header


def test_dataset_registered_and_loads():
    assert "dataset" in kinds()
    task = load_task(TASK_FILE)
    assert isinstance(task, DatasetTask)
    # the relative data_path in the JSON is resolved to an absolute path
    assert Path(task.data_path).is_absolute() and Path(task.data_path).samefile(DATA_CSV)


def test_dataset_requires_data():
    with pytest.raises(ValueError):
        validate_task({"kind": "dataset", "goal": "x"})            # no data_path / data
    with pytest.raises(ValueError):
        validate_task({"kind": "dataset", "data_path": "x", "direction": "sideways"})


def test_baseline_reads_data_and_reports_row_count(tmp_path):
    """The offline baseline script reads the CSV by absolute path and reports its row count —
    proving the data plumbing works even though the script runs in a separate sandbox workdir."""
    task = load_task(TASK_FILE)
    dev = DatasetBaselineDeveloper(task.data_path)
    res = SubprocessSandbox().run(dev.implement(Idea(operator="draft", params={})),
                                  str(tmp_path / "n0"), timeout=30.0)
    assert res.metric == 10.0   # 10 data rows in data.csv (header excluded)


def test_columns_profiles_the_csv():
    task = load_task(TASK_FILE)
    cols = task.columns()
    assert set(cols) == {"x1", "x2", "target"}
    assert len(cols["target"]) == 10
    # numeric CSV cells are coerced (else the profiler labels every column categorical)
    assert cols["target"][0] == 0 and isinstance(cols["target"][0], int)
    assert isinstance(cols["x1"][0], float)


def test_missing_data_path_rejected():
    with pytest.raises(ValueError, match="not found"):
        validate_task({"kind": "dataset", "data_path": "/no/such/file_xyz.csv"})


def test_brief_includes_path_and_open_ended_metric():
    task = load_task(TASK_FILE)                                    # no `metric` set -> agent chooses
    brief = task._brief()
    assert task.data_path in brief
    assert "metric_name" in brief and "HIGHER is better" in brief  # direction max + self-chosen metric

    fixed = DatasetTask(data_path=str(DATA_CSV), metric="f1", direction="max")
    fb = fixed._brief()
    assert "'f1'" in fb and "metric_name" not in fb               # a named metric: no self-naming ask


def test_dataset_run_end_to_end_and_replays(tmp_path):
    from looplab.replay import fold
    task = load_task(TASK_FILE)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8))
    state = anyio.run(eng.run)
    assert state.finished and len(state.nodes) == 8
    assert state.best() is not None and state.best().metric == 10.0

    # Grounding pre-phase profiled the CSV columns into the event log.
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert any(e.type == "data_profiled" for e in events)

    # Pure-fold replay reproduces the same state.
    s2 = fold(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert s2.model_dump() == state.model_dump()
