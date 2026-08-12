"""A spent control intent must not hold the run's control plane hostage forever.

Reproduced end to end on `rubertlite-dr-unified-v2`, 2026-08-11. An operator paused; the `pause`
landed within a second; the command's postcondition is `paused_and_stopped`, which ALSO requires the
engine process to exit — impossible while a multi-hour evaluation is in flight — so the record went
`timed_out`. The operator resumed and the run continued for another day. From then on
`reject_if_active` refused EVERY later control with `command_retry_required`, because that spent
pause was still "an unresolved intent with an intact durable event". `/retry` could not help either:
it re-drives the same event, which the later `resume` had already consumed (`accepted` -> `executing`
-> `timed_out`, no new `pause` appended, run still in phase `search`).

`_find_intent` proves the marked event is in the LOG. `_intent_spent` is the missing half: is its
effect still in force? A control plane that fails closed must still leave one path forward.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from looplab.events.eventstore import EventStore
from looplab.events.types import EV_PAUSE, EV_RESUME, EV_RUN_FINISHED, EV_RUN_STARTED


def _service(tmp_path: Path, *, alive: bool = False):
    """`alive` is load-bearing: `paused_and_stopped` is satisfied the moment the engine process is
    gone, so with a dead engine every paused record reconciles straight to `succeeded` and there is
    nothing left to block on. The defect only exists while the engine is STILL RUNNING, which is
    exactly the shape a long evaluation produces."""
    from looplab.serve.run_commands import RunCommandService
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    (root / "r1").mkdir(parents=True)
    srv = make_app(root).state.looplab
    srv.commands = RunCommandService(srv, engine_alive=lambda _rd: alive)
    return srv.commands, root / "r1"


PAUSE_ID = "cmd_" + "a1b2" * 8      # `_COMMAND_ID_RE`: cmd_ + 32 hex, or the observation
LIVE_ID = "cmd_" + "b2c3" * 8       # does not index the intent at all and the test goes vacuous.
HINT_ID = "cmd_" + "c3d4" * 8


def _timed_out_pause(rd: Path, commands, *, command_id=PAUSE_ID) -> Path:
    """A run that was paused under a command, then resumed — the exact shipped shape."""
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    pause = store.append(EV_PAUSE, {"_command_id": command_id})
    store.append(EV_RESUME, {})
    record = {
        "id": command_id, "event_type": EV_PAUSE, "event_seq": pause.seq,
        "baseline_seq": pause.seq - 1, "data": {},
        "postcondition": "paused_and_stopped", "status": "timed_out",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "error": {"code": "postcondition_timeout", "retryable": True,
                  "message": "paused_and_stopped was not observed in time"},
    }
    path = rd / ".commands" / f"{command_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def test_a_resumed_pause_no_longer_blocks_every_later_control(tmp_path):
    commands, rd = _service(tmp_path, alive=True)
    record = json.loads(_timed_out_pause(rd, commands).read_text())
    # NON-VACUOUS: the durable intent really is intact and retryable — the only reason this record
    # stopped blocking is that its EFFECT is gone. Without this the test would pass just as happily
    # against a record the observation never indexed.
    assert commands._find_intent(rd, PAUSE_ID, record) is not None
    assert commands._intent_spent(rd, record) is True

    # The bug: this raised 409 command_retry_required forever, on every control the operator tried.
    commands.reject_if_active(rd, "pause the run")

    _path, unresolved = commands._unresolved_terminal_record(rd)
    assert unresolved is None, "a pause whose effect was lifted is settled history"


def test_a_pause_still_in_force_is_still_an_unresolved_intent(tmp_path):
    """The blocking rule is not being weakened: while the run IS paused, that timed-out pause may
    still need driving and a legacy mutation must not overtake it."""
    commands, rd = _service(tmp_path, alive=True)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    pause = store.append(EV_PAUSE, {"_command_id": LIVE_ID})
    record = {
        "id": LIVE_ID, "event_type": EV_PAUSE, "event_seq": pause.seq,
        "baseline_seq": pause.seq - 1, "data": {},
        "postcondition": "paused_and_stopped", "status": "timed_out",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "error": {"code": "postcondition_timeout", "retryable": True, "message": "…"},
    }
    (rd / ".commands").mkdir(parents=True, exist_ok=True)
    (rd / ".commands" / f"{LIVE_ID}.json").write_text(json.dumps(record))

    with pytest.raises(HTTPException) as caught:
        commands.reject_if_active(rd, "pause the run")
    assert caught.value.detail["code"] == "command_retry_required"


def test_an_additive_intent_is_not_spent_merely_because_the_run_moved_on(tmp_path):
    """A hint/inject/budget-extend may still need driving after the run advances — that is exactly
    what the unresolved-record boundary exists for. Only a folded, reversible FLAG can be spent."""
    from looplab.events.types import EV_HINT

    commands, rd = _service(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    hint = store.append(EV_HINT, {"_command_id": HINT_ID, "text": "try x"})
    record = {
        "id": HINT_ID, "event_type": EV_HINT, "event_seq": hint.seq,
        "baseline_seq": hint.seq - 1, "data": {"text": "try x"},
        "postcondition": "folded_intent", "status": "failed",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "error": {"code": "boom", "retryable": True, "message": "…"},
    }
    (rd / ".commands").mkdir(parents=True, exist_ok=True)
    (rd / ".commands" / f"{HINT_ID}.json").write_text(json.dumps(record))

    # The rule itself. (`reject_if_active` is not asserted here: a `folded_intent` postcondition is
    # already satisfied by the intent being in the log, so this record reconciles to `succeeded` and
    # never reaches the blocking branch — which is correct, and a different property.)
    assert commands._intent_spent(rd, record) is False


def test_a_finished_run_spends_every_pending_intent(tmp_path):
    """Once a run is finished nothing can drive a control, so holding new ones behind an old failure
    only stops the operator reading or reopening it."""
    from looplab.events.types import EV_HINT

    commands, rd = _service(tmp_path)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    hint = store.append(EV_HINT, {"_command_id": HINT_ID, "text": "try x"})
    store.append(EV_RUN_FINISHED, {"reason": "done"})
    record = {
        "id": HINT_ID, "event_type": EV_HINT, "event_seq": hint.seq,
        "baseline_seq": hint.seq - 1, "data": {"text": "try x"},
        "postcondition": "folded_intent", "status": "failed",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "no_spawn",
        "error": {"code": "boom", "retryable": True, "message": "…"},
    }
    (rd / ".commands").mkdir(parents=True, exist_ok=True)
    (rd / ".commands" / f"{HINT_ID}.json").write_text(json.dumps(record))

    assert commands._intent_spent(rd, record) is True


def test_an_unreadable_log_does_not_silently_unblock_controls(tmp_path):
    """Fail closed: if the fold cannot be read we cannot PROVE the intent is spent, and guessing
    would reintroduce the overtake this boundary exists to prevent."""
    from looplab.events.types import EV_HINT

    commands, rd = _service(tmp_path)
    (rd / "events.jsonl").write_bytes(b"\x00\xffnot json\n")
    record = {"id": "x", "event_type": EV_HINT, "event_seq": 1, "data": {}}
    assert commands._run_finished(rd) is False
    assert commands._intent_spent(rd, record) is False


def test_a_landed_pause_waiting_on_a_live_engine_does_not_time_out_as_a_failure(tmp_path):
    """`paused_and_stopped` is two effects: the run pauses (immediate) and the engine process
    releases its lock (only when the in-flight evaluation settles — a pause must not abandon a
    running node). A GPU training stage runs for HOURS, so the absolute ~20-minute observation bound
    expired while everything worked correctly and the operator was told the pause "was not observed
    in time" about a pause that had landed one second after they pressed the button.

    The extension is gated on all three facts, so it bounds the WRONG ANSWER rather than the wait:
    a dead or stalled driver still terminalizes on the very next liveness probe.
    """
    commands, rd = _service(tmp_path, alive=True)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    store.append(EV_PAUSE, {"_command_id": PAUSE_ID})
    observation = commands._observe(rd)

    assert commands._pause_is_folded(rd, observation) is True

    # …and the two ways it must NOT extend: no pause folded, and an unreadable log.
    other = tmp_path / "runs" / "r2"
    other.mkdir(parents=True)
    other_store = EventStore(other / "events.jsonl")
    other_store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    assert commands._pause_is_folded(other) is False

    broken = tmp_path / "runs" / "r3"
    broken.mkdir(parents=True)
    (broken / "events.jsonl").write_bytes(b"\x00\xffnot json\n")
    assert commands._pause_is_folded(broken) is False


def test_a_landed_pause_extends_its_observation_a_bounded_number_of_times(tmp_path):
    """The other half of the extension: it has to END.

    The sibling above proves the ingredient — a folded pause on a live driver earns more time. It
    does not prove the wait terminates, and the first version of that extension re-armed the absolute
    deadline on EVERY pass, so the loop's own exit condition compared against a value it had just
    pushed forward and the record stayed `executing` for the whole evaluation. `_active_record`
    returns exactly that record and refuses `POST /commands`, `/control` and `/resume` with 409, and
    there is no cancel endpoint — so an operator who paused a multi-hour eval lost resume, abort,
    hint and inject until it finished.

    Drive the grant itself past its budget rather than asserting the predicate: ask far more times
    than the cap allows and require that the grants stop.
    """
    from looplab.serve.run_commands import PAUSE_OBSERVATION_MAX_EXTENSIONS

    commands, rd = _service(tmp_path, alive=True)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    store.append(EV_PAUSE, {"_command_id": PAUSE_ID})
    observation = commands._observe(rd)

    record = {"postcondition": "paused_and_stopped", "absolute_deadline_at": 0.0}
    granted = sum(
        bool(commands._extend_landed_pause_observation(record, rd, observation, True, float(turn)))
        for turn in range(PAUSE_OBSERVATION_MAX_EXTENSIONS + 25))
    assert granted == PAUSE_OBSERVATION_MAX_EXTENSIONS, (
        "the observation window must be long but FINITE — an unbounded one freezes every other "
        "control command behind 409 command_in_progress")
    assert record["pause_observation_extensions"] == PAUSE_OBSERVATION_MAX_EXTENSIONS

    # …and the gates that must refuse it outright, whatever the budget says.
    assert commands._extend_landed_pause_observation(
        {"postcondition": "paused_and_stopped"}, rd, observation, False, 0.0) is False, "dead driver"
    assert commands._extend_landed_pause_observation(
        {"postcondition": "stopped"}, rd, observation, True, 0.0) is False, "other postcondition"
