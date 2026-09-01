"""A shorter source silently replaced a longer archive, and rc stayed 0.

`campaign.sh` does `rm -rf "$TASK_ROOT"` at the start of every attempt, and the task root is keyed by
TASK, not by attempt. So attempt 2 writes a fresh, shorter `events.jsonl` at the very path attempt
1's was archived from.

`archive_tree` already states the right rule -- "LONGER than the source is left alone: the box's
writable layer is where a run gets deleted or restarted, and the archive is the durable half" -- and
applied it one step too late. `cp -ru` ran first, and `-u` copies whenever the SOURCE is newer; a
50-line file written seconds ago is newer than the 400-line one archived yesterday. By the time the
repair loop looked, both were short and there was nothing left for it to see.

Driven end to end on the real function, 2026-08-31: 400 lines archived, a 50-line second attempt,
archive came back 50 lines with zero rows of attempt 1 in it, rc=0, no message.

The fix keeps the superseded copy beside the new one. `.superseded-N` and not an attempt number,
because this function cannot see attempts -- only that something shorter is about to replace
something longer, which is all it needs to know.
"""
import subprocess
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot.sh"


def _archive_tree(src: Path, arch: Path) -> subprocess.CompletedProcess:
    """Run the real `archive_tree` out of snapshot.sh, nothing else from that script."""
    fn = subprocess.run(
        ["sed", "-n", "/^archive_tree() {/,/^}/p", str(SNAPSHOT)],
        capture_output=True, text=True, timeout=120,
    ).stdout
    assert "cp -ru" in fn, "could not extract archive_tree from snapshot.sh"
    return subprocess.run(
        ["bash", "-c", f"set -u\n{fn}\narchive_tree {src} {arch}"],
        capture_output=True, text=True, timeout=600,
    )


def _write(p: Path, n: int, attempt: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join('{"i": %d, "attempt": %d}\n' % (i, attempt) for i in range(n)))


def test_a_restart_does_not_take_the_previous_attempt_with_it(tmp_path):
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"

    _write(log, 400, attempt=1)
    assert _archive_tree(src, arch).returncode == 0
    archived = arch / "dsX" / "runs" / "t" / "run" / "events.jsonl"
    assert archived.read_text().count("\n") == 400

    # attempt 2: campaign.sh's rm -rf, then a shorter log at the same path
    import shutil
    shutil.rmtree(src)
    _write(log, 50, attempt=2)
    r = _archive_tree(src, arch)
    assert r.returncode == 0, r.stdout + r.stderr

    assert archived.read_text().count("\n") == 50, "the live copy should track the current attempt"
    kept = archived.parent / "events.jsonl.superseded-1"
    assert kept.is_file(), (
        "attempt 1's evidence was overwritten by a shorter attempt 2 and nothing kept it:\n"
        + r.stdout + r.stderr
    )
    body = kept.read_text()
    assert body.count("\n") == 400, f"the superseded copy is not the full first attempt: {len(body)}"
    assert '"attempt": 1' in body and '"attempt": 2' not in body


def test_it_says_so_rather_than_keeping_the_copy_quietly(tmp_path):
    """A silent rescue leaves the operator believing one attempt ever ran."""
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"
    _write(log, 400, attempt=1)
    _archive_tree(src, arch)
    import shutil
    shutil.rmtree(src)
    _write(log, 50, attempt=2)
    r = _archive_tree(src, arch)
    assert "superseded-1" in (r.stdout + r.stderr), (
        "the superseded copy was kept but never mentioned:\n" + r.stdout + r.stderr
    )


def test_every_restart_gets_its_own_layer(tmp_path):
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"
    import shutil

    for attempt, n in ((1, 400), (2, 50), (3, 1)):
        if src.exists():
            shutil.rmtree(src)
        _write(log, n, attempt)
        assert _archive_tree(src, arch).returncode == 0

    run = arch / "dsX" / "runs" / "t" / "run"
    kept = sorted(p.name for p in run.iterdir() if ".superseded-" in p.name)
    assert kept == ["events.jsonl.superseded-1", "events.jsonl.superseded-2"], (
        f"three attempts should leave two superseded layers, found {kept}"
    )
    assert (run / "events.jsonl.superseded-1").read_text().count("\n") == 400
    assert (run / "events.jsonl.superseded-2").read_text().count("\n") == 50


def test_a_growing_live_run_is_not_mistaken_for_a_restart(tmp_path):
    """The ordinary case -- a run appending while the snapshot reads it -- must stay untouched."""
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"

    _write(log, 100, attempt=1)
    assert _archive_tree(src, arch).returncode == 0
    _write(log, 300, attempt=1)                       # the run kept going
    r = _archive_tree(src, arch)
    assert r.returncode == 0, r.stdout + r.stderr

    run = arch / "dsX" / "runs" / "t" / "run"
    assert (run / "events.jsonl").read_text().count("\n") == 300
    assert not [p for p in run.iterdir() if ".superseded-" in p.name], (
        "a run that merely GREW was archived as if it had restarted -- every cycle of every live "
        "run would duplicate its log"
    )


# ------------------------------------- size was the wrong question; continuation is the right one
#
# The first rule preserved an archived file only when it was LONGER than its source, and nothing
# makes a second attempt shorter than a first. Measured 2026-09-01: an attempt-2 log of EQUAL length
# replaced 400 archived rows of attempt 1 with 400 rows of attempt 2 -- no `.superseded` written,
# no row of attempt 1 left, rc=0, in silence. A longer second attempt did the same. These are
# append-only logs, so the only benign difference is growth: the archive must be a PREFIX of the
# source.


def test_a_restart_of_equal_length_does_not_take_the_previous_attempt_with_it(tmp_path):
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"

    _write(log, 400, attempt=1)
    assert _archive_tree(src, arch).returncode == 0
    archived = arch / "dsX" / "runs" / "t" / "run" / "events.jsonl"
    assert archived.read_text().count("\n") == 400

    import shutil
    shutil.rmtree(src)
    _write(log, 400, attempt=2)          # SAME length, different content
    r = _archive_tree(src, arch)
    assert r.returncode == 0, r.stdout + r.stderr

    kept = archived.parent / "events.jsonl.superseded-1"
    assert kept.is_file(), (
        "an equal-length restart overwrote the archive and nothing was kept:\n" + r.stdout + r.stderr
    )
    body = kept.read_text()
    assert '"attempt": 1' in body and '"attempt": 2' not in body
    assert body.count("\n") == 400


def test_a_longer_restart_is_also_preserved(tmp_path):
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"
    _write(log, 400, attempt=1)
    assert _archive_tree(src, arch).returncode == 0

    import shutil
    shutil.rmtree(src)
    _write(log, 900, attempt=2)          # LONGER, and not a continuation
    assert _archive_tree(src, arch).returncode == 0
    run = arch / "dsX" / "runs" / "t" / "run"
    kept = [p for p in run.iterdir() if ".superseded-" in p.name]
    assert kept, "a longer restart wiped the archive, which size alone can never notice"
    assert '"attempt": 1' in kept[0].read_text()


def test_a_live_run_that_merely_appended_is_still_left_alone(tmp_path):
    """The ordinary case must stay free: growth is a prefix, and a prefix is not a restart."""
    src, arch = tmp_path / "src" / "dsX", tmp_path / "arch"
    log = src / "runs" / "t" / "run" / "events.jsonl"

    _write(log, 100, attempt=1)
    assert _archive_tree(src, arch).returncode == 0
    with open(log, "a") as fh:
        for i in range(100, 300):
            fh.write('{"i": %d, "attempt": %d}\n' % (i, 1))
    r = _archive_tree(src, arch)
    assert r.returncode == 0, r.stdout + r.stderr

    run = arch / "dsX" / "runs" / "t" / "run"
    assert not [p for p in run.iterdir() if ".superseded-" in p.name], (
        "a run that only appended was archived as if it had restarted -- every cycle of every live "
        "run would duplicate its log"
    )
    assert (run / "events.jsonl").read_text().count("\n") == 300
