"""DatasetTask (kind="dataset"): the fully-generative "point at data, write the whole solution,
optimize a metric" task. Runs offline via the deterministic data-reading baseline developer."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.adapters.dataset_task import DatasetBaselineDeveloper, DatasetTask, _SAMPLE_CHARS
from looplab.events.eventstore import EventStore
from looplab.core.models import Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.tools.run_tools import DataTools
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import kinds, load_task, validate_task

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


def test_brief_orientation_is_direction_correct():
    """M1 regression: the negate instruction must depend on direction. For min the loop minimizes the
    printed metric, so a natural loss (RMSE) is reported AS-IS and a natural GAIN is negated; the old
    prompt unconditionally negated a loss, inverting the objective and selecting the worst model."""
    lo = DatasetTask(data_path=str(DATA_CSV), metric="rmse", direction="min")._brief()
    assert "LOWER is better" in lo
    # under min: a natural gain is what must be negated; the brief must NOT tell the agent to negate a loss
    assert "gain (accuracy/F1/AUC/R^2), report its NEGATIVE" in lo
    assert "error/loss (RMSE, log-loss), report its NEGATIVE" not in lo

    hi = DatasetTask(data_path=str(DATA_CSV), metric="rmse", direction="max")._brief()
    assert "HIGHER is better" in hi
    # under max: a natural loss is what must be negated
    assert "error/loss (RMSE, log-loss), report its NEGATIVE" in hi
    assert "gain (accuracy/F1/AUC/R^2), report its NEGATIVE" not in hi


def test_dataset_run_end_to_end_and_replays(tmp_path):
    from looplab.events.replay import fold
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


# --- on-disk data visible to the read-only DataTools layer (assets()=={} by design) -------------
def test_data_samples_file_source():
    """A CSV-file dataset previews its on-disk data as a bounded head sample keyed by file name."""
    task = load_task(TASK_FILE)
    samples = task.data_samples()
    assert "data.csv" in samples
    body = samples["data.csv"]
    assert "x1" in body and "x2" in body and "target" in body    # the CSV header is in the sample
    assert len(body) <= _SAMPLE_CHARS                            # bounded


def test_data_samples_directory_source(tmp_path):
    """A directory-pointed dataset previews a bounded listing PLUS a head sample of the primary
    table inside it (so schema/profile have a real table to parse)."""
    d = tmp_path / "ds"
    d.mkdir()
    (d / "train.csv").write_text("a,b,target\n1,2.0,yes\n3,4.0,no\n", encoding="utf-8")
    (d / "readme.txt").write_text("hello", encoding="utf-8")
    samples = DatasetTask(data_path=str(d)).data_samples()
    listing_key = next(k for k in samples if k.endswith("/"))     # "<dirname>/" listing entry
    assert "train.csv" in samples[listing_key] and "readme.txt" in samples[listing_key]
    assert "train.csv" in samples                                # primary table surfaced for parsing
    assert "target" in samples["train.csv"]


def test_data_samples_bounded_for_large_file(tmp_path):
    """A large data file is truncated to a bounded head sample (never slurped whole), trimmed back
    to a clean line boundary so the preview never ends on a half-row."""
    big = tmp_path / "big.csv"
    big.write_text("a,b,c\n" + ("1,2,3\n" * 50000), encoding="utf-8")   # ≫ 64 KiB
    sample = DatasetTask(data_path=str(big)).data_samples()["big.csv"]
    assert len(sample) <= _SAMPLE_CHARS                          # capped, not the whole file
    assert sample.startswith("a,b,c")                            # it's the HEAD of the file
    assert sample.endswith("\n")                                 # trimmed to a whole line, no half-row
    assert _SAMPLE_CHARS - len(sample) < 16                      # only the dangling partial row dropped


def test_data_tools_sees_dataset_csv_on_disk():
    """read_asset / data_schema / data_profile all surface a CSV-file dataset's real on-disk data,
    even though the task's assets() is empty by design (was: "(this task has no data assets)")."""
    dt = DataTools(load_task(TASK_FILE))
    dt.bind_state(RunState())                                    # no recorded data_profile -> CSV fallback
    listing = dt.execute("read_asset", {})
    assert "no data assets" not in listing.lower() and "data.csv" in listing
    asset = dt.execute("read_asset", {"name": "data.csv"})
    assert "x1" in asset and "target" in asset
    sch = dt.execute("data_schema", {})
    assert "x1" in sch and "target" in sch
    prof = dt.execute("data_profile", {})
    assert "target" in prof and "numeric" in prof


def test_data_tools_sees_dataset_directory_on_disk(tmp_path):
    """For a DIRECTORY-pointed dataset columns() is empty (not a file), so DataTools' schema/profile
    fallback must parse the primary table inside the directory instead of finding nothing."""
    d = tmp_path / "ds"
    d.mkdir()
    (d / "train.csv").write_text("id,height,city,target\n1,1.8,NY,0\n2,1.6,LA,1\n3,,NY,0\n",
                                 encoding="utf-8")
    task = DatasetTask(data_path=str(d))
    assert task.columns() == {}                                  # a directory exposes no columns()
    dt = DataTools(task)
    dt.bind_state(RunState())
    assert "train.csv" in dt.execute("read_asset", {})
    sch = dt.execute("data_schema", {})
    assert "inferred from train.csv" in sch                      # used the in-dir primary table
    assert "height (numeric)" in sch and "city (categorical)" in sch
    prof = dt.execute("data_profile", {})
    assert "height: numeric" in prof and "min=1.6" in prof and "max=1.8" in prof
    assert "missing=0.33" in prof                                # 1 of 3 height values is blank


def test_data_tools_sees_dataset_tsv_directory(tmp_path):
    """A directory-pointed dataset whose primary table is a TSV is also parsed for schema/profile:
    _dir_primary_table surfaces the train.tsv sample and DataTools._primary_table parses it with a
    tab delimiter (else a tab-separated row would read as a single column)."""
    d = tmp_path / "ds"
    d.mkdir()
    (d / "train.tsv").write_text("id\theight\tcity\n1\t1.8\tNY\n2\t1.6\tLA\n", encoding="utf-8")
    dt = DataTools(DatasetTask(data_path=str(d)))
    dt.bind_state(RunState())
    assert "train.tsv" in dt.execute("read_asset", {})
    sch = dt.execute("data_schema", {})
    assert "inferred from train.tsv" in sch
    assert "height (numeric)" in sch and "city (categorical)" in sch    # tab-split, not one column
    prof = dt.execute("data_profile", {})
    assert "height: numeric" in prof and "min=1.6" in prof and "max=1.8" in prof
