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
    """§390. THE PROCESS, not the box at a moment.

    Three earlier versions of this fixture failed for three different reasons and none of them was
    the rule being wrong: `== 0` went red under a second pytest suite, the delta that replaced it
    went red under a live probe (0 before, 8 after -- the probe's workers arrived between the two
    readings), and "pick CPUs that are free right now and watch them" went red when the OTHER suite,
    pinned to the service lane, took the CPUs this fixture had just measured as free. Every one of
    them asked the box a question about a moment. The claim is about a PROCESS: a pinned neighbour
    that is asleep contributes nothing. `cpus_counted_for` answers exactly that, and nothing else on
    the box can make it wrong.
    """
    keep = os.sched_getaffinity(0)
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    try:
        _pin([0, 1])
        mine, total = os.sched_getaffinity(0), os.cpu_count()
        idle = _spawn([4, 5], busy=False)
        time.sleep(1.0)
        # THE MUTATION TARGET. Without the state check the sleeping child's CPUs come back here, and
        # the field it feeds becomes a count of workers that once existed rather than of load.
        assert ruler_selfcheck.cpus_counted_for(idle.pid, mine, total) == set()
        idle.kill(); idle.wait(timeout=10)
    finally:
        os.sched_setaffinity(0, keep)


def test_a_busy_pinned_neighbour_is_counted_as_the_cpus_it_holds():
    """The other side of the same per-process question, so neither half can be removed quietly."""
    keep = os.sched_getaffinity(0)
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    try:
        _pin([0, 1])
        mine, total = os.sched_getaffinity(0), os.cpu_count()
        busy = _spawn([4, 5], busy=True)
        time.sleep(1.0)
        assert ruler_selfcheck.cpus_counted_for(busy.pid, mine, total) == {4, 5}
        busy.kill(); busy.wait(timeout=10)
    finally:
        os.sched_setaffinity(0, keep)


def test_a_process_that_is_gone_contributes_nothing():
    """A pid that vanishes mid-walk is the ordinary case on a box that spawns workers, not an error."""
    assert ruler_selfcheck.cpus_counted_for(2 ** 22, {0, 1}, os.cpu_count() or 8) == set()


def test_work_inside_our_own_lane_is_not_outside_it():
    """The disjointness half of the rule, which nothing here drove (§388).

    Without `not (other & mine)` the function counts the CPUs we are ourselves pinned to -- and the
    reading would call the ruler's own evaluation "a loaded neighbour". A BUSY child sharing our
    lane is the case that separates the two.
    """
    keep = os.sched_getaffinity(0)
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    try:
        _pin(sorted(keep)[:2])
        mine = sorted(os.sched_getaffinity(0))
        busy = _spawn(mine, busy=True)
        time.sleep(1.0)
        total = os.cpu_count()
        assert ruler_selfcheck.cpus_counted_for(busy.pid, set(mine), total) == set()
        busy.kill(); busy.wait(timeout=10)
    finally:
        os.sched_setaffinity(0, keep)


def test_an_unpinned_process_cannot_answer_the_question(monkeypatch):
    """`None` means "not answerable here", and it is not the same answer as zero: a process whose
    affinity is the whole box has no "outside" to look at. Mutating it to `set()` left every other
    test green."""
    mine = os.sched_getaffinity(0)
    monkeypatch.setattr(os, "cpu_count", lambda: len(mine))
    assert ruler_selfcheck.busy_cpus_outside_lane_set() is None
    assert ruler_selfcheck.busy_cpus_outside_lane() is None


def test_the_count_is_exactly_the_size_of_the_set(monkeypatch):
    """One rule, two spellings -- the split introduced in §388 is where they could drift apart.

    Not measured twice on the live box: two walks of /proc a moment apart legitimately differ, and a
    test that tolerated that would tolerate the drift as well.
    """
    monkeypatch.setattr(ruler_selfcheck, "busy_cpus_outside_lane_set", lambda: {4, 5, 90})
    assert ruler_selfcheck.busy_cpus_outside_lane() == 3
    monkeypatch.setattr(ruler_selfcheck, "busy_cpus_outside_lane_set", lambda: set())
    assert ruler_selfcheck.busy_cpus_outside_lane() == 0
    monkeypatch.setattr(ruler_selfcheck, "busy_cpus_outside_lane_set", lambda: None)
    assert ruler_selfcheck.busy_cpus_outside_lane() is None


def test_a_busy_pinned_neighbour_is_counted():
    keep = os.sched_getaffinity(0)
    if os.cpu_count() is None or os.cpu_count() < 8:
        return
    try:
        _pin([0, 1])
        before = ruler_selfcheck.busy_cpus_outside_lane() or 0
        hot = _spawn([4, 5], busy=True)
        deadline = time.time() + 15
        got = before
        while time.time() < deadline and got <= before:
            got = ruler_selfcheck.busy_cpus_outside_lane() or 0
            time.sleep(0.2)
        hot.kill(); hot.wait(timeout=10)
        assert got >= before + 2, f"a neighbour burning two pinned cpus added {got - before}"
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
