"""A task FILE's relative paths are read against the file's own directory, and the run records them
absolute (doc 75 UX-04).

`looplab run <repo>/examples/dataset_task.json` refused from anywhere but the repo root, and because
`task.snapshot.json` kept the relative spelling, `resume` (and the UI server, which spawns it from its
own directory) depended on the current directory too. The rule is `core/appconfig.py::
resolve_task_paths`: beside the file first, the current directory as before when only it holds the
path, and a REFUSAL when both hold different files of that name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.appconfig import resolve_task_paths
from looplab.core.errors import ConfigRefusal

ROOT = Path(__file__).resolve().parents[1]


def _invoke(*args):
    return CliRunner().invoke(app, [str(a) for a in args])


def test_an_example_runs_from_another_directory_and_resumes_from_a_third(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    run_dir = tmp_path / "ds"
    first = _invoke("run", ROOT / "examples" / "dataset_task.json", "--backend", "toy",
                    "--max-nodes", "2", "--out", run_dir)
    assert first.exit_code == 0, first.output
    snapshot = json.loads((run_dir / "task.snapshot.json").read_text(encoding="utf-8"))
    assert Path(snapshot["data_path"]).is_absolute()
    assert Path(snapshot["data_path"]) == ROOT / "examples" / "dataset_example" / "data.csv"

    third = tmp_path / "third"
    third.mkdir()
    monkeypatch.chdir(third)
    resumed = _invoke("resume", run_dir, "--max-nodes", "3")
    assert resumed.exit_code == 0, resumed.output
    assert "nodes=3" in resumed.output


def test_two_different_files_of_one_name_are_refused_not_guessed(tmp_path, monkeypatch):
    task_dir, cwd = tmp_path / "task", tmp_path / "cwd"
    for base, rows in ((task_dir, "a,target\n1,0\n"), (cwd, "a,target\n9,9\n")):
        base.mkdir()
        (base / "data.csv").write_text(rows, encoding="utf-8")
    monkeypatch.chdir(cwd)
    with pytest.raises(ConfigRefusal, match="names two different things"):
        resolve_task_paths({"kind": "dataset", "data_path": "data.csv"}, task_dir)


def test_the_rule_beside_the_file_then_the_current_directory(tmp_path, monkeypatch):
    task_dir, cwd = tmp_path / "task", tmp_path / "cwd"
    task_dir.mkdir()
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    (task_dir / "beside.csv").write_text("x", encoding="utf-8")
    (cwd / "here.csv").write_text("x", encoding="utf-8")
    task = {"kind": "dataset", "data_path": "beside.csv", "data": {"extra": "here.csv"}}
    out = resolve_task_paths(task, task_dir)
    assert out["data_path"] == str(task_dir / "beside.csv")
    assert out["data"]["extra"] == str(cwd / "here.csv")            # a cwd-relative file still works
    assert task["data_path"] == "beside.csv", "the input is not mutated"
    missing = resolve_task_paths({"kind": "dataset", "data_path": "nope.csv"}, task_dir)
    assert missing["data_path"] == str(task_dir / "nope.csv"), "the refusal names the documented base"
    flagged = resolve_task_paths({"kind": "dataset", "data_path": "here.csv"}, None)
    assert flagged["data_path"] == str(cwd / "here.csv")


def test_a_repo_task_names_its_editable_tree_the_same_way(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = resolve_task_paths(json.loads((ROOT / "examples" / "repo_task.json").read_text()),
                             ROOT / "examples")
    assert out["editable_path"] == str(ROOT / "examples" / "repo_example")
    composable = resolve_task_paths(
        json.loads((ROOT / "examples" / "repo_stages_task.json").read_text()), ROOT / "examples")
    assert composable["repo"] == str(ROOT / "examples" / "repo_example")
    assert composable["dataset"]["raw"]["path"] == str(ROOT / "examples" / "repo_example")


def test_a_runs_own_snapshot_is_loaded_as_written(tmp_path, monkeypatch):
    """Code review: `load_task` re-read every `task.snapshot.json` against the run directory. An OLD
    snapshot stored paths as written (cwd-relative), so a file of that name both in the run dir and
    the current directory refused the resume, and any other relative path was rewritten into a
    spurious `task_changed`. Snapshots are never re-resolved; this build writes them absolute."""
    from looplab.adapters.tasks import load_task
    run_dir, cwd = tmp_path / "run", tmp_path / "cwd"
    for base, rows in ((run_dir, "a,target\n1,0\n"), (cwd, "a,target\n9,9\n")):
        base.mkdir()
        (base / "data.csv").write_text(rows, encoding="utf-8")
    snapshot = run_dir / "task.snapshot.json"
    snapshot.write_text(json.dumps({"kind": "dataset", "goal": "g", "direction": "max",
                                    "data_path": "data.csv"}), encoding="utf-8")
    monkeypatch.chdir(cwd)
    task = load_task(snapshot, existing_run=True)          # no "names two different things"
    assert Path(task.data_path).resolve() == (cwd / "data.csv").resolve(), "read as it always was"


def test_paths_are_found_by_field_and_a_malformed_field_is_left_to_validation(tmp_path, monkeypatch):
    """Code review: slots were chosen from the raw `kind`, so a kind-less task, a bare-string
    `dataset:` and a `{name: {path}}` dataset spec stayed cwd-relative beside resolved siblings; and
    `editables: 5` was iterated into a TypeError traceback instead of a one-line refusal."""
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    for name in ("r", "d.csv", "x.csv"):
        (task_dir / name).touch()
    monkeypatch.chdir(tmp_path)
    out = resolve_task_paths({"repo": "r", "dataset": "d.csv", "data": {"x": {"path": "x.csv"}}},
                             task_dir)
    assert out["repo"] == str(task_dir / "r") and out["dataset"] == str(task_dir / "d.csv")
    assert out["data"]["x"]["path"] == str(task_dir / "x.csv")
    for bad in ({"kind": "repo", "editables": 5}, {"kind": "repo", "references": True},
                {"kind": "dataset", "data": 7}):
        assert resolve_task_paths(bad, task_dir) == bad
    result = _invoke("run", _write(tmp_path / "bad.json", {"kind": "repo", "editable_path": "t",
                                                            "editables": 5, "goal": "g"}),
                     "--backend", "toy", "--out", tmp_path / "out")
    assert result.exit_code == 2 and "Traceback" not in result.output, result.output


def _write(path: Path, task: dict) -> Path:
    path.write_text(json.dumps(task), encoding="utf-8")
    return path
