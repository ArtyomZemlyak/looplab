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

There is STILL no third pace, and `at_creation_boundary` below is why the obvious candidate was
refused rather than built (backlog F1i). What was broken was never the PACE — it was the
PRECONDITION every node-count consumer shared, `state.pending_nodes()` empty, which since F1f made
evaluation children outlive the turn that admitted them is a state a GPU run does not reach until
its last evaluation has terminated. A "third pace for the concept classifier" would have had to
record an `at_node` (its consumers all do: `node_concepts`, `concept_coverage_snapshot`,
`coverage_snapshot`, `strategy_decision`), which by the rule above makes it the FIRST pace under
another name — and it would have had no self-clearing condition to bound its money the way
`occupancy_due` does. So the fix is one line of precondition, and the pace count stays two.
"""
from __future__ import annotations


def cadence_due(n: int, last: int, every: int) -> bool:
    """Whether a full window of `every` nodes has passed since this consumer last fired.

    `every <= 0` disables the cadence rather than raising: the interval knobs are `ge=1` in
    `Settings`, but the `Engine` kwargs and `EngineOptions` accept 0, and some of these gates run
    with no consumer wired at all.
    """
    return every > 0 and n > 0 and n - last >= every


# ----------------------------------------- the PRECONDITION every node-count consumer shares (F1i)
def at_creation_boundary(pending: int, *, while_evaluating: bool) -> bool:
    """Whether a node-count cadence may fire at the decision point the outer loop has just reached.

    THIS IS NOT A PACE and must never become one — it reads no `n`, no `last` and no `every`, and it
    records nothing. `cadence_due` above still decides HOW OFTEN; this decides only WHETHER THIS
    MOMENT COUNTS as one of the run's creation decision points.

    WHAT THE OLD SPELLING PROTECTED, in its own words, because it must be stated before it is
    touched. The whole reason is SIX WORDS, written once and never argued for again: `bb421e0f7`
    (2026-06-24, the commit that added the Strategist) introduced

        # docstring: 'Bounded, deterministic cadence: only at a creation decision point (no
        #             pending evals), at the seed boundary or every `strategist_every` created
        #             nodes.'
        if state.pending_nodes():
            return False

    and its commit message says nothing about the guard at all. Four more consumers then copied it
    by imitation over the next three weeks — `concept_cadence.py::_should_consult_concepts`
    ("Same shape/guards as `_should_consult`"), `lessons.py::maybe_distill_lessons` ("fires only at
    a creation decision point (no pending evals), mirroring deep-research"),
    `research_cadence.py::_maybe_deep_research` ("Auto triggers only at a creation decision point")
    and `_maybe_refresh_report`, which states no reason at all.

    The parenthesis is the tell: "no pending evals" was never the requirement, it was the
    OBSERVABLE that used to coincide with the requirement. Under serial evaluation the loop could
    only be at a creation decision point when nothing was in flight, so one test served for both —
    and the phases it gates really do want a decision point, because the Strategist rewires
    policy/operators/fidelity/widths for the nodes the policy is about to propose and the coverage
    snapshots are the brief it reads. `_apply_strategy` already says in its own docstring that those
    knobs are safe to move between iterations ("self.timeout is read fresh per eval and
    self._eval_parallel rebuilds the CapacityLimiter each batch, so a mid-run change takes effect on
    the next node without any rewiring"), and the Developer swap it guards is still taken between
    sequential `_create_node` calls. And the eval dispatcher was ALREADY hardened for this writer:
    `orchestrator.py`'s batch semaphore captures its own token total precisely because
    "`self._eval_parallel` is live and has three writers that move it mid-batch (the Strategist, an
    operator `budget_extend`, and since docs/29 F1 the proposal-derived re-pin)". So the requirement
    survives the change; only the proxy for it does not.

    WHY IT STOPPED BEING REACHABLE. Backlog F1f (2026-08-13) made the eval task group run-scoped, so
    `_run_card_session` returns while its evaluations burn and the outer loop keeps turning. The
    creation decision point is still reached — `_run_cadences` has exactly one call site, in the
    outer loop, after the width settle and the speculation settles, on a stable decision prefix that
    the `post_cadence_seq != decision_seq` re-enter maintains — but the observable is now false
    forever. Measured over `runs/` on 2026-08-18, prefixes with >0 nodes and 0 pending:

        rubertlite-dense-retrieval (to 07-18)   683 (43 windows)   classifier fired 159x
        rubertlite-dr-unified-v6   (to 08-13)   850 ( 5 windows)   fired
        rubertlite-dr-unified-v7   (from 08-14)   0                never
        rubertlite-dr-unified-v8                148 ( 1 window)    fired ONCE, in the window
        rubertlite-dr-unified-v9                  0                never
        e5small-dr-unified-v2 (live, 11.6 h)      0                never

    v8 is the one that settles the argument, and NOT because it is an older code baseline — it
    started 2026-08-14 16:25, after every commit in the F1f chain, and `git log -S` over its window
    touches neither `_eval_inflight` nor `pending_nodes`. Its config is byte-identical to v9's. Its
    148 quiescent prefixes are ONE window, the last 2.3 % of a 47.6-hour log, opening on the run's
    FINAL `node_evaluated` — 8.1 minutes of end-of-run drain — and every cadence firing that run ever
    made is inside it. So the difference between v8 and v9 is RUN SHAPE, not configuration and not
    code: the family now fires at most once per run, in the drain, and not at all in a run that ends
    with an evaluation still going (v7, v9 and the live `e5small-dr-unified-v2` each end with three
    pending nodes and recorded nothing).

    This was seen coming. `docs/audit/2026-08-07-search-loop.md` observed six days BEFORE F1f that
    the same guard on the serial deep-research path "under speculation is almost never true", and
    filed it as "Decision needed". F1f took it from almost-never to never.

    `while_evaluating` is the operator's kill switch (`Settings.cadence_while_evaluating`, ON).
    `False` restores the historical predicate byte for byte, which is what makes the old behaviour a
    negative control rather than a memory.

    THE MONEY RULE THIS DEPENDS ON, stated here because it is the reason this is safe rather than an
    unbudgeted mid-eval spend. The PACE does not change, so the number of paid passes per node count
    does not change: each consumer's `at_node` idempotence twin (`search/coverage.py::
    already_covered_at`, `_autonomous_strategy_already_recorded_at`) still admits one firing per
    node-count. What DOES change is that the loop can now reach the gate many times at the SAME `n`,
    where it used to reach it once and then create a node — and two consumers record no mark on the
    "nothing changed" path (the Strategist records only a CHANGED strategy; the concept snapshot
    records nothing when the producer yields None). Those two therefore carry an explicit in-process
    attempted-at-`n` memo. Without it this predicate is a paid LLM call per outer-loop turn.
    """
    return bool(while_evaluating) or int(pending or 0) <= 0


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
