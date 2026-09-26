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

import itertools
import json
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


def test_it_returns_once_the_lock_has_stayed_free():
    answers = itertools.chain([True, True, True], itertools.repeat(False))
    clock, said = _Clock(), []
    assert await_engine_exit(Path("r"), liveness=lambda _rd: next(answers), clock=clock,
                             sleep=clock.sleep, echo=said.append,
                             poll_s=0.5, settle_s=1.0) == ("exited", "")
    assert clock.now == 2.5 and said == []       # free at 1.5 s, and still free a second later


def test_a_lock_taken_straight_back_is_waited_on_and_its_lift_reported():
    """A `looplab resume` in its hand-off wait takes the lock within moments of the release and only
    then appends the `resume` that lifts the stop — so a free lock must STAY free before it counts."""
    clock = _Clock()
    answers = iter([True, False, True, True, True])
    lifted = iter(["", "", "", "", "the stop was lifted"])
    assert await_engine_exit(Path("r"), liveness=lambda _rd: next(answers),
                             standing=lambda: next(lifted), clock=clock, sleep=clock.sleep,
                             echo=lambda _m: None, poll_s=0.5,
                             settle_s=1.0) == ("lifted", "the stop was lifted")
    # ...and one that finishes the run instead (a finalize driver) is waited out to its own exit.
    clock = _Clock()
    answers = itertools.chain([True, False, True, True], itertools.repeat(False))
    assert await_engine_exit(Path("r"), liveness=lambda _rd: next(answers), clock=clock,
                             sleep=clock.sleep, echo=lambda _m: None, poll_s=0.5,
                             settle_s=1.0) == ("exited", "")
    assert clock.now == 3.0


def test_a_timeout_never_fires_while_a_free_lock_is_settling():
    """"still holds its lock" would be false: the settle is bounded, so it finishes instead."""
    clock = _Clock()
    assert await_engine_exit(Path("r"), timeout_s=0.5, liveness=lambda _rd: False, clock=clock,
                             sleep=clock.sleep, echo=lambda _m: None, poll_s=0.5,
                             settle_s=1.0) == ("exited", "")


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
    answers = itertools.chain([None, True, None, None], itertools.repeat(False))
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
    assert "does not stand" in out.output and "resume request is pending" in out.output
    assert "the run stays stopped" not in out.output, "it does not stand and stays stopped at once"
    # WHO serves it, with the conditions (third critic pass: an unconditional "the first server
    # started on this run's root serves it" was false for a server rooted elsewhere or without
    # the task snapshot).
    assert "whose root is this run's parent directory" in out.output
    assert "task snapshot" in out.output and "so does `looplab resume`" in out.output


def test_a_pending_finalize_request_is_named_as_one(tmp_path):
    """A finalize request is served by `looplab finalize`, which wraps the run up — it does not
    lift the stop, and the message must not say it does."""
    rd = _run_dir(tmp_path, in_flight=False)
    EventStore(rd / "events.jsonl").append("resume_requested", {"mode": "finalize"})
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 1, out.output
    assert "finalize request is pending" in out.output and "wraps the run up" in out.output
    assert "lifts the stop" not in out.output


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
    assert "does not stand: the stop was lifted" in out.output


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


# ------------------------------------------------ the server's other family of engine starter
def _command_record(rd: Path, *, event_type: str, policy: str, status: str = "executing",
                    name: str = "0" * 32, age_s: float = 0.0, error=None) -> Path:
    """A durable command record the way `serve/run_commands.py` writes one (the keys this reads),
    last touched `age_s` ago."""
    directory = rd / ".commands"
    directory.mkdir(exist_ok=True)
    path = directory / f"cmd_{name}.json"
    record = {"id": f"cmd_{name}", "status": status, "event_type": event_type,
              "engine_policy": policy, "postcondition": "engine_ack",
              "updated_at": time.time() - age_s}
    if error is not None:
        record["error"] = error
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_server_command_that_will_start_an_engine_means_the_stop_does_not_stand(tmp_path):
    """An `ENSURE_RUNNING` command that arrives while a stopped engine drains is not served by it:
    its worker waits for the exit and then starts `looplab resume`, which LIFTS the stop — and that
    plan is in `.commands/`, never in the log (critic 2026-09-26: "has exited", exit 0)."""
    rd = _run_dir(tmp_path, in_flight=False)
    _command_record(rd, event_type="budget_extend", policy="ensure_running")
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 1, out.output
    assert "does not stand: server command(s) `budget_extend`" in out.output
    assert "will start an engine now that no engine holds the lock" in out.output


def test_a_command_no_live_worker_holds_is_a_note_not_a_failure(tmp_path):
    """A server killed mid-command leaves its record `executing` for good; reading that as an
    imminent restart failed every later `stop --wait` (second critic pass, driven with SIGKILL).
    Nothing starts it NOW, so the stop stands — and the wait SAYS what would lift it."""
    rd = _run_dir(tmp_path, in_flight=False)
    _command_record(rd, event_type="node_reset", policy="ensure_running", age_s=3600)
    claim = rd / ".commands" / f".cmd_{'0' * 32}.executing"
    claim.write_text(json.dumps({"pid": 999999, "created_at": 0}), encoding="utf-8")
    old = time.time() - 3600
    import os
    os.utime(claim, (old, old))                     # the dead server's last heartbeat
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert "no engine was running" in out.output
    assert "note: server command(s) `node_reset`" in out.output
    assert "no worker has shown life for 30 s" in out.output
    # ...and what brings it back, since nothing cancels it before its deadline: a GET, a
    # re-submission the UI attaches to, a server restart for a `restart` — NOT "an open tab", which
    # polls a submitted command for 8 s and stops (fourth critic pass, driven).
    assert "a GET of the command" in out.output and "submitted again" in out.output
    assert "`restart`, the next server started on this run's parent directory" in out.output
    assert "open LoopLab tab" not in out.output and "Commands view" not in out.output


def test_the_fresher_of_the_claim_and_the_record_is_the_pulse(tmp_path):
    """A record written a moment ago is not stale because an OLD claim file sits beside it."""
    from looplab.cli.run_cmds import server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    _command_record(rd, event_type="fork", policy="ensure_running")
    claim = rd / ".commands" / f".cmd_{'0' * 32}.executing"
    claim.write_text("{}", encoding="utf-8")
    import os
    os.utime(claim, (1, 1))
    assert server_commands_restarting(rd)["coming"] == [f"`fork` (cmd_{'0' * 32}, executing)"]


def test_an_old_uncertain_start_is_a_note_about_the_child_it_may_have_left(tmp_path):
    rd = _run_dir(tmp_path, in_flight=False)
    _command_record(rd, event_type="fork", policy="ensure_running", status="failed",
                    error={"code": "engine_start_uncertain"}, age_s=3600)
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert "could not tell whether they started an engine" in out.output


def test_an_unreadable_record_starts_nothing_and_names_its_remedy(tmp_path):
    """A record the server's own reader rejects is driven by NOTHING — a GET of it answers 503 and
    no recovery reads it — so the stop stands; what it does do is block every later command, and
    the note names the quarantine (fourth critic pass)."""
    rd = _run_dir(tmp_path, in_flight=False)
    (rd / ".commands").mkdir()
    (rd / ".commands" / f"cmd_{'7' * 32}.json").write_text("{not json", encoding="utf-8")
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert f"note: command record(s) cmd_{'7' * 32} cannot be read" in out.output
    assert "resolve-activity-claims" in out.output


def test_a_large_settled_record_is_read_not_called_unreadable(tmp_path):
    """The server writes a record with `indent=2` and every non-ASCII character escaped, so a
    real `inject_node` exceeded the old 1 MiB bound (2,401,601 bytes, driven) — and a SETTLED one
    then made every `stop --wait` exit 1 with a remedy that did nothing."""
    from looplab.cli.run_cmds import server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    path = _command_record(rd, event_type="inject_node", policy="ensure_running",
                           status="timed_out")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["data"] = {"code": "ж" * 400_000}
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    assert path.stat().st_size > (1 << 20)
    assert server_commands_restarting(rd) == {"coming": [], "stale": [], "uncertain": [],
                                              "unreadable": []}
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output


def test_a_command_the_engine_already_acknowledged_or_let_expire_starts_nothing(tmp_path):
    """A server killed after the engine ACKED its command leaves the record `executing`; a GET
    settles it `succeeded` and spawns nothing, so it is no note at all. Nor is one past its
    deadline, which a re-drive now settles `timed_out` (fourth critic pass)."""
    from looplab.cli.run_cmds import _command_acks, server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    store = EventStore(rd / "events.jsonl")
    path = _command_record(rd, event_type="node_reset", policy="ensure_running", age_s=3600,
                           name="a" * 32)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["event_seq"] = 7
    path.write_text(json.dumps(record), encoding="utf-8")
    assert server_commands_restarting(rd)["stale"], "unacked, it is a stale note"
    store.append("command_ack", {"command_id": f"cmd_{'a' * 32}", "event_seq": 7})
    acks = _command_acks(store.read_all())
    assert (f"cmd_{'a' * 32}", 7) in acks
    assert server_commands_restarting(rd, acked=acks)["stale"] == []
    expired = _command_record(rd, event_type="fork", policy="ensure_running", name="b" * 32)
    record = json.loads(expired.read_text(encoding="utf-8"))
    record["absolute_deadline_at"] = time.time() - 1
    expired.write_text(json.dumps(record), encoding="utf-8")
    assert server_commands_restarting(rd, acked=acks)["coming"] == []
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0 and "note: server command" not in out.output, out.output


@pytest.mark.parametrize("updated", [10 ** 400, "1e999"])
def test_a_hand_edited_pulse_neither_crashes_the_wait_nor_lives_forever(tmp_path, updated):
    """`float()` of a 400-digit integer raised `OverflowError` AFTER the pause was appended, and
    `1e999` (infinity) read as a worker alive forever (fourth critic pass, driven)."""
    rd = _run_dir(tmp_path, in_flight=False)
    path = _command_record(rd, event_type="fork", policy="ensure_running")
    raw = path.read_text(encoding="utf-8")
    record = json.loads(raw)
    marker = str(record["updated_at"])
    path.write_text(raw.replace(marker, str(updated)), encoding="utf-8")
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert "note: server command(s) `fork`" in out.output


def test_a_recent_uncertain_engine_start_counts_as_coming(tmp_path):
    """Terminal, yet the child it may have launched can still be importing."""
    from looplab.cli.run_cmds import server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    _command_record(rd, event_type="fork", policy="ensure_running", status="failed",
                    error={"code": "engine_start_uncertain"})
    assert server_commands_restarting(rd) == {
        "coming": [f"`fork` (cmd_{'0' * 32}, engine start uncertain — its child may still be "
                   "starting)"], "stale": [], "uncertain": [], "unreadable": []}


def test_only_an_unsettled_engine_starting_record_counts(tmp_path):
    from looplab.cli.run_cmds import server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    empty = {"coming": [], "stale": [], "uncertain": [], "unreadable": []}
    assert server_commands_restarting(rd) == empty
    _command_record(rd, event_type="budget_extend", policy="ensure_running", status="succeeded",
                    name="1" * 32)
    _command_record(rd, event_type="hint", policy="no_spawn", name="2" * 32)
    _command_record(rd, event_type="run_abort", policy="ensure_driver_preserve_stop",
                    name="3" * 32)                     # a finalize driver never lifts the pause
    assert server_commands_restarting(rd) == empty
    _command_record(rd, event_type="restart", policy="restart_after_exit", status="accepted",
                    name="4" * 32)
    (rd / ".commands" / f"cmd_{'5' * 32}.json").write_text("{not json", encoding="utf-8")
    _command_record(rd, event_type="fork", policy="ensure_running", name="6" * 32, age_s=3600)
    assert server_commands_restarting(rd) == {
        "coming": [f"`restart` (cmd_{'4' * 32}, accepted)"],
        "stale": [f"`fork` (cmd_{'6' * 32}, executing)"], "uncertain": [],
        "unreadable": [f"cmd_{'5' * 32}"]}


def test_the_record_a_real_command_worker_leaves_is_the_one_read(tmp_path):
    """Pinned against the WRITER: a `budget_extend` submitted to the real command service while an
    engine is alive (so its worker waits on the exit) is named; the `hint` beside it is not."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from looplab.cli.run_cmds import server_commands_restarting
    from looplab.serve.run_commands import RunCommandService
    from looplab.serve.server import make_app
    from tests.factories import post_command

    rd = _run_dir(tmp_path, in_flight=False)
    (rd / "task.snapshot.json").write_text('{"kind":"quadratic","goal":"g","direction":"min"}',
                                          encoding="utf-8")
    application = make_app(tmp_path)
    srv = application.state.looplab
    srv.commands = RunCommandService(
        srv, engine_alive=lambda _rd: True, spawn_engine=lambda *a, **k: 4242,
        process_alive=lambda _pid: True, process_identity=lambda _pid: "child",
        startup_timeout=0.05, command_timeout=5.0, poll_interval=0.01,
        max_observation_timeout=10.0)
    client = TestClient(application)
    # One active command per run: the `hint` (NO_SPAWN, settled once its intent is folded) first.
    hint = post_command(client, "hint", {"text": "keep going"}, key="h", run_id="run").json()
    deadline = time.monotonic() + 10
    while client.get(f"/api/runs/run/commands/{hint['id']}").json()["status"] != "succeeded":
        assert time.monotonic() < deadline, "the hint never settled"
        time.sleep(0.02)
    assert server_commands_restarting(rd) == {"coming": [], "stale": [], "uncertain": [],
                                              "unreadable": []}
    assert post_command(client, "budget_extend", {"add_nodes": 2}, run_id="run").status_code < 300
    found = server_commands_restarting(rd)
    assert (len(found["coming"]) == 1 and found["coming"][0].startswith("`budget_extend` (cmd_")
            and not found["stale"]), found


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_a_timeout_no_elapsed_time_can_reach_is_refused(tmp_path, value):
    rd = _run_dir(tmp_path, in_flight=False)
    before = len(EventStore(rd / "events.jsonl").read_all())
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--timeout", value])
    assert out.exit_code == 2, out.output
    assert len(EventStore(rd / "events.jsonl").read_all()) == before


def test_the_wait_refolds_only_when_the_log_moved(tmp_path, monkeypatch):
    """Every poll asks whether the stop still stands; a whole-log fold per poll was a third of a core
    on a 30k-event log (critic 2026-09-26)."""
    from looplab.cli import run_cmds

    rd = _run_dir(tmp_path, in_flight=False)
    store = EventStore(rd / "events.jsonl")
    calls = []
    monkeypatch.setattr(run_cmds, "fold", lambda events: calls.append(len(events)) or fold(events))
    current = run_cmds._TailFold(store)
    first = current()
    assert current() is first and current() is first and len(calls) == 1
    store.append("pause", {})
    assert current().paused and len(calls) == 2
    # A SAME-LENGTH replacement (a reset in place) whose last row lifts the pause: seqs are dense,
    # so a `(length, last seq)` key served the stale "paused" (second critic pass, driven).
    events = store.read_all()
    replaced = tmp_path / "replaced.jsonl"
    other = EventStore(replaced)
    for event in events[:-1]:
        other.append(event.type, event.data)
    other.append("resume", {})
    import os
    os.replace(replaced, rd / "events.jsonl")
    assert not current().paused and len(calls) == 3


def test_an_inconclusive_first_probe_does_not_turn_a_wait_into_no_engine(tmp_path, monkeypatch):
    """`was_alive` came from ONE probe: an unreadable first one made an hours-long wait end with
    "no engine was running". Whether an engine was EVER seen is what the last line reports."""
    from looplab.engine import run_lifecycle

    rd = _run_dir(tmp_path, in_flight=True)
    answers = itertools.chain([None, True, True], itertools.repeat(False))
    monkeypatch.setattr(run_lifecycle, "engine_liveness", lambda _rd: next(answers))
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0, out.output
    assert "has exited" in out.output and "no engine was running" not in out.output


@FLOCK
def test_a_lock_retaken_after_the_release_is_waited_on_not_reported_exited(tmp_path):
    """DRIVEN end to end: a `looplab resume` in its hand-off wait takes the lock right after the
    engine lets go and only then lifts the stop. Without the settle the wait said "has exited" at
    the first free probe (the lift lands 0.9 s after the release, the takeover 0.6 s after it)."""
    rd = _run_dir(tmp_path, in_flight=True)
    release, held = threading.Event(), threading.Event()
    first = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    first.start()
    assert held.wait(5)

    def _handoff():
        release.set()
        first.join(5)                                     # the engine lets go...
        time.sleep(0.6)                                   # ...the hand-off takes the lock...
        taken, done = threading.Event(), threading.Event()
        holder = threading.Thread(target=_hold_lock, args=(rd, done, taken), daemon=True)
        holder.start()
        taken.wait(5)
        time.sleep(0.3)
        EventStore(rd / "events.jsonl").append("resume", {})    # ...and only then lifts the stop
        time.sleep(1.0)
        done.set()
        holder.join(5)

    threading.Timer(1.0, _handoff).start()
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait", "--timeout", "20"])
    assert out.exit_code == 1, out.output
    assert "does not stand: the stop was lifted" in out.output and "has exited" not in out.output


@FLOCK
def test_a_command_waiting_on_a_live_engine_is_named_after_its_exit(tmp_path):
    """The engine was SEEN holding the lock: the coming command starts an engine "now that this one
    has exited" — the branch the first two passes left undriven."""
    rd = _run_dir(tmp_path, in_flight=True)
    _command_record(rd, event_type="budget_extend", policy="ensure_running")
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(rd, release, held), daemon=True)
    holder.start()
    assert held.wait(5)
    threading.Timer(1.0, release.set).start()
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    holder.join(5)
    assert out.exit_code == 1, out.output
    assert "will start an engine now that this one has exited" in out.output
