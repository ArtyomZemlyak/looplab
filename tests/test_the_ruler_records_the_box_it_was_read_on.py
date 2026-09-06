"""A reading is a measurement of a task AND of the box it was taken on.

§313. `discrete_log` read 0.9380 at 06:38 with three sibling lanes self-checking, and 1.0274 on a
quiet box two hours later -- same lane width, same interpreter, same cached baseline, same task.
The 9.5 % between them is the box, and the recorded row said nothing about it, so the series read
as drift in the ruler. The fix has two halves and both are tested here: the reading carries the
count, and the count means what its name says.

The second half is the one that needed driving. §295's version counted every process whose affinity
was a disjoint subset -- running or not -- and on this box that included 23 orphaned forkservers
from a six-hour-dead run. It read 22 on an idle box and 22 under a fully loaded neighbouring lane.
A fixture that only ever shows a BUSY neighbour passes on both versions, so every test below that
touches the count keeps an IDLE pinned neighbour in the picture.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402
import ruler_selfcheck  # noqa: E402


def _pin(cpus):
    os.sched_setaffinity(0, set(cpus))


def _spawn(cpus, busy: bool, exe: str = sys.executable, orphan: bool = False):
    """A child pinned to `cpus`, either burning them or asleep on them."""
    code = ("import os,time\n"
            f"os.sched_setaffinity(0, {set(cpus)!r})\n"
            + ("t=time.time()\nwhile time.time()-t < 30: pass\n" if busy else "time.sleep(30)\n"))
    if not orphan:
        return subprocess.Popen([exe, "-c", code])
    # DOUBLE FORK, so the survivor is reparented to init and looks like what the bench leaves
    # behind. A test that fabricated ppid 1 by other means would not be testing the same thing.
    mid = subprocess.Popen([exe, "-c",
                            "import subprocess,sys,os\n"
                            "p=subprocess.Popen([sys.argv[1],'-c',sys.argv[2]])\n"
                            "print(p.pid, flush=True)\n", exe, code],
                           stdout=subprocess.PIPE, text=True)
    pid = int(mid.stdout.readline().strip())
    mid.wait(timeout=30)
    return pid


def test_an_idle_pinned_neighbour_is_not_counted_as_load():
    keep = os.sched_getaffinity(0)
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    try:
        _pin([0, 1])
        idle = _spawn([4, 5], busy=False)
        time.sleep(1.0)
        # THE MUTATION TARGET. Without the state check this reads 2, and the field it feeds becomes
        # a count of workers that once existed rather than of a box under load.
        assert ruler_selfcheck.busy_cpus_outside_lane() == 0
        idle.kill(); idle.wait(timeout=10)
    finally:
        os.sched_setaffinity(0, keep)


def test_a_busy_pinned_neighbour_is_counted():
    keep = os.sched_getaffinity(0)
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    try:
        _pin([0, 1])
        hot = _spawn([4, 5], busy=True)
        deadline = time.time() + 15
        got = 0
        while time.time() < deadline and got == 0:
            got = ruler_selfcheck.busy_cpus_outside_lane() or 0
            time.sleep(0.2)
        hot.kill(); hot.wait(timeout=10)
        assert got >= 2, f"a neighbour burning two pinned cpus read {got}"
    finally:
        os.sched_setaffinity(0, keep)


def test_the_recorded_reading_carries_the_count():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "readings.jsonl"
        ruler_selfcheck.append_reading(log, "pagerank", "test", [1.0, 1.1], 1.05,
                                       stamp="2026-09-06T12:00:00", lane="0-10,48-58", busy=66)
        row = json.loads(log.read_text(encoding="utf-8").strip())
        assert row["busy_cpus_outside_lane"] == 66
        # And a reading taken without the measurement says so rather than claiming a quiet box:
        # None is not zero, and a comparison that treats them the same re-tells §313's story.
        ruler_selfcheck.append_reading(log, "pagerank", "test", [1.0], 1.0,
                                       stamp="2026-09-06T12:00:01", lane=None)
        second = json.loads(log.read_text(encoding="utf-8").splitlines()[1])
        assert second["busy_cpus_outside_lane"] is None


def test_the_selfcheck_samples_the_count_while_it_runs_not_after():
    src = (BENCH / "ruler_selfcheck.py").read_text(encoding="utf-8")
    body = src[src.index("for _ in range(max(1, args.reps)):"):src.index("for why in bad:")]
    # Sampling only after the loop reads the box once every neighbour has stopped, which is how
    # the 06:38 rows came to describe a quiet box they were not taken on.
    assert body.count("busy_cpus_outside_lane()") >= 2, body


def test_orphaned_bench_workers_are_reported_and_live_ones_are_not():
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "AlgoTune" / ".venv" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.symlink_to(sys.executable)
        # One orphan (ppid 1) and one process with the SAME command line that still has a parent.
        # The second is the fixture that disagrees with the bug: a check that forgot the ppid test
        # would count it, and every running probe's workers with it.
        orphan_pid = _spawn([6, 7], busy=False, exe=str(exe), orphan=True)
        parented = _spawn([6, 7], busy=False, exe=str(exe))
        try:
            time.sleep(1.5)
            got = pulse.orphans(str(tmp))
            assert got["count"] == 1, got
            assert got["cpus"] == 2 and got["rss_mib"] > 0, got
        finally:
            parented.kill(); parented.wait(timeout=10)
            try:
                os.kill(orphan_pid, 9)
            except ProcessLookupError:
                pass
