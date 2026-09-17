"""§412. A snapshot that names a file and carries none of its bytes.

`snapshot.sh` archives the repo three ways: the commits as a bundle, the tracked edits as
`git diff HEAD`, and the status listing. A file that has never been added has no diff, so it
appears in exactly one of the three -- as a `??` line in `looplab-dirty.txt`, a name with no bytes
behind it anywhere in the archive.

Measured from the 2026-09-10 restart, which took `/var/tmp/looplab-bench` with it. The patch
restored §409, §410 and §411 in full, and the two test files those sections were written from came
back as two `??` lines; both instruments had shipped and both their drivers had to be written
again from the prose. A brand-new test is precisely the shape of file that is untracked at any
given minute, and it is the half of a change whose absence is SILENT -- the code still runs.

So the snapshot now carries them, and the restore unpacks them. The round trip is what these tests
drive: a file created and never added is present, and readable, in a tree restored from the
snapshot -- which is the property that was false, and the one the prose cannot check.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "benchmarks" / "snapshot.sh"
RESTORE = REPO / "benchmarks" / "restore_from_snapshot.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("tar") is None,
                                reason="the snapshot is a shell script over git and tar")

TRACKED = "benchmarks/kept.py"
EDITED = "# edited but not committed\n"
NEW_TEST = "tests/test_written_and_not_yet_added.py"
IGNORED = "runs/whatever.log"
BIG = "tests/data/enormous.bin"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture()
def bench(tmp_path):
    """A bench root shaped like the real one: `$BENCH_ROOT/looplab` is a git checkout."""
    src = tmp_path / "bench"
    repo = src / "looplab"
    (repo / "benchmarks").mkdir(parents=True)
    (repo / "tests" / "data").mkdir(parents=True)
    (repo / "runs").mkdir()

    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    (repo / TRACKED).write_text("print('committed')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first")

    # The three states a snapshot has to tell apart.
    (repo / TRACKED).write_text("print('committed')\n" + EDITED, encoding="utf-8")   # tracked edit
    (repo / NEW_TEST).write_text("def test_it():\n    assert True\n", encoding="utf-8")  # untracked
    (repo / IGNORED).write_text("noise\n", encoding="utf-8")                          # ignored

    # THE REST OF WHAT A SNAPSHOT IS OF, because `.complete` is written only past the shortfall
    # check and the restore refuses anything without it -- a fixture short of a measurement
    # directory produces a snapshot no restore will read, which is the script working correctly
    # and the test measuring nothing.
    at = src / "AlgoTune"
    (at / "reports").mkdir(parents=True)
    (at / "reports" / "generation.json").write_text("{}", encoding="utf-8")
    _git(at, "init", "-q")
    _git(at, "add", "-A")
    _git(at, "commit", "-qm", "ruler")
    (repo / "benchmarks" / "algotune").mkdir(parents=True)
    # The real tree ignores the ruler cache in `benchmarks/algotune/.gitignore`; the snapshot COPIES
    # it as a measurement, and it must not also ride in the untracked archive.
    (repo / "benchmarks" / "algotune" / ".gitignore").write_text(".baseline_times/\n",
                                                                encoding="utf-8")
    _git(repo, "add", "benchmarks/algotune/.gitignore")
    _git(repo, "commit", "-qm", "ignore the ruler cache")
    (repo / "benchmarks" / "algotune" / ".baseline_times").mkdir()
    (repo / "benchmarks" / "algotune" / ".baseline_times" / "t__test__w22x1r3.json").write_text(
        "{}", encoding="utf-8")
    (src / "meter").mkdir()
    (src / "meter" / "meter.jsonl").write_text("", encoding="utf-8")
    (src / "logs").mkdir()
    (src / "logs" / "probe.log").write_text("", encoding="utf-8")
    return src, repo


def _snapshot(src: Path, dest: Path, **env):
    # `SNAPSHOT_SKIP_STORE_CHECK` is the script's own documented override: it refuses a destination
    # that is non-empty and carries no `.persistent-store-id`, because writing a backup onto an
    # unmounted volume produces one that vanishes. A tmp_path is exactly that shape and is exactly
    # not that hazard.
    p = subprocess.run(["bash", str(SNAPSHOT), str(dest)], capture_output=True, text=True,
                       env={**os.environ, "BENCH_ROOT": str(src),
                            "SNAPSHOT_SKIP_STORE_CHECK": "1", **env}, timeout=300)
    made = sorted(d for d in dest.iterdir() if d.is_dir())
    assert made, p.stdout + p.stderr
    return made[-1], p


def test_an_untracked_file_is_in_the_snapshot(bench, tmp_path):
    """The defect, in one assertion: the bytes are somewhere in the archive."""
    src, _repo = bench
    out, p = _snapshot(src, tmp_path / "snaps")
    tarball = out / "looplab-untracked.tar.gz"
    assert tarball.is_file(), p.stdout + p.stderr
    with tarfile.open(tarball) as tf:
        names = tf.getnames()
    assert NEW_TEST in names, names
    assert "file(s) no patch can carry" in p.stdout, p.stdout


def test_what_the_repo_ignores_stays_out(bench, tmp_path):
    """`--exclude-standard`, so the exclusion list is the repo's own declaration and not a second
    one kept in the snapshot -- `runs/`, `.venv*/` and `node_modules/` are already named there,
    and an archive written every thirty minutes onto an S3 mount must not carry them."""
    src, _repo = bench
    out, _p = _snapshot(src, tmp_path / "snaps")
    with tarfile.open(out / "looplab-untracked.tar.gz") as tf:
        names = tf.getnames()
    assert IGNORED not in names, names


def test_a_file_too_large_is_NAMED_rather_than_silently_dropped(bench, tmp_path):
    """The second silent omission is how the first one was built.

    A hand-written test is kilobytes. Something enormous and untracked is not what this script
    should be putting into the archive every half hour -- but a reader of the snapshot has to be
    able to find out that it was left, which is exactly what `looplab-dirty.txt` could not tell
    them about the two lost tests.
    """
    src, repo = bench
    (repo / BIG).write_bytes(b"\0" * (300 * 1024))
    out, p = _snapshot(src, tmp_path / "snaps", UNTRACKED_MAX_KB="100")
    skipped = (out / "looplab-untracked-SKIPPED.txt").read_text(encoding="utf-8")
    assert BIG in skipped, skipped
    assert "untracked SKIPPED" in p.stdout, p.stdout
    with tarfile.open(out / "looplab-untracked.tar.gz") as tf:
        names = tf.getnames()
    assert BIG not in names and NEW_TEST in names, names


def test_nothing_untracked_leaves_no_empty_archive(bench, tmp_path):
    """A zero-byte tarball beside the bundle reads as "there was nothing", and the distinction
    between that and "the tar failed" is the one this block exists to keep."""
    src, repo = bench
    (repo / NEW_TEST).unlink()
    (repo / IGNORED).unlink()
    out, _p = _snapshot(src, tmp_path / "snaps")
    assert not (out / "looplab-untracked.tar.gz").exists()
    assert not (out / "looplab-untracked-SKIPPED.txt").exists()


def test_the_restore_puts_the_working_tree_back(bench, tmp_path):
    """THE ROUND TRIP, which is the only form of this claim worth having.

    Restoring the 2026-09-10 snapshot by hand, the patch was found by LISTING the directory -- it
    had no reader. So the restore applies both halves into the fresh clone it makes (never the
    live tree, which is that script's whole safety argument) and says what it did.
    """
    src, _repo = bench
    snaps = tmp_path / "snaps"
    _snapshot(src, snaps)
    dest = tmp_path / "restored"
    p = subprocess.run(["bash", str(RESTORE), str(dest), str(snaps)],
                       capture_output=True, text=True, timeout=300)
    tree = dest / "looplab"
    assert (tree / NEW_TEST).is_file(), p.stdout + p.stderr        # the file that was lost
    assert EDITED in (tree / TRACKED).read_text(encoding="utf-8"), p.stdout
    assert "untracked file(s) unpacked" in p.stdout, p.stdout
    assert "uncommitted patch applied" in p.stdout, p.stdout


def test_the_restore_can_be_asked_for_committed_state_only(bench, tmp_path):
    """The operator who wants the commits and nothing else still has that, by name."""
    src, _repo = bench
    snaps = tmp_path / "snaps"
    _snapshot(src, snaps)
    dest = tmp_path / "restored"
    p = subprocess.run(["bash", str(RESTORE), str(dest), str(snaps)],
                       capture_output=True, text=True, timeout=300,
                       env={**os.environ, "RESTORE_COMMITTED_ONLY": "1"})
    tree = dest / "looplab"
    assert (tree / TRACKED).is_file(), p.stdout + p.stderr
    assert EDITED not in (tree / TRACKED).read_text(encoding="utf-8"), p.stdout
    assert not (tree / NEW_TEST).exists(), p.stdout
