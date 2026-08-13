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
from looplab.events.types import (
    EV_HINT, EV_PAUSE, EV_RESUME, EV_RUN_FINISHED, EV_RUN_STARTED)


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


def test_an_intent_still_awaiting_its_driver_is_still_an_unresolved_intent(tmp_path):
    """The blocking rule is NOT being weakened: an intent whose effect has not landed still holds
    the boundary, and a legacy mutation must not overtake it.

    This used to be driven with a timed-out `pause` on a still-paused run. That is no longer an
    unresolved intent at all — since 2026-08-13 the pause postcondition IS the fold, so such a record
    reconciles to `succeeded` and correctly stops blocking, which is the whole point of the fix. The
    property needs an intent whose effect genuinely has not arrived, so it moves to a `budget_extend`:
    additive, `engine_ack`, and still waiting for a driver that will eventually acknowledge it.
    """
    from looplab.events.types import EV_BUDGET_EXTEND

    commands, rd = _service(tmp_path, alive=True)
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    extend = store.append(EV_BUDGET_EXTEND, {"_command_id": LIVE_ID, "add_nodes": 2})
    record = {
        "id": LIVE_ID, "event_type": EV_BUDGET_EXTEND, "event_seq": extend.seq,
        "baseline_seq": extend.seq - 1, "data": {"add_nodes": 2},
        "postcondition": "engine_ack", "status": "timed_out",
        "created_at": 1.0, "updated_at": 2.0, "engine_policy": "ensure_running",
        "error": {"code": "postcondition_timeout", "retryable": True, "message": "…"},
    }
    (rd / ".commands").mkdir(parents=True, exist_ok=True)
    (rd / ".commands" / f"{LIVE_ID}.json").write_text(json.dumps(record))

    assert commands._intent_spent(rd, record) is False
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


def test_a_landed_pause_is_the_whole_postcondition_including_on_a_legacy_record(tmp_path):
    """`paused_and_stopped` was two effects: the run pauses (immediate) and the engine PROCESS
    releases its lock (only when the in-flight evaluation settles — a pause must not abandon a
    running node). A GPU training stage runs for HOURS, so on exactly the runs where pausing matters
    the command could only ever time out, and the operator was told the pause "was not observed in
    time" about one that had landed a second after they pressed the button.

    This used to be answered with a bounded number of deadline EXTENSIONS
    (`PAUSE_OBSERVATION_MAX_EXTENSIONS`), which made the wrong answer arrive later rather than not at
    all. Since 2026-08-13 the postcondition observes THE PAUSE — the effect the operator asked for —
    and the process half is reported beside it instead of gating it.

    Both spellings are read the same way, and that is the load-bearing half of this test: a durable
    record is never rewritten, so every run wedged under the old build still carries
    `paused_and_stopped` on disk. If only the new spelling were re-pointed, those runs would stay
    exactly as stuck as they are today.
    """
    commands, rd = _service(tmp_path, alive=True)   # alive: the process half is NOT satisfied
    store = EventStore(rd / "events.jsonl")
    store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    intent = store.append(EV_PAUSE, {"_command_id": PAUSE_ID})
    observation = commands._observe(rd)

    for postcondition in ("paused", "paused_and_stopped"):
        record = {"id": PAUSE_ID, "event_type": EV_PAUSE, "event_seq": intent.seq, "data": {},
                  "postcondition": postcondition, "status": "executing"}
        assert commands._postcondition(rd, record, observation) is True, postcondition

    # …and it is not vacuous: a run that is NOT paused does not satisfy either spelling.
    other = tmp_path / "runs" / "r2"
    other.mkdir(parents=True)
    other_store = EventStore(other / "events.jsonl")
    other_store.append(EV_RUN_STARTED, {"goal": "g", "direction": "min"})
    other_intent = other_store.append(EV_HINT, {"_command_id": PAUSE_ID, "text": "x"})
    for postcondition in ("paused", "paused_and_stopped"):
        record = {"id": PAUSE_ID, "event_type": EV_HINT, "event_seq": other_intent.seq,
                  "data": {"text": "x"}, "postcondition": postcondition, "status": "executing"}
        assert commands._postcondition(rd, record) is False, postcondition


def test_the_pause_observation_extension_machinery_is_gone(tmp_path):
    """NEGATIVE pin, deliberately over the TEXT (see CLAUDE.md's guard-test tiering).

    The extension existed only to stop a `paused_and_stopped` command timing out while the pause had
    landed and the engine was still working. Its own history is why it must not come back rather than
    quietly reappear: the first version re-armed the absolute deadline on every pass, the loop's only
    exit compared against that same value, and the record therefore stayed `executing` for the whole
    evaluation — which is what `_active_record` returns, and that refuses `POST /commands`,
    `POST /control` and `POST /resume` with 409, with no cancel endpoint. The postcondition no longer
    waits for the process at all, so a re-introduced extension would be a deadline slide with nothing
    to observe, on the one command that must never be slow.
    """
    from looplab.serve import run_commands

    source = Path(run_commands.__file__).read_text(encoding="utf-8")
    assert "PAUSE_OBSERVATION_MAX_EXTENSIONS = " not in source
    assert "def _extend_landed_pause_observation" not in source
    assert not hasattr(run_commands, "PAUSE_OBSERVATION_MAX_EXTENSIONS")
