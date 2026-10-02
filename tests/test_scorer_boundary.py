"""The declared scorer set is protected and observes copied bytes, never inferred closure."""
import hashlib
import json
import os

import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.adapters.tasks import validate_task
from looplab.core.scorer_boundary import normalize_scorer_boundary
from looplab.core.setup_identity import setup_config_hash
from looplab.engine import workspace_seed
from looplab.engine.bundle import export_bundle, verify_bundle
from looplab.harness.contract import candidate_surface_refusal
from test_eval_entrypoint_protection import _write_tools
from test_seeded_base_revision import _seeder, _node

_METRIC = {"kind": "stdout_json", "key": "metric"}


def _task(src, files, **kwargs):
    return RepoTask(goal="boundary", editable_path=str(src), edit_surface=["**/*"],
        protect=[], eval=EvalSpec(command=["python", "score.py"], metric=_METRIC,
            protect_entrypoint=False, scorer_boundary={"files": files}), **kwargs)


@pytest.mark.parametrize("files", [[], ["../score.py"], ["/score.py"], ["C:score.py"],
    ["sub\\score.py"], ["./score.py"], ["sub//score.py"], ["*.py"], ["NUL.py"],
    [".git/config"], ["score.py", "SCORE.py"], ["a."], ["a /x"], [3], ["x"] * 129])
def test_declaration_refuses_unsafe_or_ambiguous_files(files):
    with pytest.raises(ValueError, match="scorer_boundary"):
        normalize_scorer_boundary({"files": files})


@pytest.mark.parametrize("value", [{}, {"files": "score.py"}, {"files": ["score.py"], "complete": True}])
def test_declaration_shape_is_operator_owned(value):
    with pytest.raises(ValueError, match="scorer_boundary"):
        EvalSpec(command=["python", "score.py"], metric=_METRIC, scorer_boundary=value)


def test_helper_protected_without_entrypoint_inference_or_manual_protect(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path, mode="none")
    (src / "rules.py").write_text("RULE=1\n")
    task = _task(src, ["rules.py", "score.py"])
    engine._repo_spec = task.repo_spec()
    tools = _write_tools(task)
    assert "protected" in tools.execute("write_file", {"path": "rules.py", "content": "RULE=2"})
    assert candidate_surface_refusal(task.repo_spec(), {"rules.py": "RULE=2"}, [])
    assert candidate_surface_refusal(task.repo_spec(), {}, ["rules.py"])
    wd = run / "nodes" / "node_0"
    receipt = seeder.materialize(_node({"rules.py": "RULE=2", "config.json": "{}"}), wd)
    deleted = _node()
    deleted.deleted = ["rules.py"]
    seeder.write_node_files(deleted, wd)
    assert (wd / "rules.py").read_text() == "RULE=1\n"
    assert receipt["scorer_boundary"]["complete"]
    first = receipt["scorer_boundary"]["digest"]
    (src / "rules.py").write_text("RULE=3\n")
    second = seeder.materialize(_node(), run / "nodes" / "node_1")
    assert second["scorer_boundary"]["digest"] != first
    assert (wd / "score.py").read_bytes() == (src / "score.py").read_bytes()


def test_workspace_relative_declaration_ignores_eval_cwd_and_maps_multi_editables(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "score.py").write_text("score")
    (b / "rules.json").write_text("rules")
    task = RepoTask(goal="multi", editables=[{"name": "app", "path": str(a)},
        {"name": "eval", "path": str(b)}], eval=EvalSpec(command=["python", "score.py"],
            cwd="app", metric=_METRIC, protect_entrypoint=False,
            scorer_boundary={"files": ["app/score.py", "eval/rules.json"]}))
    spec = task.repo_spec()
    assert "app/score.py" in spec["protected_names"] and "eval/rules.json" in spec["protected_names"]
    rows = workspace_seed.seed_candidate_workspace(spec, tmp_path / "wd", seed_mode="none", capture_base_revision=True)
    boundary = rows[0]["base_revision"]["scorer_boundary"]
    assert boundary["complete"] and boundary["declared_files"] == ["app/score.py", "eval/rules.json"]
    # A one-character named mount must win over the root mount, whose prefix is empty.
    mixed = RepoTask(goal="mixed", editable_path=str(a), editables=[{"name": "b", "path": str(b)}],
        eval=EvalSpec(command=["python", "score.py"], metric=_METRIC, protect_entrypoint=False,
            scorer_boundary={"files": ["b/rules.json"]}))
    assert mixed.repo_spec()["editables"][1]["protect"] == ["rules.json"]
    assert "b/rules.json" not in mixed.repo_spec()["editables"][0]["protect"]
    with pytest.raises(ValueError, match="declared editable"):
        task.model_validate({**task.model_dump(), "eval": {**task.eval.model_dump(),
            "scorer_boundary": {"files": ["outside.py"]}}})


def test_declared_file_must_exist_at_submit_but_missing_source_does_not_refuse_resume(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path)
    with pytest.raises(ValueError, match="must exist"):
        _task(src, ["missing.py"])
    task = _task(src, ["score.py"])
    snapshot = task.model_dump(mode="json")
    (src / "score.py").unlink()
    restored = validate_task(snapshot, existing_run=True)
    engine._repo_spec = restored.repo_spec()
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    from looplab.serve.appstate import _public_state_value
    assert _public_state_value(receipt["scorer_boundary"]) == receipt["scorer_boundary"]
    assert "score.py" in engine._repo_spec["protected_names"]
    assert receipt["scorer_boundary"]["reason"] == "declared_file_missing"
    assert receipt["scorer_boundary"]["digest"] is None


def test_data_namespace_is_not_a_scorer_source(tmp_path):
    src, _, _, _ = _seeder(tmp_path)
    (src / "data").mkdir()
    (src / "data" / "rules.json").write_text("rules")
    with pytest.raises(ValueError, match="data/reference"):
        _task(src, ["data/rules.json"], data={"data": str(src / "data")})


def test_boundary_uses_the_same_read_even_if_copied_file_changes_after_observation(tmp_path, monkeypatch):
    src, run, engine, seeder = _seeder(tmp_path)
    engine._repo_spec = _task(src, ["score.py"]).repo_spec()
    real = workspace_seed.seeded_base_revision
    calls = []
    def observe(root, *, on_file=None):
        calls.append(root)
        receipt = real(root, on_file=on_file)
        if root == run / "nodes" / "node_0":
            (root / "score.py").write_text("changed after observation")
        return receipt
    monkeypatch.setattr(workspace_seed, "seeded_base_revision", observe)
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    boundary = receipt["scorer_boundary"]
    assert boundary["complete"] and boundary["members"][0]["sha256"] == hashlib.sha256((src / "score.py").read_bytes()).hexdigest()
    assert boundary["members"][0]["sha256"] != hashlib.sha256((run / "nodes" / "node_0" / "score.py").read_bytes()).hexdigest()
    # One observation of the candidate; archive publication may independently verify staging.
    assert calls.count(run / "nodes" / "node_0") == 1


def test_incomplete_seed_cannot_confirm_a_partial_boundary(tmp_path, monkeypatch):
    src, run, engine, seeder = _seeder(tmp_path)
    engine._repo_spec = _task(src, ["score.py"]).repo_spec()
    monkeypatch.setattr(workspace_seed, "MAX_BASE_REVISION_BYTES", 1)
    boundary = seeder.materialize(_node(), run / "nodes" / "node_0")["scorer_boundary"]
    assert boundary["complete"] is False and boundary["digest"] is None and boundary["members"] == []
    assert boundary["reason"] == "seed_identity_unavailable"


def test_legacy_serialization_and_setup_identity_are_unchanged(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path)
    task = RepoTask(goal="legacy", editable_path=str(src), eval=EvalSpec(command=["python", "score.py"], metric=_METRIC))
    payload = task.model_dump(mode="json")
    assert "scorer_boundary" not in payload["eval"] and "scorer_boundary" not in task.repo_spec()
    assert setup_config_hash(payload) == setup_config_hash(json.loads(task.model_dump_json()))
    assert "scorer_boundary" not in seeder.materialize(_node(), run / "nodes" / "node_0")


def test_boundary_receipt_preserved_in_event_replay_and_bundle(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path)
    engine._repo_spec = _task(src, ["score.py"]).repo_spec()
    engine.store.append("run_started", {"run_id": "boundary", "task_id": "repo", "goal": "boundary", "direction": "min"})
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft"}, "files": {}, "code": ""})
    receipt = seeder.materialize(_node(), run / "nodes" / "node_0")
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1., "violations": [],
        "metric_provenance": {"base_revision": receipt}})
    from looplab.events.replay import fold
    assert fold(engine.store.read_all()).nodes[0].metric_provenance["base_revision"] == receipt
    out = tmp_path / "bundle"
    export_bundle(run, out)
    index = json.loads((out / "base_snapshots" / "index.json").read_text())
    assert index["entries"][1]["receipt"]["scorer_boundary"] == receipt["scorer_boundary"]
    assert verify_bundle(out) == []


def test_linked_declared_source_is_refused(tmp_path):
    src, _, _, _ = _seeder(tmp_path)
    try:
        (src / "link.py").symlink_to(src / "score.py")
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ValueError, match="without links"):
        _task(src, ["link.py"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics")
def test_boundary_includes_executable_bits(tmp_path):
    src, run, engine, seeder = _seeder(tmp_path)
    engine._repo_spec = _task(src, ["score.py"]).repo_spec()
    first = seeder.materialize(_node(), run / "nodes" / "node_0")["scorer_boundary"]
    (src / "score.py").chmod(0o755)
    second = seeder.materialize(_node(), run / "nodes" / "node_1")["scorer_boundary"]
    assert first["digest"] != second["digest"]
