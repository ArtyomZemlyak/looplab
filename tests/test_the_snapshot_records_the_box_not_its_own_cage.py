"""PROVENANCE.txt says how big the machine was. It was saying how big the snapshot's cage was.

`nproc` reports the caller's CPU affinity. §259 pinned the snapshot to the eight service lanes to
stop it stealing CPU from the arm, and from that tick onward the provenance line of a 96-cpu box
read `nproc 8` -- measured in the timer log as 112 snapshots against 115 earlier ones that say 96.

It matters because of §262: the baseline cache is keyed by LANE WIDTH (`w22x1r3`), and that key is
what makes two timings comparable at all. A restorer reading "8 cpus" off an archive whose rulers
were built on 22 has a contradiction and nothing in the snapshot to resolve it -- in the one file
whose stated purpose is answering "is this mine?".
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot.sh"


def _provenance_line() -> str:
    """The real line out of the real script -- not a paraphrase of it."""
    for line in SNAPSHOT.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith('echo "cpus ') or line.strip().startswith('echo "nproc '):
            return line.strip()
    raise AssertionError("no cpu line in the provenance block")


def _emit(cpu_list: str) -> str:
    return subprocess.run(["taskset", "-c", cpu_list, "bash", "-c", _provenance_line()],
                          capture_output=True, text=True, timeout=60).stdout.strip()


def test_the_box_size_is_the_box_not_the_affinity_of_whoever_took_the_snapshot():
    out = _emit("0")
    got = re.search(r"cpus (\d+) on the box", out)
    assert got, f"no box size in {out!r}"
    assert int(got.group(1)) == os.cpu_count(), (
        f"{out!r} reports {got.group(1)} cpus while pinned to one, on a "
        f"{os.cpu_count()}-cpu box")


def test_the_pinning_is_still_recorded_because_it_is_the_thing_that_keys_the_ruler():
    """Dropping the affinity entirely would be the opposite error: §262's whole point is that the
    lane width a measurement ran under is load-bearing, so the snapshot must still say it."""
    out = _emit("0")
    got = re.search(r"pinned to (\d+)", out)
    assert got and int(got.group(1)) == 1, f"{out!r} does not record that it ran on one cpu"


def test_the_two_numbers_are_told_apart_by_the_line_itself():
    """A reader who has to know which `nproc` variant produced which number has not been told
    anything. Both numbers carry their own words."""
    out = _emit("0")
    assert "on the box" in out and "pinned to" in out, out
    assert os.cpu_count() != 1, "this box cannot discriminate the two numbers"
