"""The shared since-last node-count cadence gate (doc 25 EC-07).

`n % every == 0` is wrong here and the codebase already knew it in one place: the node count does
not advance one at a time. A failed/merged/ablated node, a rung-0 seed batch, and — since
`llm_parallel > 1` — an ordinary build fan-out all move it by k > 1, so a modulo gate can step clean
over the only multiple in a window. The Strategist consult, the coverage snapshot and the
concept-coverage snapshot were all still on modulo.
"""
from __future__ import annotations

import pytest

from looplab.engine.cadence import cadence_due, cadence_marks


def test_a_full_window_since_the_last_fire_is_due():
    assert cadence_due(10, 0, 10) is True
    assert cadence_due(9, 0, 10) is False
    assert cadence_due(21, 12, 10) is False and cadence_due(22, 12, 10) is True


def test_a_batch_stride_that_misses_every_multiple_still_becomes_due():
    """The regression: with build width 4 and interval 5, the counts are 4, 8, 12, 16, 20 — modulo
    fires on 20 only by luck of the arithmetic, and with interval 7 it never fires at all."""
    for interval in (5, 7, 9):
        counts = [4, 8, 12, 16, 20, 24, 28]
        last, fired = 0, [n for n in counts if cadence_due(n, 0, interval)]
        assert fired, f"interval {interval} never became due on a width-4 stride"


def test_a_disabled_interval_never_fires_and_never_raises():
    """The interval knobs are ge=1 in Settings but the Engine kwargs accept 0, and some of these
    gates run with no consumer wired — a modulo there was a ZeroDivisionError mid-loop."""
    assert cadence_due(50, 0, 0) is False and cadence_due(50, 0, -3) is False


def test_an_empty_run_is_never_due():
    assert cadence_due(0, 0, 1) is False


def test_the_mark_is_the_LATEST_fire_not_the_earliest():
    """Taking the earliest would leave the window permanently open and re-fire every node — for the
    concept snapshot that is a paid LLM call per node."""
    assert cadence_marks([{"at_node": 3}, {"at_node": 17}, {"at_node": 9}]) == 17


def test_no_records_reads_as_never_fired():
    assert cadence_marks([]) == 0 and cadence_marks(None) == 0


@pytest.mark.parametrize("record", [
    {}, {"at_node": None}, {"at_node": "12"}, {"at_node": 1.5}, {"at_node": True}, "not a dict",
])
def test_a_malformed_mark_is_ignored_rather_than_guessed_at(record):
    """A cadence that trusted a junk mark could starve (a huge value) or fire every node."""
    assert cadence_marks([record]) == 0


def test_a_negative_mark_cannot_push_the_window_open_early():
    """`at_node` is a node COUNT. A negative one would make `n - last` overshoot the interval and
    fire the cadence before its window."""
    assert cadence_marks([{"at_node": -5}]) == 0
    assert cadence_marks([{"at_node": -5}, {"at_node": 4}]) == 4


def test_a_valid_mark_survives_alongside_junk():
    assert cadence_marks([{"at_node": "x"}, None, {"at_node": 8}, 7]) == 8
