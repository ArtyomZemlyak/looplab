"""`eval.artifact_sync`: the operator's copy-out runs after a node's terminal, off the eval slot.

Incident 2026-10-06: a ten-hour training's checkpoint lived only on a mount that went away. Driven
through the real `_evaluate` with a real copy command; the copy never moves the node.
"""
from __future__ import annotations

import sys

import anyio

from factories import make_engine
from looplab.engine import artifact_sync
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult

_COPY = ("import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2]); "
         "sys.exit(int(sys.argv[3]) if len(sys.argv) > 3 else 0)")


def _engine(tmp_path, command):
    engine = make_engine(tmp_path / "run")
    engine._eval_spec = {"artifact_sync": {"command": command, "timeout": 60.0}} if command else {}
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        from pathlib import Path
        (Path(workdir) / "ckpt.bin").write_bytes(b"weights")
        return RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False,
                         stderr="")

    engine._run_eval = fake_run_eval
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    assert artifact_sync.wait_for_inflight(30)
    events = engine.store.read_all()
    return engine, events, fold(events)


def test_a_finished_node_s_workdir_is_copied_and_receipted(tmp_path):
    dest = tmp_path / "durable" / "node_0"
    _engine_, events, st = _engine(
        tmp_path, [sys.executable, "-c", _COPY, "{workdir}", str(tmp_path / "durable") + "/node_{node_id}"])
    assert st.nodes[0].status.value == "evaluated"
    assert (dest / "ckpt.bin").read_bytes() == b"weights"
    rows = [e.data for e in events if e.type == "artifact_synced"]
    assert len(rows) == 1 and rows[0]["exit_code"] == 0 and rows[0]["node_id"] == 0
    assert rows[0]["command"][-1].endswith("/node_0"), "placeholders are rendered in the receipt"
    terminal = max(e.seq for e in events if e.type == "node_evaluated")
    assert min(e.seq for e in events if e.type == "artifact_synced") > terminal, (
        "the copy-out runs AFTER the terminal")


def test_a_failed_copy_is_reported_and_never_moves_the_node(tmp_path):
    _e, events, st = _engine(
        tmp_path, [sys.executable, "-c", _COPY, "{workdir}", str(tmp_path / "d"), "3"])
    assert st.nodes[0].status.value == "evaluated" and st.nodes[0].metric == 0.5
    rows = [e.data for e in events if e.type == "artifact_synced"]
    assert [r["exit_code"] for r in rows] == [3]
    assert (tmp_path / "run" / "artifact_sync.log").exists()


def test_a_missing_tool_is_a_receipt_not_a_crash(tmp_path):
    _e, events, st = _engine(tmp_path, ["/nonexistent/mc", "cp", "{workdir}", "x/"])
    assert st.nodes[0].status.value == "evaluated"
    rows = [e.data for e in events if e.type == "artifact_synced"]
    assert len(rows) == 1 and rows[0]["exit_code"] != 0


def test_nothing_declared_runs_nothing(tmp_path):
    _e, events, st = _engine(tmp_path, None)
    assert st.nodes[0].status.value == "evaluated"
    assert not any(e.type == "artifact_synced" for e in events)


def test_only_known_placeholders_are_rendered():
    argv = artifact_sync.render_argv(
        ["{workdir}", "{run_id}/{node_id}.{generation}", "{unknown}", "{}", "a{b"],
        {"workdir": "/w", "run_dir": "/r", "run_id": "v1", "node_id": 4, "generation": 2})
    assert argv == ["/w", "v1/4.2", "{unknown}", "{}", "a{b"]


def test_the_spec_refuses_an_empty_command():
    import pytest
    from pydantic import ValidationError

    from looplab.adapters.repo_task import ArtifactSyncSpec
    with pytest.raises(ValidationError):
        ArtifactSyncSpec(command=[])
    with pytest.raises(ValidationError):
        ArtifactSyncSpec(command=["mc"], timeout=0)


_NEEDS_KEY = ("import os, sys; k = os.environ.get('AWS_SECRET_ACCESS_KEY', ''); "
              "sys.stderr.write('using ' + k + '\\n'); "
              "sys.exit(3 if len(k) != 24 else 4 if os.path.basename(os.getcwd()) != 'run' else 0)")


def test_a_credential_named_in_env_passthrough_reaches_the_copy_and_it_runs_from_the_run_dir(
        tmp_path, monkeypatch):
    """critic 2026-10-08: every launch strips secret-shaped variables, so `aws`/`mc` never saw the
    pod's injected keys. Named in `env_passthrough`, the value reaches this one command — from the
    RUN directory, never the candidate's workdir — and never the receipt."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIK7MDENGbPxR")
    engine = make_engine(tmp_path / "run")
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})
    (tmp_path / "run" / "nodes" / "node_0").mkdir(parents=True)
    for passthrough, code in (([], 3), (["AWS_SECRET_ACCESS_KEY"], 0)):
        engine._eval_spec = {"artifact_sync": {"command": [sys.executable, "-c", _NEEDS_KEY],
                                               "env_passthrough": passthrough}}
        artifact_sync.start_artifact_sync(engine, 0, 0)
        assert artifact_sync.wait_for_inflight(30)
        rows = [e.data for e in engine.store.read_all() if e.type == "artifact_synced"]
        assert rows[-1]["exit_code"] == code, rows[-1]
    assert "wJalrXUtnFEMIK7MDENGbPxR" not in (tmp_path / "run" / "events.jsonl").read_text(), (
        "the value printed by the tool is masked on the receipt")


def test_the_spec_refuses_a_passthrough_that_is_not_a_name():
    import pytest
    from looplab.adapters.repo_task import ArtifactSyncSpec
    assert ArtifactSyncSpec(command=["x"], env_passthrough=["A", "A", "B_2"]).env_passthrough == ["A", "B_2"]
    with pytest.raises(ValueError):
        ArtifactSyncSpec(command=["x"], env_passthrough=["NOT A NAME"])


def test_without_credentials_the_copy_runs_from_the_workdir(tmp_path):
    """critic 2026-10-08, second round (driven): always-run-dir broke every workdir-relative command
    (`rsync -a ./ dest` copied the whole run dir, events.jsonl included)."""
    from looplab.engine.artifact_sync import sync_cwd
    assert sync_cwd("/r/nodes/node_1", "/r", {"command": ["x"]}) == "/r/nodes/node_1"
    assert sync_cwd("/r/nodes/node_1", "/r", {"command": ["x"], "env_passthrough": []}) == (
        "/r/nodes/node_1")
    assert sync_cwd("/r/nodes/node_1", "/r", {"env_passthrough": ["AWS_SECRET_ACCESS_KEY"]}) == "/r"


def test_the_cwd_follows_the_declaration_not_the_host_environment(tmp_path, monkeypatch):
    """Round 3 (driven): the cwd was keyed on which declared names THIS host's environment held, so
    one task ran from the run dir on a box with the key and from the workdir on one without — and a
    relative argv broke on one of them. A declared passthrough runs from the run dir everywhere."""
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    engine = make_engine(tmp_path / "run")
    (tmp_path / "run" / "nodes" / "node_0").mkdir(parents=True)
    where = "import os, sys; sys.exit(0 if os.path.basename(os.getcwd()) == 'run' else 5)"
    engine._eval_spec = {"artifact_sync": {"command": [sys.executable, "-c", where],
                                           "env_passthrough": ["AWS_SECRET_ACCESS_KEY"]}}
    artifact_sync.start_artifact_sync(engine, 0, 0)
    assert artifact_sync.wait_for_inflight(30)
    rows = [e.data for e in engine.store.read_all() if e.type == "artifact_synced"]
    assert rows[-1]["exit_code"] == 0, "the key is absent here, and the copy still runs from the run dir"


_ECHO = ("import os, sys; v = os.environ.get('MC_HOST_minio', ''); print('host ' + v); "
         "sys.stderr.write('error: ' + v + '\\n'); sys.exit(1)")


def test_a_tool_that_echoes_a_passthrough_secret_leaves_it_in_no_file(tmp_path, monkeypatch):
    """Round 3 (driven): `run_argv(log_path=…)` mirrored the tool's raw bytes into
    `artifact_sync.log`, which later evals may read, and a name that is not secret-SHAPED
    (`MC_HOST_minio`) escaped the receipt's env screen too. Masked by identity at the write."""
    secret = "https://minioadmin:Zq8vR2kLp0sWx7Tn@minio.local:9000"
    monkeypatch.setenv("MC_HOST_minio", secret)
    engine = make_engine(tmp_path / "run")
    (tmp_path / "run" / "nodes" / "node_0").mkdir(parents=True)
    engine._eval_spec = {"artifact_sync": {"command": [sys.executable, "-c", _ECHO],
                                           "env_passthrough": ["MC_HOST_minio"]}}
    artifact_sync.start_artifact_sync(engine, 0, 0)
    assert artifact_sync.wait_for_inflight(30)
    log = (tmp_path / "run" / "artifact_sync.log").read_text()
    assert "host ***REDACTED_ENV***" in log and "error: ***REDACTED_ENV***" in log, log
    assert "Zq8vR2kLp0sWx7Tn" not in log
    assert "Zq8vR2kLp0sWx7Tn" not in (tmp_path / "run" / "events.jsonl").read_text()


# ------------------------------------------------------------------ review 2026-10-08

_MARK = ("import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran'); "
         "sys.exit(0 if pathlib.Path(sys.argv[2]).is_dir() else 4)")


def _synced(engine):
    assert artifact_sync.wait_for_inflight(30)
    return [e.data for e in engine.store.read_all() if e.type == "artifact_synced"]


def test_a_relative_run_dir_renders_absolute_placeholders(tmp_path, monkeypatch):
    """Reproduced: under `looplab run --out runs/demo` the argv named `{workdir}` relative to a
    command run FROM that workdir, so it resolved twice — `runs/demo/nodes/node_0/runs/demo/…`."""
    import os
    from pathlib import Path
    monkeypatch.chdir(tmp_path)
    engine = make_engine(Path("run"))
    assert not Path(engine.run_dir).is_absolute()
    (tmp_path / "run" / "nodes" / "node_0").mkdir(parents=True)
    engine._eval_spec = {"artifact_sync": {"command": [
        sys.executable, "-c", _MARK, str(tmp_path / "ran.txt"), "{workdir}"]}}
    artifact_sync.start_artifact_sync(engine, 0, 0)
    rows = _synced(engine)
    assert rows[-1]["exit_code"] == 0, rows[-1]
    assert os.path.isabs(rows[-1]["command"][-1])


def test_a_workdir_linking_outside_itself_is_never_copied(tmp_path):
    """A candidate on a Docker tier cannot read `/root/.aws/credentials`, but it can leave
    `ckpt -> /root/.aws/credentials` in its workdir, and the HOST tool would upload what it names."""
    import os
    engine = make_engine(tmp_path / "run")
    wd = tmp_path / "run" / "nodes" / "node_0"
    (wd / "sub").mkdir(parents=True)
    secret = tmp_path / "home" / ".aws" / "credentials"
    secret.parent.mkdir(parents=True)
    secret.write_text("aws_secret_access_key=x")
    os.symlink(secret, wd / "sub" / "ckpt")
    os.symlink("sub", wd / "inside")                           # a link inside the workdir is fine
    engine._eval_spec = {"artifact_sync": {"command": [
        sys.executable, "-c", _MARK, str(tmp_path / "ran.txt"), "{workdir}"]}}
    artifact_sync.start_artifact_sync(engine, 0, 0)
    row = _synced(engine)[-1]
    assert not (tmp_path / "ran.txt").exists(), "the command never ran"
    assert row["skipped"] == "workdir_links_outside" and row["exit_code"] is None
    assert os.path.join("sub", "ckpt") in row["stderr_tail"] and "inside" not in row["stderr_tail"]


def test_a_declared_data_mount_is_the_operators_link_not_the_candidates(tmp_path):
    import os
    engine = make_engine(tmp_path / "run")
    data = tmp_path / "datasets" / "train"
    data.mkdir(parents=True)
    wd = tmp_path / "run" / "nodes" / "node_0"
    wd.mkdir(parents=True)
    os.symlink(data, wd / "data")
    engine._repo_spec = {"data": {"data": {"path": str(data), "mount": True}}}
    engine._eval_spec = {"artifact_sync": {"command": [
        sys.executable, "-c", _MARK, str(tmp_path / "ran.txt"), "{workdir}"]}}
    artifact_sync.start_artifact_sync(engine, 0, 0)
    assert _synced(engine)[-1]["exit_code"] == 0
    # …but the same name repointed by the candidate is not the declared mount any more.
    os.unlink(wd / "data")
    os.symlink(tmp_path / "home", wd / "data")
    artifact_sync.start_artifact_sync(engine, 0, 1)
    assert _synced(engine)[-1].get("skipped") == "workdir_links_outside"


def test_a_start_row_a_write_fence_refuses_is_no_raise(tmp_path, monkeypatch):
    """The function promises never to raise; a Replay/deletion fence refuses with RuntimeError."""
    engine = make_engine(tmp_path / "run")
    (tmp_path / "run" / "nodes" / "node_0").mkdir(parents=True)
    engine._eval_spec = {"artifact_sync": {"command": [sys.executable, "-c", "print(1)"]}}

    class _WriteFence(RuntimeError):            # the shape of the store's write-fence refusals
        pass

    def _fenced(*_a, **_k):
        raise _WriteFence("a Replay owns this run")

    monkeypatch.setattr(engine.store, "append", _fenced)
    assert artifact_sync.start_artifact_sync(engine, 0, 0) is None


def test_an_argv_is_not_masked_as_if_a_capture_had_cut_it(tmp_path, monkeypatch):
    """`mask_tool_text`'s cut-edge rule is for a CAPTURED stream; an argv word that only looks like
    the start of a passthrough value was masked in the log line and the receipt."""
    secret = "bucketprod-Zq8vR2kLp0"
    monkeypatch.setenv("MC_HOST_minio", secret)
    engine = make_engine(tmp_path / "run")
    (tmp_path / "run" / "nodes" / "node_0").mkdir(parents=True)
    engine._eval_spec = {"artifact_sync": {"command": [sys.executable, "-c", "pass", "bucketprod"],
                                           "env_passthrough": ["MC_HOST_minio"]}}
    artifact_sync.start_artifact_sync(engine, 0, 0)
    row = _synced(engine)[-1]
    assert row["command"][-1] == "bucketprod", row["command"]
    assert (tmp_path / "run" / "artifact_sync.log").read_text().splitlines()[0].endswith(
        "bucketprod")
    # A captured stream's edge still is: the cut can leave a value's prefix at its tail.
    assert artifact_sync.mask_tool_text("tail bucketprod", {"K": secret}).endswith("***")


def test_a_second_ctrl_c_during_the_release_wait_still_retires_the_tracer(monkeypatch):
    import pytest
    from looplab.engine.orchestrator import Engine
    shut = []

    class _Tracer:
        def shutdown(self, timeout_millis):
            shut.append(timeout_millis)
            return True

    host = Engine.__new__(Engine)
    host.tracer = _Tracer()

    def _interrupted(_engine, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifact_sync, "drain_before_release", _interrupted)
    with pytest.raises(KeyboardInterrupt):
        Engine.retire_tracer(host)
    assert shut, "the terminal barrier is reached whatever the wait ends in"


def test_the_release_wait_says_what_an_interrupt_leaves(monkeypatch, caplog):
    import pytest
    host = object()
    monkeypatch.setitem(artifact_sync._PENDING_BY_OWNER, id(host), 2)

    def _interrupted(timeout=None, engine=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifact_sync, "wait_for_inflight", _interrupted)
    with pytest.raises(KeyboardInterrupt), caplog.at_level("WARNING"):
        artifact_sync.drain_before_release(host, timeout=5)
    assert "interrupted" in caplog.text and "stays open" in caplog.text
