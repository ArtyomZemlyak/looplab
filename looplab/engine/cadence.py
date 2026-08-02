"""The node-count cadence gate every periodic engine phase shares (doc 25 EC-07).

`n % every == 0` is the wrong shape here and the codebase already knew it in one place: the node
count does NOT advance one at a time. A failed/merged/ablated node, a rung-0 seed batch, and — since
`llm_parallel > 1` — an ordinary build fan-out all move it by k > 1, so a modulo gate can step clean
over the only multiple in a window and skip the phase entirely. With build width 4 and an interval
of 5 the counts land on 4, 8, 12, 16, 20 and never on a multiple of 5: the phase never runs at all.

The since-last form has no such hole. It also composes with resume, because `last` comes from the
consumer's own DURABLE record of when it last fired rather than from process memory — and each
consumer keeps its own, because the Strategist consult, the coverage snapshot and the
concept-coverage snapshot advance independently and must not be able to satisfy each other's window.
"""
from __future__ import annotations


def cadence_due(n: int, last: int, every: int) -> bool:
    """Whether a full window of `every` nodes has passed since this consumer last fired.

    `every <= 0` disables the cadence rather than raising: the interval knobs are `ge=1` in
    `Settings`, but the `Engine` kwargs and `EngineOptions` accept 0, and some of these gates run
    with no consumer wired at all.
    """
    return every > 0 and n > 0 and n - last >= every


def cadence_marks(records) -> int:
    """The highest `at_node` among a consumer's folded records, else 0.

    0 means "never fired", which leaves the window open from the start of the run — right for a
    consumer that has not run yet, and right after a resume whose log carries no record either.
    A malformed or absent `at_node` is ignored rather than guessed at: a cadence that trusted a
    junk mark could either starve (a huge value) or fire every node (a negative one).
    """
    marks = [record.get("at_node") for record in (records or [])
             if isinstance(record, dict) and type(record.get("at_node")) is int
             and record.get("at_node") >= 0]
    return max(marks, default=0)
