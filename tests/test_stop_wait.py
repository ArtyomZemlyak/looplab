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

import errno
import itertools
import json
import os
import subprocess
import sys
import textwrap
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
                    name: str = "0" * 32, age_s: float = 0.0, error=None, **fields) -> Path:
    """A durable command record the way `serve/run_commands.py` writes one (the keys this reads),
    last touched `age_s` ago; `fields` add or override keys (`absolute_deadline_at`, `event_seq`,
    `spawned_by_command`, …)."""
    directory = rd / ".commands"
    directory.mkdir(exist_ok=True)
    path = directory / f"cmd_{name}.json"
    record = {"id": f"cmd_{name}", "status": status, "event_type": event_type,
              "engine_policy": policy, "postcondition": "engine_ack",
              "updated_at": time.time() - age_s}
    if error is not None:
        record["error"] = error
    record.update(fields)
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
    from looplab.cli.run_cmds import server_commands_restarting
    from looplab.serve.protocol import command_ack_index

    rd = _run_dir(tmp_path, in_flight=False)
    store = EventStore(rd / "events.jsonl")
    path = _command_record(rd, event_type="node_reset", policy="ensure_running", age_s=3600,
                           name="a" * 32)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["event_seq"] = 7
    path.write_text(json.dumps(record), encoding="utf-8")
    assert server_commands_restarting(rd)["stale"], "unacked, it is a stale note"
    store.append("command_ack", {"command_id": f"cmd_{'a' * 32}", "event_seq": 7})
    acks = command_ack_index(store.read_all())
    assert acks == {f"cmd_{'a' * 32}": (7,)}
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


_UNCERTAIN = f"`fork` (cmd_{'0' * 32}, engine start uncertain — its child may still be starting)"
_NOTHING = {"coming": [], "stale": [], "uncertain": [], "unreadable": []}


@pytest.mark.parametrize("acked", [False, True], ids=["unacked", "acked"])
def test_a_settled_uncertain_start_is_counted_whatever_its_deadline_or_ack_says(tmp_path, acked):
    """A record that SETTLED `ENGINE_START_UNCERTAIN` is terminal and never re-driven, so neither
    rule for what a re-drive would do — its ack, its deadline — says anything about it: the
    uncertainty is its CHILD's, which may still be importing. `_spawn_under_claim` settles one on a
    spawn it could not confirm, before `spawned_by_command` is ever set, and by the time a wait reads
    it its deadline has usually passed. `_command_record` never set a deadline, so no test saw the two
    rules reach such a record (critic 2026-09-26: deleting the guard survived the suite)."""
    from looplab.cli.run_cmds import server_commands_restarting
    from looplab.serve.protocol import command_ack_index

    rd = _run_dir(tmp_path, in_flight=False)
    store = EventStore(rd / "events.jsonl")
    _command_record(rd, event_type="fork", policy="ensure_running", status="failed",
                    error={"code": "engine_start_uncertain"}, event_seq=2,
                    absolute_deadline_at=time.time() - 60)
    if acked:
        store.append("command_ack", {"command_id": f"cmd_{'0' * 32}", "event_seq": 2})
    found = server_commands_restarting(rd, acked=command_ack_index(store.read_all()))
    assert found == {**_NOTHING, "coming": [_UNCERTAIN]}, found


def test_an_expired_command_that_started_a_child_is_counted_as_the_server_settles_it(tmp_path):
    """Critic 2026-09-26, driven: a record past its deadline whose worker had ALREADY spawned a
    child (`spawned_by_command` set, `spawn_claim_released` not) was skipped as "past its deadline",
    so before any GET the stop read as standing (exit 0) — while the server's own GET of that record
    settles it `ENGINE_START_UNCERTAIN` ("the detached engine has not exposed engine.lock"), and
    after that GET this check counted it as coming. It is counted as that settled record is: coming
    while fresh, the uncertain note once old. A child the worker SAW take the lock is settled
    `timed_out` with nothing left to start, and stays out."""
    from looplab.cli.run_cmds import server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    past = time.time() - 60
    _command_record(rd, event_type="fork", policy="ensure_running", event_seq=2,
                    spawned_by_command=True, engine_pid=4242, absolute_deadline_at=past)
    assert server_commands_restarting(rd) == {**_NOTHING, "coming": [_UNCERTAIN]}
    assert (server_commands_restarting(rd, now=time.time() + 3600)
            == {**_NOTHING, "uncertain": [_UNCERTAIN]})
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 1 and "engine start uncertain" in out.output, out.output

    _command_record(rd, event_type="fork", policy="ensure_running", event_seq=2,
                    spawned_by_command=True, spawn_claim_released=True, absolute_deadline_at=past)
    assert server_commands_restarting(rd) == _NOTHING


def test_the_server_settles_that_record_the_way_the_wait_counted_it(tmp_path):
    """The same record through the REAL service, so the rule above is pinned against the writer:
    a worker that appended its intent and spawned a child dies; past the deadline a GET settles the
    record `timed_out` / `engine_start_uncertain` and starts nothing — and the wait counts it as
    coming before that GET and after it alike."""
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
    spawned = []
    commands = srv.commands = RunCommandService(
        srv, engine_alive=lambda _rd: False, spawn_engine=lambda *a, **k: spawned.append(a) or 4242,
        process_alive=lambda _pid: True, process_identity=lambda _pid: "child",
        startup_timeout=0.05, command_timeout=5.0, poll_interval=0.01,
        max_observation_timeout=10.0)
    client = TestClient(application)
    commands._start_worker = lambda *_args, **_kwargs: None
    record = post_command(client, "budget_extend", {"add_nodes": 2}, run_id="run").json()
    path = commands._path(rd, record["id"])
    # What the dead worker left: the marked intent appended, a child spawned under its lease.
    row = commands._load(path)
    intent = EventStore(rd / "events.jsonl").append(
        "budget_extend", {**row["data"], "_command_id": row["id"]})
    commands._record_spawn_claim(rd, row["id"], 4242)
    row.update(status="executing", event_seq=intent.seq, spawned_by_command=True, engine_pid=4242,
               absolute_deadline_at=time.time() - 60, updated_at=time.time())
    commands._save(path, row)
    label = f"`budget_extend` ({row['id']}, engine start uncertain — its child may still be starting)"
    assert server_commands_restarting(rd)["coming"] == [label], "before the GET"

    commands._start_worker = lambda rd_, path_, record_: commands._execute(
        rd_, path_, record_, claimed=False)                     # the GET's re-drive, inline
    settled = client.get(f"/api/runs/run/commands/{row['id']}").json()
    assert settled["status"] == "timed_out", settled
    assert settled["error"]["code"] == "engine_start_uncertain", settled
    assert spawned == [], "an expired record starts no second engine"
    assert server_commands_restarting(rd)["coming"] == [label], "after the GET"


def test_only_an_engine_ack_postcondition_is_settled_by_an_ack(tmp_path):
    """The ack skip is the server's `engine_ack` postcondition, not a rule for every record: a
    `restart` waits for `restart_served`, and an acknowledgement filed under its marker and seq —
    however it got there — settles nothing, so its worker still starts the replacement engine."""
    from looplab.cli.run_cmds import server_commands_restarting
    from looplab.serve.protocol import command_ack_index

    rd = _run_dir(tmp_path, in_flight=False)
    store = EventStore(rd / "events.jsonl")
    _command_record(rd, event_type="restart", policy="restart_after_exit", name="4" * 32,
                    postcondition="restart_served", event_seq=2)
    _command_record(rd, event_type="fork", policy="ensure_running", name="6" * 32, event_seq=3)
    store.append("command_ack", {"command_id": f"cmd_{'4' * 32}", "event_seq": 2})
    store.append("command_ack", {"command_id": f"cmd_{'6' * 32}", "event_seq": 3})
    found = server_commands_restarting(rd, acked=command_ack_index(store.read_all()))
    assert found == {**_NOTHING, "coming": [f"`restart` (cmd_{'4' * 32}, executing)"]}, found


# Every row: (what differs on the record, the marker the intent is stamped with, the ack rows'
# `(command_id, event_seq)` — `...` leaves `event_seq` out — and whether the engine_ack holds). The
# intent is always seq 1, so `True == 1` and `1.0 == 1` are the equalities the server keeps.
_ID = "cmd_" + "d" * 32
_ACK_ROWS = [
    ("an int ack", {}, _ID, [(_ID, 1)], True),
    ("a float ack (the critic's case)", {}, _ID, [(_ID, 1.0)], True),
    ("a bool ack (legacy equality)", {}, _ID, [(_ID, True)], True),
    ("a float seq on the record", {"event_seq": 1.0}, _ID, [(_ID, 1)], True),
    ("a string ack", {}, _ID, [(_ID, "1")], False),
    ("an ack of another intent", {}, _ID, [(_ID, 2)], False),
    ("an ack with no seq", {}, _ID, [(_ID, ...)], False),
    ("a re-issued marker", {"intent_marker": f"{_ID}.r1"}, f"{_ID}.r1", [(f"{_ID}.r1", 1)], True),
    ("a re-issued marker acked under the id", {"intent_marker": f"{_ID}.r1"}, f"{_ID}.r1",
     [(_ID, 1)], False),
    ("an empty marker", {"intent_marker": ""}, _ID, [(_ID, 1)], True),
    ("a non-string marker", {"intent_marker": 5}, _ID, [(_ID, 1)], True),
    ("a non-string marker acked under it", {"intent_marker": 5}, _ID, [("5", 1)], False),
    ("a record with no id", {"id": None}, "", [("", 1)], True),
    ("a record with no id, acked under its file name", {"id": None}, "", [(_ID, 1)], False),
    ("an unhashable seq on the record", {"event_seq": [1]}, _ID, [(_ID, 1)], False),
]


@pytest.mark.parametrize("fields,marker,acks,expected", [row[1:] for row in _ACK_ROWS],
                         ids=[row[0] for row in _ACK_ROWS])
def test_the_wait_reads_an_acknowledgement_by_the_servers_own_rule(tmp_path, fields, marker, acks,
                                                                   expected):
    """ONE RULE, driven through both readers over the same rows (critic 2026-09-26, driven): the
    server's `_postcondition` over its incremental index, and `server_commands_restarting` over the
    log it read. The wait kept a copy — integer seqs only, the marker `intent_marker or id or <file
    name>` — so an ack row carrying `event_seq: 3.0` settled the command `succeeded` on a GET while
    the wait said "will start an engine" (exit 1), and a hand-edited list seq raised `TypeError` after
    the pause was appended. Both now ask `serve/protocol.py::engine_ack_observed`; the rows pin what
    it answers, so a change to the shared rule is red here too."""
    pytest.importorskip("fastapi")
    from looplab.cli.run_cmds import server_commands_restarting
    from looplab.serve.protocol import command_ack_index
    from looplab.serve.run_commands import RunCommandService
    from looplab.serve.server import make_app

    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    intent = store.append("budget_extend", {"add_nodes": 1, "_command_id": marker})
    assert intent.seq == 1
    for command_id, seq in acks:
        store.append("command_ack", ({"command_id": command_id} if seq is ... else
                                     {"command_id": command_id, "event_seq": seq}))
    record = {"id": _ID, "status": "executing", "event_type": "budget_extend",
              "engine_policy": "ensure_running", "postcondition": "engine_ack",
              "data": {"add_nodes": 1}, "event_seq": 1, "updated_at": time.time(), **fields}
    record = {key: value for key, value in record.items() if value is not None}
    (rd / ".commands").mkdir()
    (rd / ".commands" / f"{_ID}.json").write_text(json.dumps(record), encoding="utf-8")

    srv = make_app(tmp_path).state.looplab
    service = RunCommandService(srv, engine_alive=lambda _rd: False)
    server_says = service._postcondition(rd, dict(record), service._observe(rd))
    found = server_commands_restarting(rd, acked=command_ack_index(store.read_all()))
    wait_says = found["coming"] == []
    assert (server_says, wait_says) == (expected, expected), found


def test_a_record_past_the_bound_is_counted_as_coming_not_as_unreadable(tmp_path, monkeypatch):
    """A file past `_COMMAND_RECORD_MAX_BYTES` is one THIS check declined to read, not one the
    server's reader rejects: the wait cannot tell what it would start, so it is coming (exit 1),
    never an "unreadable" note that keeps the exit at 0."""
    from looplab.cli import run_cmds

    rd = _run_dir(tmp_path, in_flight=False)
    path = _command_record(rd, event_type="fork", policy="ensure_running")
    monkeypatch.setattr(run_cmds, "_COMMAND_RECORD_MAX_BYTES", path.stat().st_size - 1)
    assert run_cmds.server_commands_restarting(rd) == {
        **_NOTHING, "coming": [f"cmd_{'0' * 32} (too large for this check to read)"]}
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 1 and "too large for this check to read" in out.output, out.output


def test_a_commands_directory_that_cannot_be_listed_is_counted_as_coming(tmp_path, monkeypatch):
    """Nothing in `.commands/` could be read, so nothing in it can be ruled out: coming, never an
    "unreadable" note (those are records the SERVER's reader rejects, which start nothing)."""
    from looplab.cli import run_cmds

    rd = _run_dir(tmp_path, in_flight=False)
    _command_record(rd, event_type="hint", policy="no_spawn")
    real_glob = Path.glob

    def unlistable(self, pattern):
        if self.name == ".commands":
            raise OSError(errno.EIO, "Input/output error", str(self))
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", unlistable)
    assert run_cmds.server_commands_restarting(rd) == {
        **_NOTHING, "coming": [f"{rd / '.commands'} (cannot be listed)"]}


def test_a_symlinked_record_is_a_note_not_an_engine_start(tmp_path):
    """The server's own reader refuses a symlinked record (a GET of it answers 409/503 and no recovery
    follows it), so it starts nothing even when the file it points at reads as a live command: it is
    an "unreadable" note with its quarantine remedy, and the stop stands (exit 0)."""
    from looplab.cli.run_cmds import server_commands_restarting

    rd = _run_dir(tmp_path, in_flight=False)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({
        "id": f"cmd_{'9' * 32}", "status": "executing", "event_type": "fork",
        "engine_policy": "ensure_running", "postcondition": "engine_ack",
        "updated_at": time.time()}), encoding="utf-8")
    (rd / ".commands").mkdir()
    try:
        (rd / ".commands" / f"cmd_{'9' * 32}.json").symlink_to(elsewhere)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    assert server_commands_restarting(rd) == {**_NOTHING, "unreadable": [f"cmd_{'9' * 32}"]}
    out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
    assert out.exit_code == 0 and "resolve-activity-claims" in out.output, out.output


def test_the_command_reader_needs_no_fastapi(tmp_path):
    """`looplab stop --wait` reads `.commands/` on a box without the `[ui]` extra, so every rule it
    asks of a record — `deadline_passed`, `engine_ack_observed`, the policy and status words — comes
    from `serve/protocol.py`, which must never reach FastAPI. Driven in a child whose `fastapi` and
    `starlette` imports are blocked, over the records whose rules moved: an acknowledged one, an
    expired one, and an expired one that spawned a child."""
    rd = _run_dir(tmp_path, in_flight=False)
    past = time.time() - 60
    _command_record(rd, event_type="fork", policy="ensure_running", name="1" * 32, event_seq=2)
    EventStore(rd / "events.jsonl").append("command_ack",
                                           {"command_id": f"cmd_{'1' * 32}", "event_seq": 2.0})
    _command_record(rd, event_type="fork", policy="ensure_running", name="2" * 32,
                    absolute_deadline_at=past)
    _command_record(rd, event_type="fork", policy="ensure_running", name="3" * 32, event_seq=2,
                    spawned_by_command=True, absolute_deadline_at=past)
    child = textwrap.dedent("""
        import json, sys
        sys.modules["fastapi"] = None
        sys.modules["starlette"] = None
        from pathlib import Path
        from typer.testing import CliRunner
        from looplab.cli import app
        from looplab.cli.run_cmds import server_commands_restarting
        from looplab.events.eventstore import EventStore
        from looplab.serve.protocol import command_ack_index
        rd = Path(sys.argv[1])
        acked = command_ack_index(EventStore(rd / "events.jsonl").read_all())
        found = server_commands_restarting(rd, acked=acked)
        out = CliRunner().invoke(app, ["stop", str(rd), "--wait"])
        print(json.dumps({"found": found, "exit": out.exit_code, "output": out.output}))
    """)
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(root)] + [p for p in [os.environ.get("PYTHONPATH")] if p])}
    ran = subprocess.run([sys.executable, "-c", child, str(rd)], cwd=root, env=env,
                         capture_output=True, text=True, timeout=180)
    assert ran.returncode == 0, ran.stderr
    result = json.loads(ran.stdout.strip().splitlines()[-1])
    label = f"`fork` (cmd_{'3' * 32}, engine start uncertain — its child may still be starting)"
    assert result["found"] == {**_NOTHING, "coming": [label]}, result
    assert result["exit"] == 1 and "does not stand" in result["output"], result


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
