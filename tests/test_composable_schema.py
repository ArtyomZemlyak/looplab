"""The composable task schema (redesign): normalize_task infers the task from WHICH capability fields
are present (repo/dataset/cmd/kaggle/benchmark) instead of a `kind` enum, with `metric.reader` (and the
"auto" onboarding fold), `%params%` expansion, and cmd-authoritative stages. Every legacy spelling
(kind/eval/onboard/editable_path/metric.kind) still parses — so old snapshots/examples keep working."""
from __future__ import annotations

import pytest

from looplab.adapters.tasks import normalize_task, validate_task
from looplab.runtime.command_eval import expand_params, build_command


# --------------------------------------------------------------------------- normalize / inference
def test_composable_repo_infers_kind_and_maps_fields():
    n = normalize_task({
        "goal": "opt", "direction": "max",
        "repo": "/repo",
        "dataset": {"d": "/data/d", "m": "/models/m"},
        "cmd": {"command": ["python", "t.py"], "metric": {"reader": "stdout_json", "key": "r"}, "timeout": 9000},
    })
    assert n["kind"] == "repo"
    assert n["editable_path"] == "/repo"
    assert n["edit_surface"] == ["**/*"]                      # composable repo = full freedom
    assert n["data"] == {"d": "/data/d", "m": "/models/m"}
    assert n["eval"]["metric"] == {"kind": "stdout_json", "key": "r"}   # reader -> kind
    assert n["eval"]["timeout"] == 9000


def test_cmd_bare_list_and_dataset_bare_path():
    n = normalize_task({"repo": "/repo", "direction": "max", "cmd": ["python", "run.py"],
                        "dataset": "/data/foo.csv"})
    assert n["eval"]["command"] == ["python", "run.py"]
    assert n["data"] == {"dataset": "/data/foo.csv"}          # bare path -> ./dataset mount


def test_metric_reader_auto_folds_to_onboard():
    n = normalize_task({"repo": "/repo", "direction": "max",
                        "cmd": {"command": ["python", "train.py"], "metric": {"reader": "auto"}}})
    assert n["onboard"] is True
    assert n["onboard_command"] == ["python", "train.py"]
    assert n["eval"] is None                                  # onboarder builds the eval spec


def test_kaggle_infers_competition():
    n = normalize_task({"goal": "win", "direction": "max", "kaggle": "spooky-author-identification"})
    assert n["kind"] == "mlebench_real"
    assert n["competition"] == "spooky-author-identification"
    assert "kaggle" not in n


def test_benchmark_and_dataset_only():
    assert normalize_task({"benchmark": "quadratic", "direction": "min"})["kind"] == "quadratic"
    n = normalize_task({"goal": "g", "direction": "max", "dataset": "/d.csv"})
    assert n["kind"] == "dataset"


def test_legacy_schema_passes_through_unchanged():
    legacy = {"kind": "repo", "editable_path": "/repo", "direction": "max",
              "eval": {"command": ["python", "t.py"], "metric": {"kind": "stdout_json", "key": "r"}}}
    n = normalize_task(legacy)
    assert n["kind"] == "repo" and n["editable_path"] == "/repo"
    assert n["eval"]["metric"] == {"kind": "stdout_json", "key": "r"}   # untouched


def test_normalize_is_idempotent():
    once = normalize_task({"repo": "/repo", "direction": "max", "cmd": ["python", "t.py"]})
    assert normalize_task(dict(once)) == once


# --------------------------------------------------------------------------- end-to-end validation
def test_composable_repo_validates_to_repotask(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    t = validate_task({"goal": "opt", "direction": "max", "repo": str(repo),
                       "dataset": {"d": str(tmp_path)},
                       "cmd": {"command": ["python", "t.py"],
                               "metric": {"reader": "stdout_json", "key": "r"}, "timeout": 8000}})
    assert t.kind == "repo"
    assert t.eval_spec()["timeout"] == 8000
    assert list(t.data.keys()) == ["d"]


def test_repo_without_cmd_or_auto_is_rejected(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    with pytest.raises(ValueError, match="no `cmd`"):
        validate_task({"goal": "x", "direction": "max", "repo": str(repo)})


# --------------------------------------------------------------------------- %params% expansion
def test_expand_params_token_replaced_with_flags():
    out = expand_params(["python", "train.py", "%params%", "--epochs", "20"], {"lr": 0.001, "wd": 0.1})
    assert out == ["python", "train.py", "--lr", "0.001", "--wd", "0.1", "--epochs", "20"]


def test_expand_params_no_token_is_unchanged():
    argv = ["python", "train.py", "--lr", "0.001"]
    assert expand_params(argv, {"lr": 0.5}) == argv          # params baked -> not injected


def test_expand_params_token_no_params_drops_token():
    assert expand_params(["python", "t.py", "%params%"], {}) == ["python", "t.py"]


def test_build_command_expands_params_token():
    cmd, to = build_command({"command": ["python", "t.py", "%params%"], "timeout": 9000},
                            {"lr": 0.001, "epochs": 20})
    assert cmd == ["python", "t.py", "--lr", "0.001", "--epochs", "20"]
    assert to == 9000


# --------------------------------------------------------------------------- declare_stages tool
def _writetools():
    from looplab.adapters.repo_developer import RepoWriteTools
    return RepoWriteTools(surface=["**/*"], protected=[],
                          editables=[{"name": ".", "path": "/tmp", "surface": ["**/*"], "protect": []}])


def test_declare_stages_valid_stages_stage_manifest():
    t = _writetools()
    msg = t.execute("declare_stages", {"stages": [
        {"name": "data_prep", "command": ["python", "prep.py"]},
        {"name": "train", "command": ["python", "train.py", "%params%"], "timeout": 14400, "check": True},
    ]})
    assert "declared 2" in msg and "score (operator cmd)" in msg
    import json
    manifest = json.loads(t.files["looplab_stages.json"])
    assert [s["name"] for s in manifest["stages"]] == ["data_prep", "train"]
    assert manifest["stages"][1]["timeout"] == 14400.0 and manifest["stages"][1]["check"] is True


@pytest.mark.parametrize("bad,needle", [
    ([{"name": "score", "command": ["python", "x.py"]}], "reserved"),
    ([{"name": "train", "command": "python train.py"}], "list of string"),
    ([], "non-empty array"),
    ([{"command": ["python", "x.py"]}], "no `name`"),
    ([{"name": "a", "command": ["x"]}, {"name": "a", "command": ["y"]}], "duplicate"),
])
def test_declare_stages_reports_errors(bad, needle):
    t = _writetools()
    msg = t.execute("declare_stages", {"stages": bad})
    assert msg.startswith("(refused") and needle in msg
    assert "looplab_stages.json" not in t.files            # nothing staged on error
