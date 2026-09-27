"""A restart is not the Card's fault: its quarantine close leaves the Card speculatively electable.

A head whose `card_build_attempted` receipt belongs to a process that DIED is closed
`producer_failed` so the possibly-billed provider work is never silently bought twice
(`speculation.py::_serve_card_builds`). That word used to bar the Card from speculative election for
good, and the serial lane it was then sent to `await`s its whole build on the outer loop, where no
evaluation is admitted. MEASURED 2026-09-27 on MiniOneRec inf13: every restart closed its in-flight
Card this way (card-7 at seq 2547), and a node reset during the resulting serial build waited 41 min
on an idle GPU. Now the close is NAMED (`skipped_reason: unreconciled_after_restart`) and ONE such
close does not bar the Card; a second close of any kind still does, so a Card that keeps killing the
process cannot loop. The engine-level half (the real election re-elects it) is
`tests/test_card_speculation_engine.py::test_recovery_head_with_an_unreconciled_attempt_is_quarantined_not_reissued`.

PROVEN, NOT INFERRED (critic review of the first cut, same day). "No live producer, no result, a
receipt present" is also what THIS process's own attempt looks like once its producer released with
no result stored, and a name that lets a Card off its bar must not be handed to a give-up the Card
may own. The tag is now written only when the receipt's seq is at or below the tail the engine saw
when it STARTED (`speculation.py::_attempt_predates_this_process`); the last two tests below drive
the same receipt through both processes.
"""
from __future__ import annotations

import pytest

from looplab.engine.speculation import (
    CARD_BUILD_SKIP_REASONS, UNRECONCILED_AFTER_RESTART, CardSession, SpeculationMixin,
)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (EV_CARD_BUILD_ATTEMPTED, EV_CARD_BUILD_DONE,
                                  EV_CARD_BUILD_REQUESTED)
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _engine,
    _request,
    _start,
)


def _log(tmp_path, closes):
    """One request + close per entry of `closes` for card-a (a close is (skipped, reason|None)),
    and one plain producer give-up for card-b."""
    store = EventStore(tmp_path / "events.jsonl")
    for skipped, reason in closes:
        store.append(EV_CARD_BUILD_REQUESTED, {"card_id": "card-a", "generation": 0})
        row = {"card_id": "card-a", "generation": 0, "skipped": skipped}
        if reason is not None:
            row["skipped_reason"] = reason
        store.append(EV_CARD_BUILD_DONE, row)
    store.append(EV_CARD_BUILD_REQUESTED, {"card_id": "card-b", "generation": 0})
    store.append(EV_CARD_BUILD_DONE, {"card_id": "card-b", "generation": 0,
                                      "skipped": "producer_failed"})
    return fold(store.read_all())


def test_one_restart_close_does_not_bar_the_card(tmp_path):
    state = _log(tmp_path, [("producer_failed", UNRECONCILED_AFTER_RESTART)])
    # The coarse record every other reader keys on is unchanged: still a producer give-up, still
    # in the quality denominator.
    assert state.card_build_producer_failed == ["card-a", "card-b"]
    assert state.card_build_outcomes == ["producer_failed", "producer_failed"]
    assert state.card_build_producer_failed_reasons == {
        "card-a": [UNRECONCILED_AFTER_RESTART], "card-b": [""]}
    # …but only the Card whose producer really gave up is barred.
    assert SpeculationMixin._producer_failed_card_ids(state) == {"card-b"}


def test_a_second_close_of_any_kind_bars_it_as_before(tmp_path):
    two_restarts = _log(tmp_path / "a", [("producer_failed", UNRECONCILED_AFTER_RESTART)] * 2)
    assert "card-a" in SpeculationMixin._producer_failed_card_ids(two_restarts)
    restart_then_real = _log(tmp_path / "b", [("producer_failed", UNRECONCILED_AFTER_RESTART),
                                              ("producer_failed", None)])
    assert "card-a" in SpeculationMixin._producer_failed_card_ids(restart_then_real)
    real_then_restart = _log(tmp_path / "c", [("producer_failed", None),
                                              ("producer_failed", UNRECONCILED_AFTER_RESTART)])
    assert "card-a" in SpeculationMixin._producer_failed_card_ids(real_then_restart)


def test_a_legacy_close_with_no_reason_still_bars(tmp_path):
    """Every log written before the reason existed folds exactly as it did."""
    state = _log(tmp_path, [("producer_failed", None)])
    assert SpeculationMixin._producer_failed_card_ids(state) == {"card-a", "card-b"}


def test_a_stale_close_is_not_a_give_up_whatever_its_reason(tmp_path):
    state = _log(tmp_path, [("stale", UNRECONCILED_AFTER_RESTART)])
    assert "card-a" not in state.card_build_producer_failed
    assert "card-a" not in state.card_build_producer_failed_reasons


def test_the_reason_is_registered_and_the_coarse_vocabulary_is_unchanged():
    assert UNRECONCILED_AFTER_RESTART in CARD_BUILD_SKIP_REASONS


# ------------------------------------------------- whose receipt it is, driven through the engine

class _ClosingTaskGroup:
    """A task group already tearing down: `start_soon` refuses. The one way a live process keeps its
    OWN attempt receipt with no producer running and no result stored — `_start_request_producer`
    rolls its in-memory marker back and leaves the durable receipt (its "ACCEPTED asymmetry")."""

    def start_soon(self, *_args, **_kwargs):
        raise RuntimeError("the task group is closing")


def _attempted_at_teardown(run_dir):
    """Engine A elects card-7 and writes its attempt receipt through the real producer start, whose
    spawn is then refused at teardown: a receipt, no producer, no result — in A's own process."""
    engine, producer = _engine(run_dir)
    _start(engine)
    _add_ready_draft(engine)
    key = engine._request_key(_request(engine))
    session = CardSession(max_eval_seconds=None, wall_deadline=None,
                          task_group=_ClosingTaskGroup())
    with pytest.raises(RuntimeError, match="closing"):
        engine._start_request_producer(fold(engine.store.read_all()), session)
    receipts = [e for e in engine.store.read_all() if e.type == EV_CARD_BUILD_ATTEMPTED]
    assert len(receipts) == 1 and not engine._spec_build_inflight and not engine._spec_builds, (
        "precondition: a receipt of A's own, nothing running and nothing stored")
    return engine, producer, key


def _closes(engine):
    return [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]


def test_this_processs_own_unreconciled_attempt_closes_bare_and_bars_the_card(tmp_path):
    """The receipt is A's own, so the quarantine is an in-process give-up: bare `producer_failed`,
    exactly the close every quarantine wrote before the name existed, and the Card is barred.
    MUTATION: name every quarantine close (the first cut's inference) and this close carries
    `unreconciled_after_restart`, letting the Card off a bar its own give-up earned."""
    engine, producer, key = _attempted_at_teardown(tmp_path / "own")
    assert engine._serve_card_builds() is True
    assert _closes(engine) == [{"card_id": key[0], "generation": key[1],
                                "skipped": "producer_failed"}], _closes(engine)
    assert producer.calls == 0, "the quarantine never re-issues the possibly-charged work"
    assert engine._card_requires_serial_fallback(key[0]) is True


def test_a_restarted_engine_names_the_close_of_the_attempt_an_earlier_process_made(tmp_path):
    """The SAME receipt, served by the process that replaced A: its engine-start boundary
    (`_enter_run`, what `looplab resume` runs) is past the receipt, so the close is proven a restart's
    and the Card stays electable. MUTATION: never name the close and the Card is barred for a kill."""
    _dead, producer, key = _attempted_at_teardown(tmp_path / "restart")
    restarted, _unused = _engine(tmp_path / "restart", producer=producer)
    restarted._enter_run()
    assert restarted._serve_card_builds() is True
    assert _closes(restarted) == [{"card_id": key[0], "generation": key[1],
                                   "skipped": "producer_failed",
                                   "skipped_reason": UNRECONCILED_AFTER_RESTART}], _closes(restarted)
    assert producer.calls == 0
    assert restarted._card_requires_serial_fallback(key[0]) is False
    # …and the proof is the boundary, not the order of calls: it was noted at engine start, before
    # this process served anything, and it sits at or above the dead process's receipt.
    receipt = next(e for e in restarted.store.read_all() if e.type == EV_CARD_BUILD_ATTEMPTED)
    assert restarted._spec_entry_seq is not None and receipt.seq <= restarted._spec_entry_seq
