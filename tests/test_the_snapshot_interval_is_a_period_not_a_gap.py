"""`snapshot_timer.sh 1800` must snapshot every 1800 s, not every 1800 s PLUS the snapshot.

The loop slept the full interval AFTER each snapshot, so the period was interval + duration. That
is invisible at 127 s snapshots (1927 s instead of 1800) and it is not invisible at 1765 s, which is
what `20260904-094347` took: everything but the runs archive finished in 7 s, and `cp -ru` spent
1758 s copying four probe trees that were LIVE and growing under it. The archive step scales with
how many probes are RUNNING, so a full bench stretched the period towards 3565 s — the recovery
window doubled and the only number the sweep reads, snapshot age, stayed under its threshold.

These tests run the real `_loop` against a stub `snapshot.sh` that sleeps, and time the ticks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "benchmarks"


def _stand(tmp_path: Path, snapshot_seconds: float, interval: int):
    """A timer beside a stub `snapshot.sh` that takes `snapshot_seconds` and always succeeds."""
    bench = tmp_path / "bench"
    (bench / "logs").mkdir(parents=True)
    # `meter/` is one of the trees `fingerprint` watches, and it must MOVE every tick or the loop
    # reports "nothing new" and snapshots once. The first version of this stand wrote its marker
    # somewhere nothing watched and timed exactly one tick.
    (bench / "meter").mkdir()
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(HERE / "snapshot_timer.sh", run / "snapshot_timer.sh")
    shutil.copy(HERE / "bench_trees.sh", run / "bench_trees.sh")
    # The fingerprint must CHANGE every tick, or the loop skips the snapshot and times nothing.
    (run / "snapshot.sh").write_text(
        "#!/bin/bash\n"
        f"date +%s.%N >> {bench}/ticks\n"
        f"sleep {snapshot_seconds}\n"
        f"date +%s.%N > {bench}/meter/moved-$$\n"
        "exit 0\n", encoding="utf-8")
    os.chmod(run / "snapshot.sh", 0o755)
    env = dict(os.environ, BENCH_ROOT=str(bench), SNAPSHOT_DEST=str(tmp_path / "dest"))
    return run, bench, env


def _ticks(bench: Path) -> list[float]:
    body = (bench / "ticks").read_text(encoding="utf-8") if (bench / "ticks").exists() else ""
    return [float(x) for x in body.split()]


def _run_loop(run: Path, env, interval: int, seconds: float) -> list[float]:
    proc = subprocess.Popen(["bash", str(run / "snapshot_timer.sh"), "_loop", str(interval)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(seconds)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    return proc.stdout.read() if proc.stdout else ""


def test_a_slow_snapshot_does_not_stretch_the_period(tmp_path):
    """Interval 4 s, snapshot 3 s. The period must stay ~4 s, not become ~7 s."""
    run, bench, env = _stand(tmp_path, snapshot_seconds=3.0, interval=4)
    _run_loop(run, env, 4, 13.5)
    ticks = _ticks(bench)
    assert len(ticks) >= 3, f"only {len(ticks)} snapshots in 13.5 s; the loop did not run"
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 5.5, (
        f"gaps {[round(g, 2) for g in gaps]}: the loop slept the interval AFTER the snapshot, so "
        "the period is interval + duration (4 + 3 = 7), not the interval the operator asked for")


def test_an_overrunning_snapshot_says_so_instead_of_running_quietly(tmp_path):
    """Snapshot 3 s under a 1 s interval: back to back is right, silence is not."""
    run, bench, env = _stand(tmp_path, snapshot_seconds=3.0, interval=1)
    out = _run_loop(run, env, 1, 8.0)
    assert re.search(r"took \d+s, at or over the 1s interval", out), (
        f"an overrunning tick must name itself; got:\n{out}")
    gaps = [b - a for a, b in zip(_ticks(bench), _ticks(bench)[1:])]
    assert gaps and max(gaps) < 4.5, f"expected back-to-back ~3 s ticks, got {gaps}"


def test_the_loop_still_sleeps_when_the_snapshot_is_fast(tmp_path):
    """The remainder must not collapse to zero: a 0.2 s snapshot under a 3 s interval still waits
    ~3 s. A fix that always ran back to back would pass the first test and burn the box."""
    run, bench, env = _stand(tmp_path, snapshot_seconds=0.2, interval=3)
    _run_loop(run, env, 3, 7.0)
    ticks = _ticks(bench)
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps, f"only {len(ticks)} ticks"
    assert min(gaps) > 2.0, f"gaps {[round(g, 2) for g in gaps]}: the loop stopped sleeping"
