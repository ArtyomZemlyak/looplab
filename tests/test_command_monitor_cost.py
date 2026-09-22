"""What the command monitor pays per tick while it waits (review 2026-09-22, SRV1-05).

`RunCommandService._monitor` polls every `poll_interval` (50 ms in production) for as long as a
command is unresolved — for a Finalize, up to the ~20-minute absolute deadline while the engine
writes its wrap-up. Every tick asked `_postcondition`, and every state-reading postcondition calls
`CommandObservation.state()`: the fold of the whole log (memoized per revision) plus a DEEP COPY of
the folded `RunState` (not memoized — it is a defensive copy). Measured by the reviewer on an
874-event log: 9.9 ms per tick on an UNCHANGED log, 39.8 ms after an append; i.e. up to ~20 % of a
core for twenty minutes, per finalizing run, to learn nothing new.

The verdict is a pure function of the observed log revision and the engine liveness (the monitor
never changes the record fields `_postcondition` reads), so it is asked again only when one of
those moved — and a state-reading ask is rate-limited to `engine_proc._PENDING_RECHECK_S`, the
interval the tail waiter already uses for the same fold. The deadline exit and every re-spawn
branch still take their own fresh, serialized look, so a skipped tick can delay an observation but
never decide one.
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("fastapi")

from looplab.core.models import RunState  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve import command_observation, engine_proc  # noqa: E402
from looplab.serve import run_commands as rc  # noqa: E402
from looplab.serve.run_commands import RunCommandService  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


class _FinalizingEngine:
    """An engine that holds `engine.lock` until told to release it: a Finalize still wrapping up."""

    def __init__(self):
        self.alive = True
        self.spawns = []

    def is_alive(self, _rd):
        return self.alive

    def is_process_alive(self, _pid):
        return True

    def get_process_identity(self, _pid):
        return "child-generation"

    def spawn(self, args, **_kwargs):
        self.spawns.append(list(args))
        return 4242


def _finalize_under_a_live_engine(tmp_path, monkeypatch, *, observation: float):
    """A durable Finalize command, admitted but not yet executed, over an engine that is alive."""
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    (rd / "task.snapshot.json").write_text(
        '{"kind":"quadratic","goal":"g","direction":"min"}', encoding="utf-8")
    srv = make_app(tmp_path).state.looplab
    engine = _FinalizingEngine()
    srv.commands = commands = RunCommandService(
        srv, engine_alive=engine.is_alive, spawn_engine=engine.spawn,
        process_alive=engine.is_process_alive, process_identity=engine.get_process_identity,
        startup_timeout=0.05, command_timeout=0.2, poll_interval=0.01,
        max_observation_timeout=observation)
    queued = {}
    # Drive the worker on THIS thread, so every tick is counted and the test owns its duration.
    monkeypatch.setattr(commands, "_start_worker",
                        lambda rd_, path, record: queued.update(args=(rd_, path, record)))
    accepted = commands.submit(rd, "finalize-once", "run_abort", {"reason": "finalized"},
                               expected_generation=commands.run_generation(rd))
    assert accepted["status"] == "accepted" and "args" in queued
    return commands, rd, engine, queued["args"]


class _Meter:
    """Counts log folds and `RunState` deep copies, and brackets the monitor LOOP with snapshots:
    the first tick's heartbeat opens the window, the deadline exit (`_terminalize_expired`, which
    takes its own fresh look on purpose) closes it."""

    def __init__(self, monkeypatch, commands, *, on_tick=None):
        self.folds = self.copies = self.ticks = 0
        self.opened = self.closed = None
        real_fold = command_observation.fold

        def counting_fold(events):
            self.folds += 1
            return real_fold(events)

        real_copy = RunState.model_copy

        def counting_copy(state, *args, **kwargs):
            self.copies += 1
            return real_copy(state, *args, **kwargs)

        real_heartbeat = commands._heartbeat_execution

        def heartbeat(rd, command_id):
            if self.opened is None:
                self.opened = (self.folds, self.copies, time.monotonic())
            self.ticks += 1
            if on_tick is not None:
                on_tick(self.ticks)
            return real_heartbeat(rd, command_id)

        real_expire = commands._terminalize_expired

        def expire(*args, **kwargs):
            self.closed = (self.folds, self.copies, time.monotonic())
            return real_expire(*args, **kwargs)

        monkeypatch.setattr(command_observation, "fold", counting_fold)
        monkeypatch.setattr(RunState, "model_copy", counting_copy)
        monkeypatch.setattr(commands, "_heartbeat_execution", heartbeat)
        monkeypatch.setattr(commands, "_terminalize_expired", expire)

    def loop(self) -> tuple[int, int, float]:
        (folds0, copies0, t0), (folds1, copies1, t1) = self.opened, self.closed
        return folds1 - folds0, copies1 - copies0, t1 - t0


def test_an_unchanged_log_costs_the_finalize_monitor_no_fold_and_at_most_one_copy(
        tmp_path, monkeypatch):
    commands, rd, engine, (run_dir, path, record) = _finalize_under_a_live_engine(
        tmp_path, monkeypatch, observation=0.8)
    meter = _Meter(monkeypatch, commands)

    commands._execute(run_dir, path, record, claimed=False)

    folds, copies, _elapsed = meter.loop()
    assert meter.ticks >= 10, "the premise: the monitor polled an unchanged log many times"
    assert folds == 0, f"{folds} folds over {meter.ticks} ticks of an unchanged log"
    assert copies <= 1, f"{copies} RunState deep copies over {meter.ticks} ticks of an unchanged log"
    # ...and it is not vacuous: the command really was watched to its deadline, and no driver was
    # started over the live one.
    assert commands._load(path)["status"] == "timed_out"
    assert engine.spawns == []


def test_a_growing_log_is_re_read_at_most_once_per_recheck_interval(tmp_path, monkeypatch):
    """The finalization TAIL: the engine keeps appending, so the revision moves on every tick. The
    state-reading ask is rate-limited to the tail waiter's own interval instead of refolding the
    whole log at the poll rate."""
    commands, rd, _engine, (run_dir, path, record) = _finalize_under_a_live_engine(
        tmp_path, monkeypatch, observation=1.2)
    store = EventStore(rd / "events.jsonl")
    meter = _Meter(monkeypatch, commands, on_tick=lambda n: store.append(
        "setup_step", {"step": f"wrap-up {n}"}))

    commands._execute(run_dir, path, record, claimed=False)

    folds, copies, elapsed = meter.loop()
    budget = int(elapsed / engine_proc._PENDING_RECHECK_S) + 2
    assert meter.ticks >= 20, "the premise: the log grew on many ticks"
    assert folds <= budget and copies <= budget, (
        f"{folds} folds / {copies} copies over {meter.ticks} ticks of a growing log in "
        f"{elapsed:.2f}s; the recheck interval allows {budget}")


def test_a_finalize_that_completes_mid_wait_is_still_observed_by_the_loop(tmp_path, monkeypatch):
    """The other direction: skipping unchanged ticks must not skip the CHANGE. The engine finishes
    and releases its lock while the monitor waits, and the command succeeds long before its
    deadline — through the loop, not through the deadline's final look."""
    commands, rd, engine, (run_dir, path, record) = _finalize_under_a_live_engine(
        tmp_path, monkeypatch, observation=30.0)
    finished_at = {}

    def finish_after_a_few_ticks(tick):
        if tick == 5:
            EventStore(rd / "events.jsonl").append("run_finished", {"reason": "finalized"})
            engine.alive = False
            finished_at["t"] = time.monotonic()

    meter = _Meter(monkeypatch, commands, on_tick=finish_after_a_few_ticks)
    worker = threading.Thread(target=commands._execute, args=(run_dir, path, record),
                              kwargs={"claimed": False}, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), "the monitor never noticed the finalize complete"
    assert commands._load(path)["status"] == "succeeded"
    assert meter.closed is None, "succeeded inside the loop, not at the deadline exit"
    assert meter.ticks >= 5 and "t" in finished_at


def test_a_released_lock_alone_is_a_change_worth_asking_about(tmp_path, monkeypatch):
    """The engine's finish is already in the log, and the ONLY thing that moves afterwards is the
    lock: the engine releases it after its last write (read-model, trace, tree), appending nothing.
    Liveness is half of the gate's key for exactly this — a revision-only key would never ask again
    and the command would wait out its whole deadline for the final look."""
    commands, rd, engine, (run_dir, path, record) = _finalize_under_a_live_engine(
        tmp_path, monkeypatch, observation=30.0)

    def finish_then_release(tick):
        if tick == 3:
            EventStore(rd / "events.jsonl").append("run_finished", {"reason": "finalized"})
        elif tick == 10:
            engine.alive = False                  # the lock goes; the log does not move

    meter = _Meter(monkeypatch, commands, on_tick=finish_then_release)
    worker = threading.Thread(target=commands._execute, args=(run_dir, path, record),
                              kwargs={"claimed": False}, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), "a released lock with an unchanged log was never re-asked"
    assert commands._load(path)["status"] == "succeeded"
    assert meter.closed is None and meter.ticks >= 10


def test_the_gate_asks_again_only_when_what_the_verdict_reads_has_moved():
    """The rule, stated. `_PostconditionGate` decides whether a monitor tick may skip the ask."""
    gate = rc._PostconditionGate({"postcondition": "finished_and_stopped"}, min_interval=0.25)
    assert gate.reads_state
    assert gate.due("rev-1", True, now=0.0)
    gate.asked("rev-1", True, now=0.0)
    assert not gate.due("rev-1", True, now=100.0), "nothing moved: the answer cannot have changed"
    assert not gate.due("rev-2", True, now=0.1), "the log moved, but a state read is rate-limited"
    assert gate.due("rev-2", True, now=0.25)
    assert gate.due("rev-1", False, now=0.25), "the engine released its lock"
    assert gate.due("rev-1", None, now=0.25), "liveness became unknown"

    ack = rc._PostconditionGate({"postcondition": "engine_ack"}, min_interval=0.25)
    assert not ack.reads_state
    ack.asked("rev-1", True, now=0.0)
    assert not ack.due("rev-1", True, now=0.01)
    assert ack.due("rev-2", True, now=0.01), "an index lookup is cheap: never rate-limited"

    # An ATTACHED record reads the folded state whatever its kind (`_attached_finalize_intact`).
    attached = rc._PostconditionGate({"postcondition": "engine_ack", "attached": True}, 0.25)
    assert attached.reads_state
