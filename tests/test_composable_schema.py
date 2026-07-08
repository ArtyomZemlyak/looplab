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


def test_metric_reader_alias_applies_to_sub_readers():
    # review fix: reader→kind must reach metrics/constraints/cross_check, not just the primary metric,
    # else a `reader:`-spelled sub-reader silently defaults to stdout_json at eval time.
    e = normalize_task({"repo": "/r", "direction": "max", "cmd": {
        "command": ["python", "r.py"], "metric": {"reader": "stdout_json", "key": "s"},
        "metrics": {"lat": {"reader": "file_json", "path": "m.json", "key": "lat"}},
        "constraints": [{"reader": "file_json", "path": "m.json", "key": "lat", "max": 100}],
        "cross_check": {"reader": "stdout_regex", "pattern": "acc=([0-9.]+)"}}})["eval"]
    assert e["metric"]["kind"] == "stdout_json"
    assert e["metrics"]["lat"]["kind"] == "file_json" and "reader" not in e["metrics"]["lat"]
    assert e["constraints"][0]["kind"] == "file_json"
    assert e["cross_check"]["kind"] == "stdout_regex"


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


def test_no_capability_field_is_rejected_not_quadratic():
    # mega-review fix: a typo'd capability field (repo_path instead of repo) used to silently fall
    # back to the quadratic toy and burn the run's budget on (x-3)^2. The old /api/start kind-guard's
    # promise ("no silent default to quadratic") is now enforced in normalize_task itself.
    with pytest.raises(ValueError, match="cannot infer the task"):
        normalize_task({"goal": "tune my model", "direction": "max", "repo_path": "/home/me/proj"})
    # an explicit toy task still works (kind or benchmark spelled out)
    assert normalize_task({"kind": "quadratic", "direction": "min"})["kind"] == "quadratic"


def test_string_cmd_is_rejected_with_actionable_error():
    # mega-review fix: dict("python test.py") used to raise a cryptic 'dictionary update sequence'
    # ValueError — an unhandled 500 on /api/start and a TUI crash. Now a clear, catchable message.
    with pytest.raises(ValueError, match="argv list"):
        normalize_task({"goal": "g", "direction": "max", "repo": "/r", "cmd": "python test.py"})


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


def test_declare_stages_works_under_restricted_surface():
    # mega-review fix: the manifest is TOOL-owned and validated, so it is gated on the PROTECT list
    # only — the legacy default surface ["**/*.py"] can never match a root .json, and a surface gate
    # made the REQUIRED tool refuse on every legacy repo task (the stage pipeline never activated).
    from looplab.adapters.repo_developer import RepoWriteTools
    t = RepoWriteTools(surface=["**/*.py"], protected=[],
                       editables=[{"name": ".", "path": "/tmp", "surface": ["**/*.py"], "protect": []}])
    msg = t.execute("declare_stages", {"stages": [{"name": "train", "command": ["python", "t.py"]}]})
    assert msg.startswith("declared 1")
    assert "looplab_stages.json" in t.files


def test_declare_stages_respects_protect_list():
    # An operator may explicitly protect the manifest to disable Developer pipelines.
    from looplab.adapters.repo_developer import RepoWriteTools
    t = RepoWriteTools(surface=["**/*"], protected=["looplab_stages.json"],
                       editables=[{"name": ".", "path": "/tmp", "surface": ["**/*"], "protect": []}])
    msg = t.execute("declare_stages", {"stages": [{"name": "train", "command": ["python", "t.py"]}]})
    assert msg.startswith("(refused") and "protected" in msg
    assert "looplab_stages.json" not in t.files


# --------------------------------------------------------------------------- operator cmd.stages
def test_cmd_stages_only_validates_and_is_canonical(tmp_path):
    # mega-review fix: the documented {"stages": [...]} cmd form used to be REJECTED ('command Field
    # required') and a command+stages cmd silently DROPPED the stages (EvalSpec had no such field).
    repo = tmp_path / "r"; repo.mkdir()
    t = validate_task({"goal": "g", "direction": "max", "repo": str(repo),
                       "cmd": {"stages": [
                           {"name": "train", "command": ["python", "train.py", "%params%"], "timeout": 7200},
                           {"name": "score", "command": ["python", "score.py"]}],
                           "metric": {"reader": "stdout_json", "key": "m"}}})
    es = t.eval_spec()
    assert [s["name"] for s in es["stages"]] == ["train", "score"]   # survives model_dump
    assert es["stages"][0]["timeout"] == 7200.0


def test_cmd_stages_reach_the_engine_pipeline(tmp_path):
    # The operator's stages ARE the pipeline: _resolve_stages returns them (with %params% expanded)
    # and IGNORES a Developer manifest on disk.
    import json
    from looplab.engine.orchestrator import Engine
    es = {"stages": [{"name": "train", "command": ["python", "train.py", "%params%"]},
                     {"name": "score", "command": ["python", "score.py"]}],
          "command": [], "timeout": 600.0}
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "sneak", "command": ["python", "x.py"]}]}), encoding="utf-8")
    stages = Engine._resolve_stages(object.__new__(Engine), str(tmp_path), es, {"lr": 0.1})
    assert [s["name"] for s in stages] == ["train", "score"]         # dev manifest ignored
    assert stages[0]["command"] == ["python", "train.py", "--lr", "0.1"]


def test_engine_rejects_invalid_dev_manifest(tmp_path):
    # mega-review fix: a hand-written manifest bypassing declare_stages used to be consumed
    # unvalidated — a stage named 'score' would produce TWO score stages (score.log clobbered,
    # stage-scoped re-runs confused) and a full-scorer stage double-ran the eval. The engine now
    # runs the SAME shared validator and falls back to the single command.
    import json
    from looplab.engine.orchestrator import Engine
    es = {"command": ["python", "score.py"], "timeout": 600.0}
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "score", "command": ["python", "fake_score.py"]}]}),
        encoding="utf-8")
    assert Engine._resolve_stages(object.__new__(Engine), str(tmp_path), es, {}) is None
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "a", "command": ["x"]}, {"name": "a", "command": ["y"]}]}),
        encoding="utf-8")
    assert Engine._resolve_stages(object.__new__(Engine), str(tmp_path), es, {}) is None


def test_engine_accepts_valid_dev_manifest(tmp_path):
    import json
    from looplab.engine.orchestrator import Engine
    es = {"command": ["python", "score.py"], "timeout": 600.0}
    (tmp_path / "looplab_stages.json").write_text(
        json.dumps({"stages": [{"name": "train", "command": ["python", "train.py"]}]}), encoding="utf-8")
    stages = Engine._resolve_stages(object.__new__(Engine), str(tmp_path), es, {},
                                    score_cmd=["python", "score.py"], score_timeout=60.0)
    assert [s["name"] for s in stages] == ["train", "score"]
    assert stages[-1] == {"name": "score", "command": ["python", "score.py"], "timeout": 60.0}


def test_cmd_without_command_or_stages_is_rejected(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    with pytest.raises(ValueError, match="command.*or.*stages|stages.*pipeline"):
        validate_task({"goal": "g", "direction": "max", "repo": str(repo),
                       "cmd": {"metric": {"reader": "stdout_json", "key": "m"}}})


# --------------------------------------------------------------------------- review-fix regressions
def test_dataset_bare_path_does_not_clobber_explicit_mount():
    n = normalize_task({"repo": "/r", "direction": "max", "cmd": ["python", "x.py"],
                        "data": {"dataset": "/A"}, "dataset": "/B"})
    assert n["data"]["dataset"] == "/A" and n["data"]["dataset2"] == "/B"   # both kept, no clobber


def test_dataset_with_string_data_does_not_crash():
    n = normalize_task({"repo": "/r", "direction": "max", "cmd": ["python", "x.py"],
                        "data": "ignored-str", "dataset": "/B"})
    assert n["data"] == {"dataset": "/B"}                 # string `data` ignored, no crash


def test_build_command_no_double_inject_with_token_and_params_style():
    cmd, _ = build_command({"command": ["python", "t.py", "%params%"], "params_style": "cli_overrides"},
                           {"lr": 0.001})
    assert cmd == ["python", "t.py", "--lr", "0.001"]      # only the token form, not also lr=0.001


def test_per_source_data_permissions(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    src = tmp_path / "d"; src.mkdir()
    t = validate_task({"goal": "g", "direction": "max", "repo": str(repo),
                       "cmd": {"command": ["python", "t.py"], "metric": {"reader": "stdout_json", "key": "r"}},
                       "dataset": {
                           "raw": {"path": str(src), "mount": True, "edit": False},
                           "work": {"path": str(src), "mount": False, "edit": True},
                           "bare": str(src)}})   # bare path -> all defaults
    assert t.data["raw"].mount and not t.data["raw"].edit
    assert not t.data["work"].mount and t.data["work"].edit
    assert t.data["bare"].mount and not t.data["bare"].edit and t.data["bare"].copy_modify   # defaults
    prot = t.repo_spec()["protected_names"]
    assert "raw" in prot and "raw/**" in prot and "bare/**" in prot   # non-edit sources protected
    assert "work" not in prot and "work/**" not in prot              # edit=true source stays writable
    assert "read-only mount" in t._data_brief() and "writable copy" in t._data_brief()
    # the protection must be ENFORCED by the in-house Developer's write gate (exact-match mode) — a
    # `dir/**` protect entry has to guard the whole tree, not just the literal string.
    from looplab.adapters.repo_developer import RepoWriteTools
    rs = t.repo_spec()
    wt = RepoWriteTools(surface=rs["edit_surface"], protected=rs["protected_names"],
                        editables=[{"name": ".", "path": str(repo), "surface": rs["edit_surface"],
                                    "protect": rs["protected_names"]}])
    assert "protected" in wt.execute("write_file", {"path": "raw/x.csv", "content": "a"})       # non-edit refused
    assert "protected" in wt.execute("write_file", {"path": "raw/sub/y.csv", "content": "a"})   # nested refused
    assert "wrote" in wt.execute("write_file", {"path": "work/x.csv", "content": "a"})          # edit=true OK


def test_api_start_infers_kind_for_composable_task():
    # review fix: /api/start used to 400 a kind-less task; it now infers the kind via normalize_task.
    from looplab.adapters.tasks import normalize_task, kinds
    kind = normalize_task({"goal": "g", "direction": "max", "repo": "/r",
                           "cmd": ["python", "t.py"]}).get("kind")
    assert kind == "repo" and kind in kinds()
