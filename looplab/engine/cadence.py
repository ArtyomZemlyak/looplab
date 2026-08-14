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

There is a SECOND clock here since 2026-08-14, `occupancy_due` (backlog F1g), and it is deliberately
not a variant of the first: a node count cannot express "an evaluation has been running for four
hours and the board behind it is empty", which is the state that cost this box 167.7 GPU-h. Read its
docstring before adding a third — the rule that keeps two paces from collapsing into one is that a
pace which records an `at_node` mark is a node-count pace whatever it is called.
"""
from __future__ import annotations


def cadence_due(n: int, last: int, every: int) -> bool:
    """Whether a full window of `every` nodes has passed since this consumer last fired.

    `every <= 0` disables the cadence rather than raising: the interval knobs are `ge=1` in
    `Settings`, but the `Engine` kwargs and `EngineOptions` accept 0, and some of these gates run
    with no consumer wired at all.
    """
    return every > 0 and n > 0 and n - last >= every


# ------------------------------------------------------------------- the SECOND pacing rule (F1g)
#
# `cadence_due` above paces on the NODE COUNT, and on a run whose evaluations are hours long that is
# the wrong clock entirely. Measured across the 52-run corpus on this box: 167.7 GPU-h with NO
# evaluation running at all, against 164.4 GPU-h of work actually done — the largest single number in
# `docs/29-operator-backlog-2026-08-11.md`, and bigger than the F1f barrier it sits beside. The board
# empties while a long evaluation runs; `n` does not move, so nothing on a node-count window is ever
# due; and the producer only gets another chance once the node it is waiting for terminates. The run
# then pays a full build latency SERIALLY after every terminal instead of hiding it behind the
# evaluation that was already running (v6: a 15-37 minute hole between every consecutive pair).
#
# So there are two clocks, and this is the other one. Note what it is NOT paced on: it does not read
# `n`, `last`, or any at_node mark, and no consumer of it writes one.
def occupancy_due(inflight: int, queued: int, width: int) -> bool:
    """Whether the run should PRODUCE now because a GPU is busy and the board behind it is empty.

    `inflight` — evaluations actually running. `queued` — work already built and waiting to be
    admitted (a pending Node the consumer will start on its next poll). `width` — the settled eval
    concurrency. Due when an evaluation is running AND the supply behind it does not cover the slots:
    "an evaluation is running and the board has nothing selectable", which
    `docs/29-operator-backlog-2026-08-11.md` F1g names as the genuine trigger.

    `inflight > 0` is the load-bearing half and not a formality. With nothing running, an empty board
    is the ORDINARY create turn the outer loop has always taken, and this rule must not become a
    second, differently-worded copy of it. What is new is only that the same production is reachable
    while a slot is held.

    WHY IT CANNOT SATISFY — OR BE SATISFIED BY — `cadence_due` AND `already_covered_at`. Those two
    are a pair: `cadence_due` is a since-last WINDOW over a consumer's own durable marks, and
    `search/coverage.py::already_covered_at` is its at_node IDEMPOTENCE twin, and each is
    parametrized over its own consumer's records precisely so two consumers cannot advance each
    other. A second pacing rule that recorded an `at_node` would break both halves at once: it would
    close the node-count window for a full `every` nodes (starving the consumer it fired for), and
    its own record would then make `already_covered_at` refuse the NEXT starvation at the same node
    count — a rule that fires once per node count is a node-count rule wearing another name.

    This one records nothing, so neither can happen. Its idempotence is the CONDITION, which is
    self-clearing: production makes the board non-empty, `queued` rises, and the rule is no longer
    due until the board empties again. The one direction that does compose is the honest one — work
    produced here becomes a Node, `n` advances, and the node-count cadences become due because a node
    really was created.
    """

    return inflight > 0 and (inflight + queued) < max(1, width)


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


def seed_boundary_due(n: int, last: int, n_seeds: int) -> bool:
    """The FIRST-EVER firing of a periodic phase: at or past the seed boundary, never fired yet.

    `n == n_seeds` is what this used to be, in both consumers of the gate, and it is the exact defect
    this module's header describes — an EQUALITY against a count that advances in strides of k > 1.
    A fan-out or a speculative prefetch steps over the instant and the phase's first firing is lost;
    for a phase whose ordinary interval is longer than the run, that means it never fires at all.

    Measured 2026-08-11, concept tagging (`concept_retag_every=30`, longer than any real run here):

      | run                        | nodes | n_seeds | node_concepts events |
      | rubert-dr-0807             |    14 |       1 | 1  (n == 1 is hard to miss) |
      | lt-recovery-0811           |     6 |       3 | 0  |
      | rubertlite-dr-unified-v2   |     3 |       3 | 0  |

    The 6-node run consulted its Strategist 28 times, so the shared `pending_nodes()`/`n == 0` guards
    were passing constantly — only the equality was missed. Every downstream concept surface reads as
    broken because of it: the run list's concept rollup, the memory shelf's per-record tags, the
    global concept map.

    `>=` rather than `==`, gated on `last == 0` so it can only ever fire ONCE. It cannot advance a
    consumer that has already run — that is `cadence_due`'s job and its window stays the operator's
    knob.
    """
    return n > 0 and last <= 0 and n >= max(1, int(n_seeds or 0))
