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


def test_conflicting_repo_editable_path_aliases_error():
    # arch-review §3 P0-5: {repo: NEW, editable_path: OLD} must ERROR, not silently keep OLD.
    with pytest.raises(ValueError, match="conflicting task aliases"):
        normalize_task({"goal": "g", "direction": "max", "repo": "/new", "editable_path": "/old",
                        "cmd": ["python", "t.py"]})
    # equal aliases are fine (redundant but not conflicting)
    n = normalize_task({"goal": "g", "direction": "max", "repo": "/same", "editable_path": "/same",
                        "cmd": ["python", "t.py"]})
    assert n["editable_path"] == "/same"


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


def test_declare_stages_rejects_a_nonexistent_absolute_data_path(tmp_path):
    # The #1 real failure: the Developer copies the repo's argparse-default `--train_dataset
    # /…/train.pck` that isn't on this machine. declare_stages must BOUNCE it (with a "ls the real
    # data" message) so the Developer re-declares with a path that exists — the run doesn't ship a
    # train stage doomed to FileNotFoundError.
    t = _writetools()
    msg = t.execute("declare_stages", {"stages": [
        {"name": "train", "command": ["python", "train.py",
                                       "--train_dataset", "/definitely/not/here/smkt/train.pck"]}]})
    assert msg.startswith("(refused") and "DO NOT EXIST" in msg and "/definitely/not/here/smkt/train.pck" in msg
    assert "looplab_stages.json" not in t.files                # nothing staged

    # a val path embedded in a JSON-string arg is caught too
    msg2 = t.execute("declare_stages", {"stages": [
        {"name": "train", "command": ["python", "train.py",
                                       "--val_datasets", '{"val": "/nope/val.parquet"}']}]})
    assert "DO NOT EXIST" in msg2 and "/nope/val.parquet" in msg2

    # RELATIVE paths (resolve to mounts at eval time) and %params% are NOT flagged; an EXISTING
    # absolute path is fine. Use pytest's platform-native writable temporary directory rather than
    # assuming POSIX `/tmp` exists and is writable on Windows.
    p = tmp_path / "ll_exists.pck"
    p.write_bytes(b"")
    ok = t.execute("declare_stages", {"stages": [
        {"name": "prep", "command": ["python", "prep.py", "--out", "./data/train.pck"]},
        {"name": "train", "command": ["python", "train.py", "--train_dataset", str(p),
                                     "--lr", "%params%"]}]})
    assert ok.startswith("declared")


def test_declare_stages_allows_a_pipeline_produced_intermediate_path():
    # A valid data_prep->train pipeline: prep WRITES /scratch/prep/train.npy (--out), train READS it.
    # Neither exists at declare time, but the read must NOT be flagged "missing" — it's produced by an
    # earlier stage. Only genuinely-hallucinated EXTERNAL inputs (never produced) are bounced.
    t = _writetools()
    ok = t.execute("declare_stages", {"stages": [
        {"name": "prep", "command": ["python", "prep.py", "--out", "/scratch/prep/train.npy"]},
        {"name": "train", "command": ["python", "train.py", "--data", "/scratch/prep/train.npy"]}]})
    assert ok.startswith("declared")
    assert "looplab_stages.json" in t.files
    # …but a stage reading a NON-produced, non-existent absolute path is still bounced.
    t2 = _writetools()
    bad = t2.execute("declare_stages", {"stages": [
        {"name": "train", "command": ["python", "train.py", "--data", "/scratch/prep/train.npy"]}]})
    assert bad.startswith("(refused") and "/scratch/prep/train.npy" in bad


def test_auto_reader_whitespace_command_is_treated_as_absent():
    # A whitespace-only `cmd.command` under the auto reader must NOT become a per-character argv: it slips
    # past the `.strip()` shell-string check but was still truthy at `if _cmd`, so `list("  ")` produced
    # [' ', ' ']. It must be treated as ABSENT (no onboard_command), like an empty string.
    from looplab.adapters.tasks import normalize_task
    d = normalize_task({"goal": "g", "direction": "max", "repo": "/repo",
                        "cmd": {"command": "  ", "metric": {"reader": "auto"}}})
    assert "onboard_command" not in d or not d.get("onboard_command")
    # a REAL shell string still raises (actionable message), not silently char-split
    with pytest.raises(ValueError, match="argv list"):
        normalize_task({"goal": "g", "direction": "max", "repo": "/repo",
                        "cmd": {"command": "python test.py", "metric": {"reader": "auto"}}})
    # a proper argv list is preserved
    d2 = normalize_task({"goal": "g", "direction": "max", "repo": "/repo",
                         "cmd": {"command": ["python", "t.py"], "metric": {"reader": "auto"}}})
    assert d2["onboard_command"] == ["python", "t.py"]


def test_declare_stages_flags_read_before_write_ordering():
    # The produced-path exclusion is stage-ORDER-aware: a path only a LATER stage writes must NOT excuse
    # an EARLIER stage that reads it — that pipeline is a guaranteed FileNotFoundError (train runs first).
    t = _writetools()
    bad = t.execute("declare_stages", {"stages": [
        {"name": "train", "command": ["python", "train.py", "--data", "/scratch/x.npy"]},
        {"name": "export", "command": ["python", "export.py", "--out", "/scratch/x.npy"]}]})
    assert bad.startswith("(refused") and "/scratch/x.npy" in bad


def test_declare_stages_excuses_directory_output_and_unlisted_flag_spellings():
    # A produced intermediate is recognized by ANY output-flag spelling (not a hardcoded list): a
    # DIRECTORY output (`--outdir /scratch/prep`) covers a later read under it, and unlisted spellings
    # (`--export-dir`) are matched by the de-dashed "out/export/save/dest/dump/writ" hint.
    t = _writetools()
    ok = t.execute("declare_stages", {"stages": [
        {"name": "prep", "command": ["python", "prep.py", "--outdir", "/scratch/prep"]},
        {"name": "train", "command": ["python", "train.py", "--data", "/scratch/prep/train.npy"]}]})
    assert ok.startswith("declared")
    t2 = _writetools()
    ok2 = t2.execute("declare_stages", {"stages": [
        {"name": "prep", "command": ["python", "prep.py", "--export-dir", "/scratch/e"]},
        {"name": "train", "command": ["python", "train.py", "--data", "/scratch/e/feats.parquet"]}]})
    assert ok2.startswith("declared")


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


def test_declare_stages_refuses_when_operator_declared_the_pipeline():
    # P12: on a task with operator `cmd.stages` the engine runs the operator pipeline VERBATIM and
    # ignores the Developer manifest — "declaring" one must refuse loudly with the real route to a
    # fix (edit the stage's script), not succeed into a file nobody reads (the repaired node would
    # otherwise re-run the identical pipeline until abandon).
    from looplab.adapters.repo_developer import RepoWriteTools
    t = RepoWriteTools(surface=["**/*"], protected=[], operator_stages=True,
                       editables=[{"name": ".", "path": "/tmp", "surface": ["**/*"], "protect": []}])
    msg = t.execute("declare_stages", {"stages": [{"name": "train", "command": ["python", "t.py"]}]})
    assert msg.startswith("(refused") and "OPERATOR-declared" in msg and "script" in msg
    assert "looplab_stages.json" not in t.files
    # ...and the tool's own description warns up front, so the model needn't burn a call to learn it
    spec = next(s for s in t.specs() if s["function"]["name"] == "declare_stages")
    assert "refuse" in spec["function"]["description"]
    # a task WITHOUT operator stages keeps the plain description (no scary false warning)
    t2 = RepoWriteTools(surface=["**/*"], protected=[],
                        editables=[{"name": ".", "path": "/tmp", "surface": ["**/*"], "protect": []}])
    spec2 = next(s for s in t2.specs() if s["function"]["name"] == "declare_stages")
    assert "OPERATOR-declared" not in spec2["function"]["description"]


def test_write_refusal_distinguishes_data_mount_from_eval_protection():
    # The protect list guards BOTH the operator's eval files and read-only data mounts (defensively —
    # see RepoTask._protected_names); the refusal must name the REAL reason so the model takes the
    # right next step (write derived data elsewhere vs leave the scorer alone).
    from looplab.adapters.repo_developer import RepoWriteTools
    t = RepoWriteTools(surface=["**/*"], protected=["dataset", "dataset/**", "grader.py"],
                       data_mounts=["dataset"],
                       editables=[{"name": ".", "path": "/tmp", "surface": ["**/*"], "protect": []}])
    mount = t.execute("write_file", {"path": "dataset/x.csv", "content": "a,b\n"})
    assert "read-only data mount" in mount and "derived" in mount
    evalp = t.execute("write_file", {"path": "grader.py", "content": "x = 1\n"})
    assert "operator owns the eval" in evalp


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


def test_dataset_dict_collision_with_data_is_rejected():
    # mega-review fix: mounts.update(ds) silently overwrote an explicit `data` mount of the same
    # name (the /A mount vanished and every node evaluated against /B with no error).
    with pytest.raises(ValueError, match="BOTH `data` and `dataset`"):
        normalize_task({"repo": "/r", "direction": "max", "cmd": ["python", "x.py"],
                        "data": {"raw": "/A"}, "dataset": {"raw": {"path": "/B"}}})


def test_kaggle_wins_over_stale_competition():
    # mega-review fix: setdefault kept a stale boss-authored `competition`, so a user editing the
    # Kaggle field launched a different competition than the form displayed.
    n = normalize_task({"goal": "g", "direction": "max",
                        "kaggle": "nomad2018-predict-transparent-conductors",
                        "competition": "spooky-author-identification", "kind": "mlebench_real"})
    assert n["competition"] == "nomad2018-predict-transparent-conductors"


def test_dataset_kind_flattens_permission_objects(tmp_path):
    # mega-review fix: the documented permission-object form for `dataset` values used to bounce
    # off DatasetTask.data (dict[str,str]) with a pydantic type error for non-repo tasks; the
    # permissions are repo machinery, so only the path survives for the dataset kind.
    src = tmp_path / "train.csv"; src.write_text("a,b\n1,2\n", encoding="utf-8")
    t = validate_task({"goal": "g", "direction": "max",
                       "dataset": {"raw": {"path": str(src), "preprocess": True}}})
    assert t.kind == "dataset" and t.data["raw"] == str(src)


def test_dataset_with_string_data_does_not_crash():
    n = normalize_task({"repo": "/r", "direction": "max", "cmd": ["python", "x.py"],
                        "data": "ignored-str", "dataset": "/B"})
    assert n["data"] == {"dataset": "/B"}                 # string `data` ignored, no crash


def test_build_command_no_double_inject_with_token_and_params_style():
    cmd, _ = build_command({"command": ["python", "t.py", "%params%"], "params_style": "cli_overrides"},
                           {"lr": 0.001})
    assert cmd == ["python", "t.py", "--lr", "0.001"]      # only the token form, not also lr=0.001


def test_operator_cmd_stages_survive_validation(tmp_path):
    # review fix (HIGH): EvalSpec had no `stages` field, so an operator-declared cmd.stages was silently
    # dropped and the "cmd declares stages → canonical" branch was dead. They must round-trip now.
    repo = tmp_path / "r"; repo.mkdir()
    t = validate_task({"goal": "g", "direction": "max", "repo": str(repo),
                       "cmd": {"stages": [{"name": "train", "command": ["python", "train.py"], "timeout": 9000},
                                          {"name": "score", "command": ["python", "test.py"]}],
                               "metric": {"reader": "stdout_json", "key": "r"}}})
    es = t.eval_spec()
    assert [s["name"] for s in es["stages"]] == ["train", "score"]   # not stripped
    assert es["command"] == []                                       # command optional when stages given


def test_eval_needs_command_or_stages():
    from looplab.adapters.repo_task import EvalSpec
    with pytest.raises(ValueError, match="command.*OR.*stages|stages"):
        EvalSpec()                                                   # neither → rejected


def test_per_source_data_permissions(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    src = tmp_path / "d"; src.mkdir()
    t = validate_task({"goal": "g", "direction": "max", "repo": str(repo),
                       "cmd": {"command": ["python", "t.py"], "metric": {"reader": "stdout_json", "key": "r"}},
                       "dataset": {
                           "raw": {"path": str(src), "mount": True, "edit": False},
                           "work": {"path": str(src), "mount": False, "edit": True},
                           "copy": {"path": str(src), "mount": False},   # copy-in, edit default False
                           "bare": str(src)}})   # bare path -> all defaults
    assert t.data["raw"].mount and not t.data["raw"].edit
    assert not t.data["work"].mount and t.data["work"].edit
    assert t.data["bare"].mount and not t.data["bare"].edit and t.data["bare"].copy_modify   # defaults
    prot = t.repo_spec()["protected_names"]
    assert "raw" in prot and "raw/**" in prot and "bare/**" in prot   # non-edit MOUNTED sources protected
    assert "work" not in prot and "work/**" not in prot              # edit=true source stays writable
    # mega-review fix: a mount:false source is a PHYSICAL per-node copy the brief calls "a writable
    # copy" — protecting it made copy-in mode unusable (every write under ./copy was refused).
    assert "copy" not in prot and "copy/**" not in prot
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


def test_single_file_writable_copy_is_surfaced_under_a_narrow_surface(tmp_path):
    # A mount:false source that is a single FILE materializes at ./<name> (not ./<name>/…). The surface
    # must carry the BARE name too — `name/**` alone never matches the file path, so under a narrow
    # edit_surface the "writable copy" the brief promises would be refused.
    repo = tmp_path / "r"; repo.mkdir()
    f = tmp_path / "train.csv"; f.write_text("a,b\n", encoding="utf-8")
    t = validate_task({"goal": "g", "direction": "max", "repo": str(repo), "edit_surface": ["src/**"],
                       "cmd": {"command": ["python", "t.py"], "metric": {"reader": "stdout_json", "key": "r"}},
                       "dataset": {"train.csv": {"path": str(f), "mount": False}}})
    surf = t.repo_spec()["edit_surface"]
    assert "train.csv" in surf and "train.csv/**" in surf     # both the bare file and the tree glob
    from looplab.adapters.repo_developer import RepoWriteTools
    rs = t.repo_spec()
    wt = RepoWriteTools(surface=rs["edit_surface"], protected=rs["protected_names"],
                        editables=[{"name": ".", "path": str(repo), "surface": rs["edit_surface"],
                                    "protect": rs["protected_names"]}])
    assert "wrote" in wt.execute("write_file", {"path": "train.csv", "content": "x"})   # the file copy is writable
    assert "surface" in wt.execute("write_file", {"path": "other.py", "content": "x"})  # narrow surface still holds


def test_mount_true_plus_edit_true_is_coerced_to_a_writable_copy(tmp_path):
    # A mounted original is a read-only symlink: the agent's build-time writes to ./name escape the
    # workdir and are dropped, so mount:true + edit:true would silently no-op. COERCE it to mount:false
    # (a writable per-node copy — what edit:true wants) rather than reject, so pre-existing runs whose
    # task.snapshot.json legally carried the combo still `resume`/`finalize` instead of failing to load.
    from looplab.adapters.repo_task import DataSpec
    coerced = DataSpec(path="/d", mount=True, edit=True)
    assert coerced.mount is False and coerced.edit is True               # -> a writable per-node copy
    # `{edit:true}` with mount defaulting to True hits the same coercion
    assert DataSpec(path="/d", edit=True).mount is False
    # the two valid intents still construct unchanged
    assert DataSpec(path="/d", mount=True, edit=False).mount is True     # read-only mount
    assert DataSpec(path="/d", mount=False, edit=True).edit is True      # writable per-node copy
    # a whole task carrying the combo loads (coerced), and the source becomes an agent-writable copy
    repo = tmp_path / "r"; repo.mkdir()
    adapter = validate_task({"goal": "g", "direction": "max", "repo": str(repo),
                             "cmd": {"command": ["python", "t.py"], "metric": {"reader": "stdout_json", "key": "r"}},
                             "dataset": {"d": {"path": str(tmp_path), "mount": True, "edit": True}}})
    assert adapter.data["d"].mount is False and adapter.data["d"].edit is True


def test_docker_wrap_binds_only_symlinked_sources(tmp_path):
    # mega-review fix: edit:false is enforced at the MOUNT layer for sandboxed evals — a symlink-mounted
    # source rides along as a bind (readonly unless edit:true) so the /work symlink resolves. arch-review
    # §4 P1-8: only ACTUAL symlinks are bound (a copied-in source already rides in /work), and the mount
    # uses `--mount` not the `-v` colon form (malformed for a Windows host path -> "too many colons").
    import os
    import shutil
    import pytest
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.command_eval import make_docker_wrap
    e = object.__new__(Engine)
    e._repo_spec = {"data": {"raw": {"path": "/data/raw", "mount": True, "edit": False},
                             "rw": {"path": "/data/rw", "mount": True, "edit": True},
                             "cp": {"path": "/data/cp", "mount": False, "edit": False},
                             "legacy": "/data/legacy"},
                    "references": [{"name": "lib", "path": "/refs/lib", "mount": True},
                                   {"name": "ctx", "path": "/refs/ctx", "mount": False}]}
    wd = tmp_path / "wd"
    wd.mkdir()
    try:                                              # materialize the mount:true sources AS symlinks
        for name in ("raw", "rw", "legacy", "lib"):   # (dangling is fine — is_symlink() is True)
            os.symlink("/nonexistent/target", wd / name)
    except (OSError, NotImplementedError):
        pytest.skip("platform can't create symlinks (the copy-in path is exercised elsewhere)")
    (wd / "cp").mkdir()                                # copied-in sources are real dirs, never bound
    (wd / "ctx").mkdir()
    binds = e._data_binds(str(wd))
    assert ("/data/raw", True) in binds and ("/data/rw", False) in binds
    assert ("/data/legacy", True) in binds and ("/refs/lib", True) in binds
    assert all(p not in ("/data/cp", "/refs/ctx") for p, _ in binds)   # copy-in / context-only: no bind
    if shutil.which("docker"):                       # argv assembly (docker CLI present only)
        wrap = make_docker_wrap(str(tmp_path), "img", binds=binds)
        argv = wrap(["python", "x.py"], str(tmp_path))
        assert "--mount" in argv                                     # extra binds use --mount, not -v colon
        assert "type=bind,src=/data/raw,dst=/data/raw,readonly" in argv
        assert "type=bind,src=/data/rw,dst=/data/rw" in argv          # edit:true -> writable (no readonly)
        # only the /work bind uses `-v`; no extra data bind does (that colon form was the P1-8 crash)
        assert [a for a in argv if a == "-v"] and argv[argv.index("-v") + 1].endswith(":/work")


def test_data_binds_skips_copied_in_sources(tmp_path):
    # arch-review §4 P1-8: a mount:true source that ended up COPIED (Windows os.symlink fallback) must
    # not be bound — it already lives in /work and its drive path has no Linux container target.
    import os
    from looplab.engine.orchestrator import Engine
    e = object.__new__(Engine)
    e._repo_spec = {"data": {"copied": {"path": "/data/copied", "mount": True, "edit": False}}}
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "copied").mkdir()                            # copied in as a real dir, NOT a symlink
    assert e._data_binds(str(wd)) is None              # nothing to bind


def test_api_start_infers_kind_for_composable_task():
    # review fix: /api/start used to 400 a kind-less task; it now infers the kind via normalize_task.
    from looplab.adapters.tasks import normalize_task, kinds
    kind = normalize_task({"goal": "g", "direction": "max", "repo": "/r",
                           "cmd": ["python", "t.py"]}).get("kind")
    assert kind == "repo" and kind in kinds()


def test_regex_reader_accepts_pattern_in_key():
    # An LLM/operator authoring the composable metric naturally puts the regex in `key` (the field
    # stdout_json uses). normalize must promote it to `pattern` so stdout_regex/file_regex don't crash
    # the eval with KeyError('pattern').
    from looplab.adapters.tasks import normalize_task
    from looplab.runtime.command_eval import read_metric
    t = normalize_task({"goal": "g", "direction": "max", "repo": "examples/repo_example",
                        "cmd": {"command": ["python", "e.py"],
                                "metric": {"reader": "stdout_regex", "key": "R@100: ([0-9.]+)"}}})
    m = t["eval"]["metric"]
    assert m["kind"] == "stdout_regex" and m["pattern"] == "R@100: ([0-9.]+)" and "key" not in m
    assert read_metric("R@100: 0.83\n", "/tmp", m) == 0.83
    # a genuinely malformed regex spec must fail the node (None), not crash the run
    assert read_metric("x", "/tmp", {"kind": "stdout_regex"}) is None


def test_stray_dotted_cmd_key_is_rejected_with_an_actionable_error():
    # The docs describe fields in dotted shorthand (`cmd.setup`, `cmd.profiles`, …); a model — the
    # assistant's propose_run especially — sometimes emits them LITERALLY as top-level keys instead of
    # nesting. Silently dropping them lost the setup with no signal; normalize must REJECT with a
    # message that tells the caller to nest it, so propose_run bounces it back and the assistant fixes
    # itself. A correctly-nested `cmd.setup` still works.
    import pytest
    from looplab.adapters.tasks import normalize_task, validate_task
    with pytest.raises(ValueError) as ei:
        normalize_task({"goal": "g", "direction": "max", "repo": "examples/repo_example",
                        "cmd": {"command": ["python", "e.py"],
                                "metric": {"reader": "stdout_json", "key": "m"}},
                        "cmd.setup": ["pip", "install", "-r", "requirements.txt"]})
    msg = str(ei.value)
    assert "cmd.setup" in msg and "setup" in msg and "cmd" in msg      # names the fix (nest it)
    # the NESTED form is accepted and takes effect
    t = normalize_task({"goal": "g", "direction": "max", "repo": "examples/repo_example",
                        "cmd": {"command": ["python", "e.py"], "setup": ["pip", "install", "."],
                                "metric": {"reader": "stdout_json", "key": "m"}}})
    assert t["eval"]["setup"] == ["pip", "install", "."]
    # and propose_run surfaces the raise to the assistant instead of proposing a broken task
    with pytest.raises(ValueError):
        validate_task({"goal": "g", "direction": "max", "repo": "examples/repo_example",
                       "cmd": {"command": ["python", "e.py"], "metric": {"reader": "stdout_json", "key": "m"}},
                       "cmd.setup": ["x"]})
