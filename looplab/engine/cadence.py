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


# --------------------------------------------------------------- the ONE knob whose 0 is not "off"
#
# Every other interval here reads `0` as DISABLED (see `cadence_due` above), and that is the right
# default for a knob nobody asked for. `deep_research_every` is the exception, by owner decision on
# 2026-08-07: "if it's supposed to work in parallel then we should remove its start restriction
# altogether. Base assumption is that we work in parallel, so deep research can run in parallel too —
# so its default should be 0, i.e. it should start right away."
#
# WHY THE KNOB NEEDED A SECOND SPELLING RATHER THAN A SMALLER NUMBER. The cadence counts NODES while
# the whole feature is phrased around TIME ("a two-day eval is re-researched about hourly"). Measured
# on the three flagship GPU runs `runs/rubert-dr-0804/0805/0807` — 1.5-4 hours per node,
# `deep_research_every=3` and `concurrent_research=true` in every snapshot — deep research fired
# ZERO times and `research_attempted`/`research_completed` have zero rows in all three; a first think
# would not have arrived before 5-12 hours of wall clock. Every run where it DID fire has sub-second
# evals. A window of 1 would still be a window; what the workload needs is no window at all.
#
# So `0` is now the zero-WIDTH window: due at the first node and at every node-count thereafter that
# has not already been researched (`_already_researched_at` is what keeps a resume from re-paying).
# "Off" moved to a NEGATIVE value, `DEEP_RESEARCH_OFF` (-1) — manual `deep_research` control events
# and the Strategist's `request_research` still fire there, exactly as `0` used to mean.
#
# The translation lives HERE, next to `cadence_due`'s `every > 0`, and NOT inside it: `cadence_due`
# is shared by `lessons_every` / `lessons_refresh_every` / `report_every` / `strategist_every` /
# `concept_retag_every`, and for all of those `0` means off and must keep meaning off.
DEEP_RESEARCH_OFF = -1


def deep_research_window(every: int) -> int:
    """Settle `deep_research_every` into a `cadence_due` window.

    `0` (the product default) settles to `1` — a window one node wide is the narrowest one
    `cadence_due` can express, and against its `n - last >= every` form that is due at EVERY new
    node-count, i.e. "start right away and keep going". Anything negative settles to `0`, which is
    what `cadence_due` already reads as disabled. Positive values pass through unchanged.

    Non-integers and bools settle to OFF rather than raising: this gate is read from a resumed
    snapshot and from a partially-built `Engine` in tests, and a junk value must not start paying a
    provider on its own. (A bool is an int in Python, and `True` would otherwise mean "every node".)
    """
    if isinstance(every, bool) or not isinstance(every, int):
        return 0
    if every == 0:
        return 1
    return every if every > 0 else 0


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
