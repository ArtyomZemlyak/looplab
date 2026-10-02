"""Initial archive choice is explicit, verified and immutable for a launched run."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.adapters.tasks import validate_task
from looplab.core.atomicio import rmtree_readonly_aware
from looplab.core.errors import ConfigRefusal
from looplab.engine import seed_base, workspace_seed
from looplab.engine.bundle import export_bundle, verify_bundle
from looplab.engine.workspace import WorkspaceSeeder
from looplab.events.eventstore import EventStore
from test_seeded_base_revision import _seeder, _node


def _origin(tmp_path, *, start=True):
    src, run, engine, seeder = _seeder(tmp_path)
    if start:
        engine.store.append("run_started", {"run_id": "origin", "task_id": "repo", "goal": "pin", "direction": "min"})
    revision = seeder.materialize(_node(), run / "nodes" / "node_0")
    selector = {"run_dir": str(run.resolve()), "event_seq": revision["seed_event_seq"], "digest": revision["digest"]}
    return src, run, revision, selector


def _task(src, selector, **kwargs):
    return RepoTask(goal="pin", direction="min", editable_path=str(src), edit_surface=["**/*"],
        seed_base=selector, eval=EvalSpec(command=["python", "score.py"],
            metric={"kind": "stdout_json", "key": "metric"},
            scorer_boundary={"files": ["score.py"]}), **kwargs)


@pytest.mark.parametrize("change", [{"event_seq": True}, {"event_seq": -1}, {"digest": "a"},
    {"digest": "A" * 64}, {"run_dir": "relative"}, {"receipt": {}}, {"run_dir": None}])
def test_selector_shape_is_not_a_supplied_receipt(tmp_path, change):
    _, _, _, selector = _origin(tmp_path)
    with pytest.raises(ValueError, match="seed_base"):
        seed_base.normalize_seed_base({**selector, **change})


def test_pinned_bytes_survive_live_source_loss_and_every_seed_mode(tmp_path):
    src, origin, revision, selector = _origin(tmp_path)
    task = _task(src, selector)
    spec = task.repo_spec()
    assert spec["editables"][0]["path"] == str(origin / revision["archive"]["path"])
    assert spec["editables"][0]["origin_path"] == str(src)
    (src / "experiment.env").write_text("live drift")
    rmtree_readonly_aware(src)
    restored = validate_task(task.model_dump(), existing_run=True)
    for i, mode in enumerate(("none", "tracked", "auto", "all")):
        rows = workspace_seed.seed_candidate_workspace(restored.repo_spec(), tmp_path / f"wd{i}",
            seed_mode=mode, ignore=lambda directory, names: names, capture_base_revision=True)
        receipt = rows[0]["base_revision"]
        assert receipt["digest"] == revision["digest"]
        assert receipt["selection"] == {"kind": "recorded_seed", **selector}
        assert (tmp_path / f"wd{i}" / "experiment.env").read_text() == "VALUE=old\n"
        assert rows[0]["mode"] == "pinned" and receipt["scorer_boundary"]["complete"]
    # Developer/probe callers with capture disabled still verify and copy the same base.
    workspace_seed.seed_candidate_workspace(spec, tmp_path / "probe", capture_base_revision=False)
    assert (tmp_path / "probe" / "score.py").read_bytes() == (origin / revision["archive"]["path"] / "score.py").read_bytes()


@pytest.mark.parametrize("fault", ["missing", "corrupt", "digest", "sequence", "log", "tail"])
def test_bad_origin_refuses_before_copy_with_no_live_fallback(tmp_path, fault):
    src, origin, revision, selector = _origin(tmp_path)
    archive = origin / revision["archive"]["path"]
    if fault == "missing":
        rmtree_readonly_aware(archive)
    elif fault == "corrupt":
        (archive / "score.py").write_text("corrupt")
    elif fault == "digest":
        selector["digest"] = "0" * 64
    elif fault == "sequence":
        selector["event_seq"] = 999
    else:
        with (origin / "events.jsonl").open("ab") as f:
            f.write(b"broken\n" if fault == "log" else b'{"seq":')
    out = tmp_path / "candidate"
    out.mkdir()
    with pytest.raises(ConfigRefusal, match="seed_base"):
        seed_base.seed_pinned_workspace(selector, [{"name": ".", "path": str(src)}], out)
    assert not list(out.iterdir())
    assert (src / "score.py").read_text() == "print(1)\n"


def test_event_zero_is_readable_but_cannot_select_a_run_start(tmp_path):
    _, origin, revision, selector = _origin(tmp_path)
    with pytest.raises(ConfigRefusal, match="workspace_seeded"):
        seed_base.selected_seed_base({**selector, "event_seq": 0})
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    _, _, _, first = _origin(standalone, start=False)
    assert seed_base.normalize_seed_base(first)["event_seq"] == 0
    assert seed_base.selected_seed_base(first)[0].is_dir()


def test_complete_batches_are_read_and_later_sequence_gaps_refuse(tmp_path):
    _, origin, _, selector = _origin(tmp_path)
    store = EventStore(origin / "events.jsonl")
    store.append_many([("phase_progress", {"stage": "setup"}), ("phase_progress", {"stage": "idle"})])
    assert seed_base.selected_seed_base(selector)[0].is_dir()
    rows = store.path.read_text().splitlines()
    # Preserve a writer-valid batch, but put a noncontiguous ordinary record after it.
    event = store.append("phase_progress", {"stage": "idle"}).model_dump(mode="json")
    event["seq"] += 1
    store.path.write_text("\n".join([*rows, json.dumps(event)]) + "\n")
    with pytest.raises(ConfigRefusal, match="sequence"):
        seed_base.selected_seed_base(selector)


def test_bounded_origin_read_never_interprets_a_truncated_prefix(tmp_path, monkeypatch):
    _, _, _, selector = _origin(tmp_path)
    monkeypatch.setattr(seed_base, "MAX_PIN_LOG_BYTES", 32)
    with pytest.raises(ConfigRefusal, match="oversized or incomplete"):
        seed_base.selected_seed_base(selector)


def test_files_outside_new_editable_namespace_refuse(tmp_path):
    src, _, _, selector = _origin(tmp_path)
    out = tmp_path / "candidate"
    out.mkdir()
    with pytest.raises(ConfigRefusal, match="editable namespaces"):
        seed_base.seed_pinned_workspace(selector, [{"name": "only", "path": str(src)}], out)
    assert not list(out.iterdir())


def test_source_drift_between_verification_and_read_refuses_scoring(tmp_path, monkeypatch):
    src, origin, revision, selector = _origin(tmp_path)
    real = seed_base.selected_seed_base
    def drift(value):
        archive, receipt = real(value)
        (archive / "experiment.env").write_text("changed after verification")
        return archive, receipt
    monkeypatch.setattr(seed_base, "selected_seed_base", drift)
    out = tmp_path / "candidate"
    out.mkdir()
    with pytest.raises(ConfigRefusal, match="changed during copy"):
        seed_base.seed_pinned_workspace(selector, [{"name": ".", "path": str(src)}], out)


def test_corrupt_destination_write_is_not_accepted(tmp_path, monkeypatch):
    src, _, _, selector = _origin(tmp_path)
    real = seed_base.atomic_write_bytes
    def corrupt(path, data, **kwargs):
        real(path, data + b"wrong", **kwargs)
    monkeypatch.setattr(seed_base, "atomic_write_bytes", corrupt)
    out = tmp_path / "candidate"
    out.mkdir()
    with pytest.raises(ConfigRefusal, match="destination verification"):
        seed_base.seed_pinned_workspace(selector, [{"name": ".", "path": str(src)}], out)


def test_existing_destination_is_preserved(tmp_path):
    src, _, _, selector = _origin(tmp_path)
    out = tmp_path / "candidate"
    out.mkdir()
    (out / "keep").write_text("operator")
    with pytest.raises(ConfigRefusal, match="empty"):
        seed_base.seed_pinned_workspace(selector, [{"name": ".", "path": str(src)}], out)
    assert (out / "keep").read_text() == "operator"


def test_multi_namespace_seed_has_no_duplicate_root_copy(tmp_path):
    src, origin, _, _ = _origin(tmp_path)
    extra = src / "rules"
    extra.mkdir()
    (extra / "rule.json").write_text("rule")
    # A new full seed provides the aggregate root plus a one-character named editable.
    mounts = [{"name": ".", "path": str(src)}, {"name": "a", "path": str(extra)}]
    rows = workspace_seed.seed_candidate_workspace({"editables": mounts}, tmp_path / "seed", capture_base_revision=True,
        base_archive_dir=origin / "base_snapshots")
    revision = rows[0]["base_revision"]
    event = EventStore(origin / "events.jsonl").append("workspace_seeded", {"node_id": 1, "materialized": [], "base_revision": revision})
    selector = {"run_dir": str(origin), "event_seq": event.seq, "digest": revision["digest"]}
    result = workspace_seed.seed_candidate_workspace({"editables": mounts, "seed_base": selector}, tmp_path / "candidate", capture_base_revision=True)
    assert result[0]["base_revision"]["digest"] == revision["digest"]
    assert sum(row["count"] for row in result if row["kind"] == "editable") == revision["file_count"]
    assert (tmp_path / "candidate" / "a" / "rule.json").read_text() == "rule"


def test_launch_pin_cannot_be_changed_added_or_removed_on_resume(tmp_path):
    src, _, _, selector = _origin(tmp_path)
    store = EventStore(tmp_path / "target" / "events.jsonl")
    store.append("run_started", {"run_id": "target", "seed_base": selector})
    before = store.path.read_bytes()
    seed_base.enforce_initial_seed_base(store.read_all(), selector)
    for altered in (None, {**selector, "digest": "0" * 64}):
        with pytest.raises(ConfigRefusal, match="run_started"):
            seed_base.enforce_initial_seed_base(store.read_all(), altered)
    legacy = [SimpleNamespace(type="run_started", data={})]
    with pytest.raises(ConfigRefusal, match="run_started"):
        seed_base.enforce_initial_seed_base(legacy, selector)
    assert before == store.path.read_bytes()


def test_engine_reentry_and_materialize_refuse_pin_changes_before_writes(tmp_path):
    from tests.factories import make_engine
    src, _, _, selector = _origin(tmp_path)
    engine = make_engine(tmp_path / "target")
    engine._repo_spec = _task(src, selector).repo_spec()
    engine.store.append("run_started", {"run_id": "target", "seed_base": selector})
    engine._repo_spec["seed_base"] = {**selector, "digest": "0" * 64}
    before = engine.store.path.read_bytes()
    work = engine.run_dir / "nodes" / "node_0"
    work.mkdir(parents=True)
    (work / "keep").write_text("previous attempt")
    for enter in (engine._enter_run, engine._reentry_repin,
                  lambda: engine.workspace.materialize(_node(), work)):
        with pytest.raises(ConfigRefusal, match="run_started"):
            enter()
        assert engine.store.path.read_bytes() == before
        assert (work / "keep").read_text() == "previous attempt"
    engine._repo_spec["seed_base"] = selector
    rmtree_readonly_aware(seed_base.selected_seed_base(selector)[0])
    for enter in (engine._enter_run, lambda: engine.workspace.materialize(_node(), work)):
        with pytest.raises(ConfigRefusal, match="archive"):
            enter()
        assert engine.store.path.read_bytes() == before
        assert (work / "keep").read_text() == "previous attempt"


def test_real_developer_command_reads_pin_after_source_loss(tmp_path):
    from looplab.tools.dev_commands import DevCommandTools
    src, _, _, selector = _origin(tmp_path)
    task = _task(src, selector, developer_commands=[{
        "name": "read_base", "command": [sys.executable, "-c",
            "from pathlib import Path; print(Path('experiment.env').read_text())"]}])
    tool = DevCommandTools(task.repo_spec())
    rmtree_readonly_aware(src)
    result = tool.execute_result("run_dev_command", {"name": "read_base"})
    assert not result.is_error and "VALUE=old" in result.content
    assert result.structured["workspace"] == "disposable"
    assert not src.exists()


def test_cwd_remap_and_live_source_argv_refusal(tmp_path):
    src, _, _, selector = _origin(tmp_path)
    task = _task(src, selector)
    payload = task.model_dump()
    payload["eval"]["cwd"] = str(src)
    task = RepoTask.model_validate(payload)
    seeder = WorkspaceSeeder(SimpleNamespace(_repo_spec=task.repo_spec()))
    assert seeder.sandbox_cwd(tmp_path / "wd", str(src)) == str((tmp_path / "wd").resolve())
    for cwd in ("../outside", str(tmp_path / "outside")):
        with pytest.raises(ValueError, match="seed_base"):
            RepoTask.model_validate({**payload, "eval": {**payload["eval"], "cwd": cwd}})
    with pytest.raises(ValueError, match="workspace-relative source"):
        RepoTask.model_validate({**payload, "eval": {**payload["eval"], "command": ["python", str(src / "score.py")]}})


def test_bundle_can_be_selected_as_origin_and_legacy_dumps_unchanged(tmp_path):
    src, origin, _, selector = _origin(tmp_path)
    out = tmp_path / "bundle"
    export_bundle(origin, out)
    assert verify_bundle(out) == []
    selector["run_dir"] = str(out)
    task = _task(src, selector)
    assert task.repo_spec()["seed_base"] == selector
    composed = validate_task({"repo": str(src), "goal": "pin", "seed_base": selector,
                             "cmd": task.eval.model_dump()})
    assert composed.repo_spec()["seed_base"] == selector
    assert validate_task(composed.model_dump(), existing_run=True).seed_base == selector
    legacy = RepoTask(goal="legacy", editable_path=str(src))
    assert "seed_base" not in legacy.model_dump() and "seed_base" not in legacy.repo_spec()
    assert "seed_base" not in json.loads(legacy.model_dump_json())


def test_linked_destination_does_not_write_outside(tmp_path):
    src, _, _, selector = _origin(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "candidate"
    try:
        out.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        pytest.skip("Windows symlink privilege unavailable")
    with pytest.raises(ConfigRefusal, match="plain"):
        seed_base.seed_pinned_workspace(selector, [{"name": ".", "path": str(src)}], out)
    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics")
def test_native_executable_bits_are_preserved(tmp_path):
    src, origin, _, _ = _origin(tmp_path)
    (src / "score.py").chmod(0o755)
    rows = workspace_seed.seed_candidate_workspace({"editables": [{"name": ".", "path": str(src)}]},
        tmp_path / "seed", capture_base_revision=True, base_archive_dir=origin / "base_snapshots")
    receipt = rows[0]["base_revision"]
    event = EventStore(origin / "events.jsonl").append("workspace_seeded", {"materialized": [], "base_revision": receipt})
    selector = {"run_dir": str(origin), "event_seq": event.seq, "digest": receipt["digest"]}
    workspace_seed.seed_candidate_workspace({"editables": [{"name": ".", "path": str(src)}], "seed_base": selector}, tmp_path / "copy")
    assert (tmp_path / "copy" / "score.py").stat().st_mode & 0o111 == 0o111
