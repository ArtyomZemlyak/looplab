"""The agent-worktree backup and its restore under the review of 2026-10-16: relative run roots,
restore points that are explicit and TOTAL, a stuck restore that never stops the live backup, the
location rule on a restored path, plain-file versions that see a same-size rewrite, names that are
not UTF-8, a mirror that lost its state while the producer fails, and the per-worktree capture that
is reused while nothing moved. Real git, real mirror passes."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import anyio
import pytest

import looplab.engine.code_worktree as cw
from looplab.engine.code_identity import sync_run
from looplab.engine.code_repo import CodeRepo
from looplab.engine.orchestrator import Engine
from looplab.events.run_mirror import MirrorConfig, mirror_pass, restore_run
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from tests._posix_gates import BYTES_FILENAMES
from tests.test_code_identity import _git, _repo_task


def _run(tmp_path) -> tuple:
    """`(operator repo, run dir)`: one evaluated node under `tmp_path/src/run`, the task snapshot
    beside the log as `looplab run` writes it (the location rule reads it)."""
    repo, task = _repo_task(tmp_path)
    r, d = task.build_roles()
    rd = tmp_path / "src" / "run"
    anyio.run(Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                     policy=GreedyTree(n_seeds=1, max_nodes=1)).run)
    (rd / "task.snapshot.json").write_text(task.model_dump_json(), encoding="utf-8")
    return repo, rd


def _cfg(tmp_path, **kw) -> MirrorConfig:
    return MirrorConfig(src_root=tmp_path / "src", dst_root=tmp_path / "dst", max_bytes_per_s=0,
                        max_files_per_s=0, run_extras=cw.agent_backup, **kw)


def _wipe_and_restore(tmp_path, cfg, run="run") -> Path:
    shutil.rmtree(tmp_path / "src")                          # the pod restart wipes tmpfs
    os.makedirs(tmp_path / "src")
    out = restore_run(cfg.dst_root, run, cfg.src_root)
    assert not out["failures"], out
    assert out["agent_worktrees"] == f"looplab code-status --sync {tmp_path / 'src' / run}"
    return tmp_path / "src" / run


def _draft(rd: Path, dirty: bool = True) -> Path:
    wt = Path(cw.add_worktree(rd, "draft")["path"])
    (wt / "train.py").write_text("print('LR: 0.03')\n")
    _git(wt, "commit", "-qam", "proposal")
    if dirty:
        (wt / "notes.txt").write_text("try warmup\n")         # uncommitted
    return wt


def test_a_relative_run_root_backs_up_on_the_python_3_11_temp_rule(tmp_path, monkeypatch):
    """`looplab run-mirror` defaults to `--src runs`. On Python 3.11 `TemporaryDirectory(dir=<a
    relative dir>)` is relative too, and `git -C <worktree>` resolved `GIT_INDEX_FILE` /
    `GIT_OBJECT_DIRECTORY` / the pathspec file against the WORKTREE: every pass failed."""
    _, rd = _run(tmp_path)
    _draft(rd)
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp_311(suffix=None, prefix=None, dir=None):    # 3.12 made the result absolute
        made = real_mkdtemp(suffix, prefix, dir)
        return os.path.relpath(made) if dir is not None and not os.path.isabs(dir) else made
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_311)
    monkeypatch.chdir(tmp_path)
    cfg = MirrorConfig(src_root=Path("src"), dst_root=Path("dst"), max_bytes_per_s=0,
                       max_files_per_s=0, run_extras=cw.agent_backup)
    report = mirror_pass(cfg)
    assert not report.errors, report.as_dict()
    index = json.loads((tmp_path / "dst/run/code/agent_backup/agents.json").read_text())
    assert index["worktrees"][0]["dirty_commit"], index


def test_only_an_explicit_restore_point_restores_and_a_stuck_restore_never_stops_the_backup(
        tmp_path):
    """A3: `sync_run` runs under every code path — a submits-nothing overlay preview included — so
    it never restores agent worktrees; `code-status --sync` does. B1/2: a restore held back by a
    conflict keeps its backup AND the live worktrees made since are backed up beside it, so a
    second wipe loses neither."""
    from typer.testing import CliRunner
    from looplab.cli import app
    _, rd = _run(tmp_path)
    _draft(rd)
    cfg = _cfg(tmp_path)
    assert not mirror_pass(cfg).errors
    rd = _wipe_and_restore(tmp_path, cfg)
    stray = rd / "code" / "worktrees" / "draft"
    sync_run(rd)
    cw.overlay_from_patch(rd, "--- a/recipe.env\n+++ b/recipe.env\n@@ -1 +1 @@\n-SEED=1\n"
                              "+SEED=2\n")                     # a submits-nothing preview syncs
    assert not stray.exists() and (rd / cw.AGENT_BACKUP_REL / "agents.json").is_file()
    stray.mkdir(parents=True)                                 # something sits at its path
    (stray / "mine.txt").write_text("keep\n")
    cli = CliRunner().invoke(app, ["code-status", str(rd), "--sync"])
    assert cli.exit_code == 0, cli.output
    assert '"worktree_path_exists"' in cli.output and (stray / "mine.txt").is_file()
    # The live worktree made after the restore is backed up while the old backup is pending.
    fresh = Path(cw.add_worktree(rd, "fresh")["path"])
    (fresh / "recipe.env").write_text("SEED=5\n")
    files = cw.agent_backup(rd, tmp_path / "spool")
    pending = [rel for rel in files if "/pending/" in rel]
    assert sorted(Path(rel).name for rel in pending) == ["agents.bundle", "agents.json"], files
    assert not mirror_pass(cfg).errors
    shutil.rmtree(stray)
    rd = _wipe_and_restore(tmp_path, cfg)
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert sorted(report["restored"]) == ["draft", "fresh"] and not report["pending"], report
    assert not (rd / cw.AGENT_BACKUP_REL).exists()
    assert (rd / "code/worktrees/draft/notes.txt").read_text() == "try warmup\n"
    assert (rd / "code/worktrees/fresh/recipe.env").read_text() == "SEED=5\n"


def _restored_with(tmp_path, edit, dirty: bool = True) -> Path:
    """A mirrored-and-restored run whose restored `agents.json` rows `edit` rewrote."""
    _, rd = _run(tmp_path)
    _draft(rd, dirty)
    cfg = _cfg(tmp_path)
    assert not mirror_pass(cfg).errors
    rd = _wipe_and_restore(tmp_path, cfg)
    index_path = rd / cw.AGENT_BACKUP_REL / "agents.json"
    index = json.loads(index_path.read_text())
    edit(index["worktrees"][0])
    index_path.write_text(json.dumps(index))
    return rd


def test_a_restore_is_total_rolls_back_a_half_made_worktree_and_retries(tmp_path, monkeypatch):
    rd = _restored_with(tmp_path, lambda row: None)
    calls = []

    def failing(root, worktree, row):
        calls.append(worktree)
        raise OSError("disk full")
    monkeypatch.setattr(cw, "_restore_plain", failing)
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["pending"] and report["skipped"][0]["reason"] == "restore_failed", report
    assert calls and not calls[0].exists()                    # rolled back: no worktree_path_exists
    assert not (rd / "code/worktrees/.meta/draft.json").exists()
    monkeypatch.undo()
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["restored"] == ["draft"] and not report["pending"], report


def test_a_malformed_row_or_a_missing_bundle_is_reported_not_raised(tmp_path):
    rd = _restored_with(tmp_path, lambda row: row.update(head=5, files="x"))
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["skipped"] == [{"name": "draft", "reason": "row_malformed"}], report
    rd2 = restore_run(tmp_path / "dst", "run", tmp_path / "src", as_name="run2")["to"]
    (Path(rd2) / cw.AGENT_BACKUP_REL / "agents.bundle").unlink()  # skipped as over --max-file-mb
    report = sync_run(rd2, restore_agents=True)["agent_worktrees"]
    assert report["pending"] and report["skipped"][0]["reason"] == "bundle_missing", report
    assert "--max-file-mb" in report["skipped"][0]["detail"]


def test_a_detached_row_without_a_dirty_commit_restores(tmp_path):
    """B2: `dirty_commit` None and no dirty ref in the bundle compared EQUAL, so a row whose head
    differed from its branch sent `checkout --detach` to a commit that is not there and aborted
    every later row."""
    rd = _restored_with(tmp_path, lambda row: row.update(head="1" * 40), dirty=False)
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["restored"] == ["draft"], report


def test_a_restored_outside_path_obeys_the_location_rule(tmp_path):
    """A4: a tampered (or foreign) backup must not make the restore write into the operator's
    source tree: the path is held to `_worktree_location`, resolved."""
    repo = tmp_path / "repo"

    def into_source(row):
        row.update(outside_run=True, path=str(repo.resolve() / "agentwt"))
    rd = _restored_with(tmp_path, into_source)
    status = _git(repo, "status", "--porcelain")
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["skipped"][0]["reason"] == "outside_path_refused", report
    assert report["skipped"][0]["code"] == "code_worktree_path_refused" and report["pending"]
    assert not (repo / "agentwt").exists() and _git(repo, "status", "--porcelain") == status
    link = tmp_path / "persist-link"
    link.symlink_to(tmp_path)                                 # resolves elsewhere than recorded
    index_path = rd / cw.AGENT_BACKUP_REL / "agents.json"
    index = json.loads(index_path.read_text())
    index["worktrees"][0]["path"] = str(link / "wt")
    index_path.write_text(json.dumps(index))
    report = sync_run(rd, restore_agents=True)["agent_worktrees"]
    assert report["skipped"][0]["code"] == "code_worktree_path_not_canonical", report


def test_a_same_size_rewrite_that_kept_its_mtime_is_a_new_plain_version(tmp_path):
    """`cp -p`, `rsync -t`, a fixed-shape save inside one coarse tick: (size, mtime) did not move,
    so the stale copy was kept and restored."""
    ckpt = tmp_path / "model.pt"
    ckpt.write_bytes(b"\x01" * 64)
    before = ckpt.stat()
    ckpt.write_bytes(b"\x02" * 64)
    os.utime(ckpt, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = ckpt.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert cw._plain_version(after) != cw._plain_version(before)


def test_a_plain_file_restores_with_its_stored_copys_mtime_and_leaves_no_temp(tmp_path,
                                                                             monkeypatch):
    """B3: the mirror reads the LIVE file after the row was written, so the stored bytes carry the
    stored copy's mtime, not the row's; and a failed lay-back leaves no `.looplab-restore`."""
    root, wt = tmp_path / "backup", tmp_path / "wt"
    (root / "files/draft").mkdir(parents=True)
    wt.mkdir()
    stored = root / "files/draft/ckpt.pt.vabc"
    stored.write_bytes(b"new bytes")
    os.utime(stored, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    row = {"files": [{"path": "ckpt.pt", "stored": "files/draft/ckpt.pt.vabc", "size": 9,
                      "mtime_ns": 1_000_000_000_000_000_000}]}
    assert cw._restore_plain(root, wt, row) == []
    assert (wt / "ckpt.pt").stat().st_mtime_ns == 2_000_000_000_000_000_000
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("EIO")))
    with pytest.raises(OSError):
        cw._restore_plain(root, wt, row)
    monkeypatch.setattr(os, "replace", real_replace)
    assert sorted(p.name for p in wt.iterdir()) == ["ckpt.pt"]


@BYTES_FILENAMES
def test_a_name_that_is_not_utf8_is_skipped_by_name_not_backed_up_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "LARGE_FILE_BYTES", 16)
    _, rd = _run(tmp_path)
    wt = Path(cw.add_worktree(rd, "draft")["path"])
    with open(os.path.join(os.fsencode(wt), b"ckpt-\xe9.pt"), "wb") as fh:  # latin-1, ignored
        fh.write(b"\x01" * 64)
    (wt / "big.txt").write_text("x" * 64)                      # a plain file that IS carried
    files = cw.agent_backup(rd, tmp_path / "spool")
    row = json.loads((tmp_path / "spool" / "agents.json").read_text())["worktrees"][0]
    assert row["files_skipped"] == [{"path": "ckpt-\\xe9.pt", "reason": "name_not_utf8"}], row
    assert [f["path"] for f in row["files"]] == ["big.txt"]
    assert all(rel.encode("utf-8") for rel in files)
    cfg = _cfg(tmp_path)
    assert not mirror_pass(cfg).errors


def test_an_unchanged_worktree_is_not_re_captured_and_the_ignored_walk_is_bounded(
        tmp_path, monkeypatch):
    """Every pass used to re-hash every dirty file into a fresh store and walk every ignored
    directory, for every run including finished ones."""
    monkeypatch.setattr(cw, "MAX_WALK_ENTRIES", 2)
    _, rd = _run(tmp_path)
    wt = _draft(rd)
    (wt / "outputs").mkdir()
    for i in range(5):
        (wt / "outputs" / f"f{i}.txt").write_text(str(i))      # ignored (`outputs/`)
    adds = []
    real = cw.run_git

    def counting(*argv, **kw):
        if "add" in argv:
            adds.append(argv)
        return real(*argv, **kw)
    monkeypatch.setattr(cw, "run_git", counting)
    spool = tmp_path / "spool"
    cw.agent_backup(rd, spool)
    row = json.loads((spool / "agents.json").read_text())["worktrees"][0]
    assert {"path": "outputs/", "reason": "walk_bound"} in row["files_skipped"], row
    assert len(row["files"]) == 2 and len(adds) == 1
    bundle_before = (spool / "agents.bundle").stat().st_mtime_ns
    assert cw.agent_backup(rd, spool) and len(adds) == 1        # nothing moved: reused
    assert json.loads((spool / "agents.json").read_text())["worktrees"][0] == row
    assert (spool / "agents.bundle").stat().st_mtime_ns == bundle_before
    (wt / "notes.txt").write_text("try cosine\n")
    cw.agent_backup(rd, spool)
    assert len(adds) == 2
    after = json.loads((spool / "agents.json").read_text())["worktrees"][0]
    assert after["dirty_commit"] != row["dirty_commit"]
    # A second worktree's change re-creates the bundle: the reused dirty commit is captured
    # again (it is not in the new scratch store) and still restores.
    other = Path(cw.add_worktree(rd, "other")["path"])
    (other / "recipe.env").write_text("SEED=7\n")
    cw.agent_backup(rd, spool)
    listed = CodeRepo(rd).git("bundle", "list-heads", str(spool / "agents.bundle")).stdout
    assert after["dirty_commit"].encode() in listed
