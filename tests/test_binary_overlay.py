"""Binary files in an external agent's worktree/patch overlay (doc 79 §9 leftover, 2026-10-14):
stored by content in `RUN_DIR/code/blobs`, carried as a NUL-prefixed marker in `Node.files`, and
materialized byte-exact — verified by hash — into the node's workdir."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from looplab.engine.code_repo import CodeRepoError
from looplab.engine.code_worktree import (BLOB_MARKER, add_worktree, is_blob_marker,
                                          overlay_from_worktree, resolve_blob, store_blob)
from looplab.engine.workspace import WorkspaceSeeder

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _git(cwd, *argv):
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def test_a_worktree_binary_becomes_a_blob_marker_and_materializes_byte_exact(tmp_path):
    from tests.test_upstream_lane import fixture
    from looplab.engine.code_identity import sync_run
    lane, _store, _generation, _ = fixture(tmp_path)
    rd = lane.rd
    sync_run(rd)
    wt = Path(add_worktree(rd, "agent-bin")["path"])
    (wt / "README.md").write_bytes(PNG)                  # on the edit surface, not UTF-8
    (wt / "recipe.env").write_text("MOMENTUM=0.9\n")
    overlay = overlay_from_worktree(rd, "agent-bin")
    assert overlay["files"]["recipe.env"] == "MOMENTUM=0.9\n"
    marker = overlay["files"]["README.md"]
    assert is_blob_marker(marker) and overlay["binary_files"] == ["README.md"]
    assert resolve_blob(rd, marker) == PNG
    # Materialization writes the bytes, not the marker.
    engine = SimpleNamespace(run_dir=rd, _assets=[], _repo_spec={"protected_names": []})
    work = tmp_path / "work"
    WorkspaceSeeder(engine).write_node_files(SimpleNamespace(files=overlay["files"], deleted=[]),
                                             work)
    assert (work / "README.md").read_bytes() == PNG
    assert (work / "recipe.env").read_text() == "MOMENTUM=0.9\n"


def test_a_missing_or_altered_blob_refuses_the_materialization(tmp_path):
    marker = store_blob(tmp_path, b"\x00\x01binary")
    assert resolve_blob(tmp_path, marker) == b"\x00\x01binary"
    assert store_blob(tmp_path, b"\x00\x01binary") == marker           # content-addressed
    blob = next((tmp_path / "code" / "blobs").iterdir())
    blob.chmod(0o644)
    blob.write_bytes(b"tampered")
    engine = SimpleNamespace(run_dir=tmp_path, _assets=[], _repo_spec={"protected_names": []})
    with pytest.raises(CodeRepoError, match="does not match"):
        WorkspaceSeeder(engine).write_node_files(
            SimpleNamespace(files={"w.bin": marker}, deleted=[]), tmp_path / "w")
    blob.unlink()
    with pytest.raises(CodeRepoError, match="missing"):
        resolve_blob(tmp_path, marker)
    with pytest.raises(CodeRepoError):
        resolve_blob(tmp_path, BLOB_MARKER + "sha256=zz;size=1")


def test_the_mirror_copies_blobs_but_not_the_rebuildable_repository(tmp_path):
    from looplab.events.run_mirror import MirrorConfig, mirror_pass
    rd = tmp_path / "src" / "r"
    rd.mkdir(parents=True)
    (rd / "events.jsonl").write_text('{"seq":0}\n')
    store_blob(rd, b"\x00blob")
    (rd / "code" / "repo.git" / "objects").mkdir(parents=True)
    (rd / "code" / "repo.git" / "objects" / "x").write_text("x")
    mirror_pass(MirrorConfig(src_root=tmp_path / "src", dst_root=tmp_path / "dst",
                             max_bytes_per_s=0, max_files_per_s=0))
    out = tmp_path / "dst" / "r" / "code"
    assert len(list((out / "blobs").iterdir())) == 1 and not (out / "repo.git").exists()


def test_upstream_readiness_names_each_missing_obligation():
    """doc 79 §9 leftover: an external run without an `upstream` block learns exactly what to
    declare, and that `code-export` delivers its fixes meanwhile — no silent default gate."""
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.core.config import Settings
    from looplab.engine.code_identity import upstream_readiness
    bare = RepoTask(id="t", direction="max", editable_path="/tmp",
                    eval=EvalSpec(command=["python", "s.py"], metric={"kind": "stdout_json",
                                                                       "key": "m"}))
    out = upstream_readiness(bare, Settings(backend="toy", upstream_mode="off"))
    assert out["ready"] is False
    assert [m["what"] for m in out["missing"]] == [
        "seed_base", "eval.scorer_boundary", "upstream", "Settings.upstream_mode"]
    assert "code-export" in out["meanwhile"]
    assert upstream_readiness(None, None)["missing"][0]["what"] == "repo_task"
