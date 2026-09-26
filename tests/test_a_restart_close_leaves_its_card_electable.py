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
"""
from __future__ import annotations

from looplab.engine.speculation import (
    CARD_BUILD_SKIP_REASONS, UNRECONCILED_AFTER_RESTART, SpeculationMixin,
)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_BUILD_DONE, EV_CARD_BUILD_REQUESTED


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
