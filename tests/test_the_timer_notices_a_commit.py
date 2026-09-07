"""The snapshot timer watched what was MEASURED and never what was WRITTEN.

Measured 2026-09-02 01:03 on the live box: HEAD was 8c288f59 and the newest snapshot recorded
88560d41 -- four commits of code with no snapshot taken. Every path the fingerprint watches answers
"has anything been measured since last time", and committing measures nothing, so the timer sat
quiet through all four.

That is defensible for the TRIGGER and not for the ARTEFACT. The snapshot it fires copies
`looplab.bundle`, and the bundle exists to survive a /var/tmp wipe; the 2026-08-29 wipe cost 37
UNPUSHED commits, which is precisely the work a bundle is the only backup for. A box where
measurement stops and coding continues lets the bundle fall arbitrarily far behind by construction.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TIMER = REPO / "benchmarks" / "snapshot_timer.sh"


def _git(repo: Path, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _fingerprint(root: Path) -> str:
    """Run the timer's own `fingerprint` out of the script, nothing else from it."""
    src = subprocess.run(["sed", "-n", "/^fingerprint() {/,/^}/p", str(TIMER)],
                         capture_output=True, text=True, check=True).stdout
    assert "md5sum" in src, "could not extract fingerprint() from snapshot_timer.sh"
    prog = (f'ROOT="{root}"\n'
            f'. "{REPO}/benchmarks/bench_trees.sh"\n' + src + "\nfingerprint\n")
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def _bench(tmp_path: Path) -> Path:
    root = tmp_path / "bench"
    repo = root / "looplab"
    (repo / "benchmarks" / "algotune" / ".baseline_times").mkdir(parents=True)
    (root / "meter").mkdir(parents=True)
    (root / "AlgoTune" / "reports").mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first")
    return root


def test_a_commit_moves_the_fingerprint(tmp_path):
    """The defect itself: four commits produced no snapshot because none of them moved this."""
    root = _bench(tmp_path)
    before = _fingerprint(root)
    (root / "looplab" / "a.py").write_text("x = 2\n")
    _git(root / "looplab", "add", "-A")
    _git(root / "looplab", "commit", "-qm", "second")
    assert _fingerprint(root) != before, (
        "committing does not move the fingerprint, so the bundle can fall arbitrarily far behind")


def test_an_UNCOMMITTED_edit_moves_it_too(tmp_path):
    """`looplab-uncommitted.patch` is in every snapshot for a reason: uncommitted work is what a
    wipe takes first. A trigger blind to it archives a patch of nothing."""
    root = _bench(tmp_path)
    before = _fingerprint(root)
    (root / "looplab" / "a.py").write_text("x = 3   # not committed\n")
    assert _fingerprint(root) != before, "an uncommitted edit leaves the timer quiet"


def test_a_pyc_under_the_repo_does_NOT_move_it(tmp_path):
    """And the reason this is two git calls rather than a `find` over the tree: pytest leaves
    `.pyc` and `.pytest_cache` under the repo, and a fingerprint that fired on those would snapshot
    on every test run and archive nothing new."""
    root = _bench(tmp_path)
    before = _fingerprint(root)
    cache = root / "looplab" / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-312.pyc").write_bytes(b"\x00\x01")
    (root / "looplab" / ".gitignore").write_text("__pycache__/\n")
    _git(root / "looplab", "add", "-A")
    _git(root / "looplab", "commit", "-qm", "ignore caches")
    settled = _fingerprint(root)
    (cache / "b.cpython-312.pyc").write_bytes(b"\x00\x02")
    assert _fingerprint(root) == settled, "a build artefact moves the fingerprint"


def test_nothing_changing_leaves_it_alone(tmp_path):
    """The whole value of the trigger is that it is quiet when there is nothing to save."""
    root = _bench(tmp_path)
    assert _fingerprint(root) == _fingerprint(root)
