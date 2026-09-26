"""`looplab stop --wait`: "stop after the current node" is a command, not a watcher over the log.

THE INCIDENT (doc 68 §0 f): an operator wanted a run to stop once its current node finished and
built it by hand — a watcher over `events.jsonl` plus a kill by PID — and the watcher itself hung on
a `ps` over a process tree in D-state. What the operator could not see is that the mechanism already
existed: a `pause` is honoured at the loop's next iteration, the loop then DRAINS every evaluation
already running (`speculation.py::_drain_adopted_evals`), and only then does the process exit and the
OS release `engine.lock`. A stop never kills an evaluation. The one thing missing was a way to WAIT
for that, and the doc string said only "breaks on its next iteration".

`--wait` waits on the LOCK (`engine/run_lifecycle.py::engine_liveness`), because the log answers the
wrong question: "the pause is folded" is minutes or hours earlier than "nothing is running any more".
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.cli.run_cmds import await_engine_exit
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests._posix_gates import FLOCK


class _Clock:
    """A clock the test DRIVES: `sleep` advances it, so no verdict races a real timer."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_it_returns_as_soon_as_the_lock_is_seen_free():
    answers = iter([True, True, True, False])
    clock, said = _Clock(), []
    assert await_engine_exit(Path("r"), liveness=lambda _rd: next(answers), clock=clock,
                             sleep=clock.sleep, echo=said.append,
                             poll_s=0.5) == ("exited", "")
    assert clock.now == 1.5 and said == []


def test_it_says_what_it_is_waiting_on_at_the_echo_cadence():
    clock, said = _Clock(), []
    alive = {"n": 0}

    def liveness(_rd):
        alive["n"] += 1
        return alive["n"] < 200                     # free after ~100 s of polling at 0.5 s

    assert await_engine_exit(Path("r"), liveness=liveness, clock=clock, sleep=clock.sleep,
                             echo=said.append, describe=lambda: "still waiting: node 3",
                             poll_s=0.5, echo_every_s=30.0) == ("exited", "")
    assert said == ["still waiting: node 3"] * 3    # at 30 s, 60 s and 90 s — and not every poll


def test_a_timeout_gives_up_and_an_unobservable_lock_is_not_guessed():
    clock = _Clock()
    assert await_engine_exit(Path("r"), timeout_s=5.0, liveness=lambda _rd: True, clock=clock,
                             sleep=clock.sleep, echo=lambda _m: None,
                             poll_s=0.5) == ("timeout", "")
    assert 5.0 <= clock.now < 6.0
    # `engine_liveness` answers None where the filesystem cannot lock: "not proven free" is never
    # reported as "exited", and it is never waited on forever either...
    assert await_engine_exit(Path("r"), liveness=lambda _rd: None, clock=clock,
                             sleep=clock.sleep, echo=lambda _m: None) == ("unobservable", "")
    # ...but ONE unreadable probe (a lock file racing its own creation) does not end the wait.
    answers = iter([None, True, None, None, False])
    assert await_engine_exit(Path("r"), liveness=lambda _rd: next(answers), clock=clock,
                             sleep=clock.sleep, echo=lambda _m: None) == ("exited", "")


def test_a_stop_that_no_longer_stands_ends_the_wait_as_lifted():
    """A free lock beside a lifted pause is an engine between two lives, not a stopped run."""
    clock = _Clock()
    verdicts = iter(["", "", "the stop was lifted"])
    assert await_engine_exit(Path("r"), liveness=lambda _rd: True,
                             standing=lambda: next(verdicts), clock=clock, sleep=clock.sleep,
                             echo=lambda _m: None) == ("lifted", "the stop was lifted")


def _run_dir(tmp_path: Path, *, in_flight: bool) -> Path:
    """A real run directory; node 0 admitted to the sandbox by the current owner when `in_flight`."""
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "seed"},
                                  "code": "pass\n"})
    if in_flight:
        store.append("node_eval_started", {"node_id": 0, "generation": 0})
    return rd


def test_with_no_engine_running_it_returns_at_once(tmp_path):
    rd = _run_dir(tmp_path, in_flight=False)
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert "no engine was running" in out.output
    assert fold(EventStore(rd / "events.jsonl").read_all()).paused


def test_a_crashed_engines_stale_receipt_is_not_reported_as_running(tmp_path):
    """`eval_activity_started` outlives a crashed owner until the NEXT one clears it, so the
    in-flight list is printed only while an engine really holds the lock."""
    rd = _run_dir(tmp_path, in_flight=True)
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert "already running" not in out.output and "no engine was running" in out.output


def test_a_pending_resume_request_is_not_a_stopped_run(tmp_path):
    """The server's post-exit waiter spawns `looplab resume` for an unserved request, and that child
    LIFTS the pause — so "the lock is free" is not "the run stopped" while one is pending."""
    rd = _run_dir(tmp_path, in_flight=False)
    EventStore(rd / "events.jsonl").append("resume_requested", {"mode": "resume"})
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 1, out.output
    assert "did not stop" in out.output and "resume request is pending" in out.output


@pytest.mark.parametrize("argv", [["--timeout", "5"], ["--wait", "--timeout", "-1"]])
def test_a_timeout_it_would_ignore_or_misread_is_refused_before_anything_is_appended(tmp_path, argv):
    rd = _run_dir(tmp_path, in_flight=False)
    before = len(EventStore(rd / "events.jsonl").read_all())
    out = CliRunner().invoke(app, ["stop", str(rd), *argv])
    assert out.exit_code == 2, out.output
    assert len(EventStore(rd / "events.jsonl").read_all()) == before, "nothing may be appended"


def _hold_lock(rd: Path, release: threading.Event, held: threading.Event, *, land=None):
    """Hold `engine.lock` the way a live engine does — an exclusive flock on its own open file —
    until `release` fires, then optionally append the in-flight node's terminal first (the drain)."""
    import fcntl                          # POSIX; every caller is gated on `FLOCK`

    with open(rd / "engine.lock", "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        held.set()
        release.wait(10)
        if land is not None:
            land()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@FLOCK
def test_it_waits_for_a_live_engine_and_reports_how_the_running_node_ended(tmp_path):
    """DRIVEN against a real lock holder: the command must still be waiting while the lock is held,
    return once the holder lets go, and say how the node it was waiting on ended."""
    rd = _run_dir(tmp_path, in_flight=True)
    release, held = threading.Event(), threading.Event()

    def _land():
        EventStore(rd / "events.jsonl").append(
            "node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.25, "violations": []})

    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), kwargs={"land": _land},
                              daemon=True)
    holder.start()
    assert held.wait(5)
    threading.Timer(1.0, release.set).start()
    started = time.monotonic()
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    holder.join(5)
    assert out.exit_code == 0, out.output
    assert time.monotonic() - started >= 0.9, "it returned while the engine still held the lock"
    assert "finish 1 evaluation(s) already running (node 0)" in out.output
    assert "node 0: evaluated (metric 0.25)" in out.output
    assert "has exited" in out.output


@FLOCK
def test_a_resume_that_lifts_the_stop_mid_wait_is_reported_not_waited_out(tmp_path):
    """While the engine still holds the lock, a later `resume` lifts the pause (an operator, or the
    handoff a `looplab resume` performs): the wait must say the stop is gone and exit non-zero,
    not keep waiting for an exit that no longer means anything."""
    rd = _run_dir(tmp_path, in_flight=True)
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    holder.start()
    assert held.wait(5)
    threading.Timer(1.0, lambda: EventStore(rd / "events.jsonl").append("resume", {})).start()
    try:
        out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--timeout", "20"])
    finally:
        release.set()
        holder.join(5)
    assert out.exit_code == 1, out.output
    assert "did not stop: the stop was lifted" in out.output


@FLOCK
def test_a_symlinked_run_dir_is_waited_on_not_blamed_on_the_filesystem(tmp_path):
    """`stop` accepts a run dir reached through a symlink; `engine_liveness` refuses one unresolved,
    which read as "this filesystem cannot lock"."""
    rd = _run_dir(tmp_path, in_flight=True)
    link = tmp_path / "latest"
    link.symlink_to(rd, target_is_directory=True)
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    holder.start()
    assert held.wait(5)
    threading.Timer(1.0, release.set).start()
    out = CliRunner().invoke(app, ["stop", str(link), "--wait"])
    holder.join(5)
    assert out.exit_code == 0, out.output
    assert "cannot observe" not in out.output and "has exited" in out.output


@FLOCK
def test_a_timeout_exits_nonzero_and_leaves_the_stop_recorded(tmp_path):
    rd = _run_dir(tmp_path, in_flight=True)
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--timeout", "0.5"])
    finally:
        release.set()
        holder.join(5)
    assert out.exit_code == 1, out.output
    assert "gave up after 0.5s" in out.output
    assert fold(EventStore(rd / "events.jsonl").read_all()).paused, "the stop itself stands"


@FLOCK
def test_without_wait_it_does_not_block_even_on_a_held_lock(tmp_path):
    """The default is unchanged: `stop` records the pause and returns while an engine is still
    running. A direct Python call (Typer's OptionInfo defaults left in place, which are truthy) must
    not start waiting either — that is the compatibility surface `finalize` documents."""
    from looplab.cli.run_cmds import stop

    rd = _run_dir(tmp_path, in_flight=True)
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        started = time.monotonic()
        out = CliRunner().invoke(app, ["stop", str(rd)])
        assert out.exit_code == 0 and "has exited" not in out.output
        stop(rd)                                        # returns, rather than polling a held lock
        assert time.monotonic() - started < 5
    finally:
        release.set()
        holder.join(5)
    assert fold(EventStore(rd / "events.jsonl").read_all()).paused
