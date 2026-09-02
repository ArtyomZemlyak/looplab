"""`.complete` had no reader, so it documented rather than guarded.

9f72d32c added the marker -- written last, and only past snapshot.sh's shortfall check -- because
nothing distinguished a snapshot being WRITTEN from one that was done, and `git bundle create`
writes straight to its final path, so a bundle caught mid-write is TRUNCATED while the directory
looks like every other. Then nothing consulted it. A marker nobody reads is documentation.

Every sweep also asserts "the bundle is verified by restoring it", and that verification was a
person typing `git clone`. This is that person, as a script, refusing the snapshots the marker says
are unfinished.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "benchmarks" / "restore_from_snapshot.sh"


def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin", "HOME": str(repo)})


def _snapshot(root: Path, name: str, *, complete: bool, subject: str = "the tree",
              truncate: bool = False) -> Path:
    """One snapshot directory holding a real looplab bundle."""
    repo = root / f".src-{name}"
    repo.mkdir(parents=True)
    (repo / "kept.txt").write_text(subject + "\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", subject)
    head = subprocess.run(["git", "-C", str(repo), "log", "--oneline", "-1"],
                          capture_output=True, text=True, check=True).stdout.strip()

    out = root / name
    out.mkdir(parents=True)
    # BOTH bundles, because the script checks both and a snapshot marked complete carries both --
    # snapshot.sh writes them and exits 1 if either source is missing. The first version of this
    # fixture wrote only looplab's, and the script correctly reported the other as absent: the
    # fixture was wrong, not the tool, and shipping the fixture's version would have taught the
    # script to accept half a snapshot.
    for who in ("looplab", "AlgoTune"):
        subprocess.run(["git", "-C", str(repo), "bundle", "create", str(out / f"{who}.bundle"),
                        "--all"], check=True, capture_output=True)
        (out / f"{who}-HEAD.txt").write_text(head + "\n")
    if truncate:
        b = out / "looplab.bundle"
        b.write_bytes(b.read_bytes()[: max(1, b.stat().st_size // 3)])
    if complete:
        (out / ".complete").write_text("2026-09-02T00:00:00Z\n")
    return out


def _run(dest: Path, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(TOOL), str(dest), str(root)],
                          capture_output=True, text=True, timeout=600)


def test_it_skips_an_unfinished_snapshot_and_says_which(tmp_path):
    """The newest is not the criterion. An unmarked directory is skipped BY NAME, because "there
    were none" and "there were three and all unfinished" need different actions."""
    root = tmp_path / "snapshots"
    _snapshot(root, "20260901-100000", complete=True, subject="the good tree")
    _snapshot(root, "20260901-110000", complete=False, subject="the half-written tree")
    r = _run(tmp_path / "dest", root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped as unfinished (no .complete): 20260901-110000" in r.stdout, r.stdout
    assert "restoring from 20260901-100000" in r.stdout, r.stdout
    assert (tmp_path / "dest" / "looplab" / "kept.txt").read_text() == "the good tree\n"


def test_no_complete_snapshot_is_a_refusal_and_not_a_silent_pick(tmp_path):
    root = tmp_path / "snapshots"
    _snapshot(root, "20260901-100000", complete=False)
    r = _run(tmp_path / "dest", root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NO COMPLETE SNAPSHOT" in r.stderr, r.stderr
    assert not (tmp_path / "dest" / "looplab").exists()


def test_a_truncated_bundle_is_reported_not_half_restored(tmp_path):
    """The failure the marker exists for, arriving anyway: a bundle that is marked complete and is
    not clonable must fail loudly rather than leave a partial checkout behind."""
    root = tmp_path / "snapshots"
    _snapshot(root, "20260901-100000", complete=True, truncate=True)
    r = _run(tmp_path / "dest", root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "WILL NOT CLONE" in r.stdout, r.stdout


def test_it_checks_the_restored_head_against_what_the_snapshot_recorded(tmp_path):
    """A clone that succeeds onto the WRONG branch is the documented failure; the bundle carries
    several refs. The comparison is at the RECORDED length -- the first version cut the clone's sha
    to twelve against a `git log --oneline` abbreviation of seven, so every restore said WRONG TREE
    about a tree that was right."""
    root = tmp_path / "snapshots"
    snap = _snapshot(root, "20260901-100000", complete=True)
    (snap / "looplab-HEAD.txt").write_text("deadbee some other commit\n")
    r = _run(tmp_path / "dest", root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "WRONG TREE" in r.stdout and "deadbee" in r.stdout, r.stdout


def test_it_refuses_to_write_over_an_existing_checkout(tmp_path):
    """Restoring is not the operator's decision to swap; a silent overwrite is how a half-restore
    replaces a working tree."""
    root = tmp_path / "snapshots"
    _snapshot(root, "20260901-100000", complete=True)
    dest = tmp_path / "dest"
    (dest / "looplab").mkdir(parents=True)
    r = _run(dest, root)
    assert r.returncode == 1, r.stdout
    assert "refusing to write over it" in r.stdout, r.stdout
