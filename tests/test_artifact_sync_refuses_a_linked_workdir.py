"""The copy-out never follows a LINKED node workdir (review 2026-10-08).

`start_artifact_sync` asked `workdir.is_dir()`, which follows a symlink: with `nodes/node_N` linked
elsewhere, the operator's copy command — run with the host's credentials — would upload whatever the
link names. It now takes `core/node_evidence.py::node_workdir`, the refusal the log readers and
`_parent_workdirs_env` already apply.
"""
from __future__ import annotations

import sys

from factories import make_engine
from looplab.engine import artifact_sync

_COPY = "import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2])"


def _engine(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._eval_spec = {"artifact_sync": {"command": [
        sys.executable, "-c", _COPY, "{workdir}", str(tmp_path / "dest")], "timeout": 60.0}}
    return engine


def test_a_linked_node_workdir_is_not_copied(tmp_path):
    secret = tmp_path / "elsewhere"
    secret.mkdir()
    (secret / "credentials").write_text("host secret")
    engine = _engine(tmp_path)
    nodes = engine.run_dir / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / "node_0").symlink_to(secret, target_is_directory=True)
    assert artifact_sync.start_artifact_sync(engine, 0, 0) is None
    assert artifact_sync.wait_for_inflight(30)
    assert not (tmp_path / "dest").exists(), "the copy-out followed a linked node workdir"
    assert not any(e.type == "artifact_synced" for e in engine.store.read_all())


def test_a_linked_nodes_directory_is_not_copied_either(tmp_path):
    other = tmp_path / "other_run_nodes"
    (other / "node_0").mkdir(parents=True)
    engine = _engine(tmp_path)
    engine.run_dir.mkdir(parents=True, exist_ok=True)
    nodes = engine.run_dir / "nodes"
    if nodes.exists():
        nodes.rmdir()
    nodes.symlink_to(other, target_is_directory=True)
    assert artifact_sync.start_artifact_sync(engine, 0, 0) is None


def test_a_real_workdir_is_still_copied(tmp_path):
    engine = _engine(tmp_path)
    workdir = engine.run_dir / "nodes" / "node_0"
    workdir.mkdir(parents=True)
    (workdir / "ckpt.bin").write_bytes(b"weights")
    assert artifact_sync.start_artifact_sync(engine, 0, 0) is not None
    assert artifact_sync.wait_for_inflight(30)
    assert (tmp_path / "dest" / "ckpt.bin").read_bytes() == b"weights"


def test_no_workdir_runs_nothing(tmp_path):
    assert artifact_sync.start_artifact_sync(_engine(tmp_path), 0, 0) is None
