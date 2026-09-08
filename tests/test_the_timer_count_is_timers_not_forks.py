"""Five timers where there is one, because bash forks inherit the cmdline.

Measured 2026-09-08 during a sweep: the scan "every process whose cmdline holds `snapshot_timer.sh`
and `_loop`" read **five**, and four were gone a second later — a cycle was running, and bash forks
for a pipeline or a command substitution carry the parent's command line. A DUPLICATE timer is
worth catching (two snapshots race for one lock; §313 drove the loser exiting 3 with nothing
written), which is exactly why the count may not cry wolf whenever a cycle happens to be in flight.

The discriminator is the parent: the daemon is `nohup`ed and reparented to init, a fork of its cycle
has the daemon as its parent. `/proc` cannot be arranged to hold two timers on demand, so the rule
takes a process table and the tests fabricate one.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import pulse  # noqa: E402

TIMER = "/bin/bash benchmarks/snapshot_timer.sh _loop 1800"


def test_a_cycle_in_flight_is_not_four_extra_timers():
    table = [{"pid": "100", "ppid": "1", "cmdline": TIMER}] + [
        {"pid": str(200 + i), "ppid": "100", "cmdline": TIMER} for i in range(4)]
    got = pulse.timer_processes(table)
    assert got["daemons"] == ["100"], got
    assert len(got["forks"]) == 4, got
    assert got["duplicate"] is False, got


def test_two_daemons_are_a_duplicate_and_say_so():
    """The failure the count exists for: two `nohup`ed timers, both children of init."""
    table = [{"pid": "100", "ppid": "1", "cmdline": TIMER},
             {"pid": "101", "ppid": "1", "cmdline": TIMER}]
    got = pulse.timer_processes(table)
    assert got["duplicate"] is True and got["daemons"] == ["100", "101"], got


def test_an_unrelated_process_is_not_a_timer():
    """`snapshot.sh` is not `snapshot_timer.sh`, and a shell that merely mentions the script in an
    argument is neither -- this is the self-match the sweep list warns about with `pkill -f`."""
    table = [{"pid": "300", "ppid": "1", "cmdline": "/bin/bash benchmarks/snapshot.sh"},
             {"pid": "301", "ppid": "1", "cmdline": "grep snapshot_timer.sh /var/log/x"}]
    got = pulse.timer_processes(table)
    assert got["daemons"] == [] and got["forks"] == [], got


def test_the_reader_survives_a_process_that_exits_under_it(tmp_path):
    """A census, not a transaction: a pid that vanishes between `listdir` and `open` is skipped."""
    (tmp_path / "123").mkdir()
    (tmp_path / "123" / "cmdline").write_bytes(TIMER.encode())
    (tmp_path / "123" / "status").write_text("Name:\tbash\nPPid:\t1\n", encoding="utf-8")
    (tmp_path / "456").mkdir()          # no cmdline, no status: the vanishing one
    (tmp_path / "notapid").mkdir()
    table = pulse.process_table(str(tmp_path))
    assert [r["pid"] for r in table] == ["123"], table
    assert pulse.timer_processes(table)["daemons"] == ["123"]
