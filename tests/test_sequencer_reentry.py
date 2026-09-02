"""The run command sequencer states that it cannot be re-entered, instead of inviting it.

The in-process guard was an `RLock` while the interprocess half is not re-entrant at all. A nested
`sequence()` on one thread therefore passed the RLock and then contended with ITS OWN first
descriptor — POSIX `flock` is per open-file-description and this opens a fresh one per call — so the
second acquire spun to the full acquire budget and answered
`503 "timed out waiting for the run command sequencer"`: a message describing contention with
another process, about a thread blocked on itself.

No production path nests today, which is exactly why the `RLock` was a decoy: it read as permission
for something the layer below cannot do, and the failure it produced named the wrong cause.

A plain `Lock` states the rule. Re-entry is a NAMED refusal rather than a deadlock against that
plain lock, which would have been a worse answer to the same mistake — the caller would hang until
the budget expired and get the same misleading 503.
"""
from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from looplab.events.eventstore import EventStore
from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import make_app


def _service(tmp_path):
    # The sequencer reaches `srv.root` for its lock directory, so it needs the real AppState the
    # server builds rather than a bare path.
    return RunCommandService(make_app(tmp_path).state.looplab)


def _run_dir(tmp_path, name="demo"):
    rd = tmp_path / name
    rd.mkdir(parents=True, exist_ok=True)
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": name, "task_id": "task", "goal": "g", "direction": "min"})
    return rd


def test_the_in_process_guard_is_not_re_entrant(tmp_path):
    """MUTATION: restore the `RLock` -> the nested call passes here and blocks on its own flock
    descriptor until the acquire budget expires, then reports cross-process contention."""
    service = _service(tmp_path)
    rd = _run_dir(tmp_path)

    with service.sequence(rd):
        with pytest.raises(RuntimeError, match="re-entered"):
            with service.sequence(rd):
                pass


def test_the_refusal_names_the_fix(tmp_path):
    """A refusal an operator or a developer cannot act on is a worse 503 with a different code."""
    service = _service(tmp_path)
    rd = _run_dir(tmp_path)

    with service.sequence(rd):
        with pytest.raises(RuntimeError) as info:
            with service.sequence(rd):
                pass

    assert "not re-entrant" in str(info.value)
    assert "Hoist" in str(info.value), "the refusal must say what to do about it"


def test_a_DIFFERENT_run_is_still_sequenceable_from_the_same_thread(tmp_path):
    """The guard is per RUN, not per thread. MUTATION: track threads rather than (thread, run) ->
    one thread can never sequence two runs, and every batch operation deadlocks."""
    service = _service(tmp_path)
    first, second = _run_dir(tmp_path, "one"), _run_dir(tmp_path, "two")

    with service.sequence(first):
        with service.sequence(second):
            pass


def test_the_lock_is_released_and_re_enterable_afterwards(tmp_path):
    """The bookkeeping must not leak: a run sequenced once must be sequenceable again."""
    service = _service(tmp_path)
    rd = _run_dir(tmp_path)

    for _ in range(3):
        with service.sequence(rd):
            pass

    with service.sequence(rd):
        pass


def test_the_marker_is_dropped_after_a_RAISE_inside_the_block(tmp_path):
    """The `finally` has to run on the exception path too, or one failed command makes the run
    permanently un-sequenceable by that thread.

    MUTATION: clear the marker only on the success path -> the second `with` below raises
    're-entered' about a block that already exited.
    """
    service = _service(tmp_path)
    rd = _run_dir(tmp_path)

    with pytest.raises(ValueError):
        with service.sequence(rd):
            raise ValueError("the body failed")

    with service.sequence(rd):
        pass


def test_another_thread_is_unaffected_by_this_threads_marker(tmp_path):
    """The refusal is about ONE thread re-entering. A second thread must still contend normally —
    that is the lock's actual job.

    MUTATION: key the marker by run alone -> a concurrent worker gets a spurious RuntimeError
    instead of waiting its turn.
    """
    service = _service(tmp_path)
    rd = _run_dir(tmp_path)
    outcome: list = []

    def _other():
        try:
            with service.sequence(rd):
                outcome.append("acquired")
        except RuntimeError as exc:            # the bug this pins
            outcome.append(f"refused: {exc}")
        except HTTPException:
            outcome.append("timed-out")        # acceptable: it waited and gave up

    with service.sequence(rd):
        worker = threading.Thread(target=_other, daemon=True)
        worker.start()
        worker.join(timeout=1.0)

    worker.join(timeout=5.0)
    assert not any(item.startswith("refused") for item in outcome), outcome
