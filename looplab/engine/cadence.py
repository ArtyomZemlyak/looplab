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

    FOUR OF THE FIVE now call this, and the fifth is a REFUSAL rather than an oversight — say so
    here, because the first cut of this fix converted two and left the reader of this paragraph to
    assume the family was done. The 2026-08-19 corpus audit is what closed the other two:
    `lessons_distilled` and `report_generated (trigger=cadence)` are 19/1/0/1/0/0 and 26/1/0/1/0/0
    over dense-retrieval / v6 / v7 / v8 / v9 / the live `e5small-dr-unified-v2` — zero in exactly
    the three runs with no quiescent prefix, on the same configuration as the runs where they fire.
    `_maybe_deep_research` keeps the old predicate, and the evidence for that is in the same table:
    `research_completed (trigger=cadence)` is 27/5/2/14/6/9, alive in ALL SIX runs, because the
    CONCURRENT half of that one decision (`orchestrator._spawn_research` -> `_due_research_trigger`)
    never carried the guard. Moving the serial half would put a main-task think and a background
    think at the same node count with only a read-then-write window between their shared
    `_cadence_research_marks` check and their receipts — buying a double-spend to reach work that is
    already being done. The residual hole is `concurrent_research=false`, which is not the shipped
    default; it is filed as `docs/BACKLOG.md` F1i-b rather than patched here.

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

    BEING DUE IS NOT THE SAME AS PRODUCING, and this rule read as due for five hours and nineteen
    minutes while producing nothing (`runs/e5small-dr-unified-v4`, 2026-08-20: node 0 scored
    0.758851, node 1 held GPU 1, node 2 was killed by the freshness gate having never been admitted,
    and GPU 0 sat dark from 09:45:42 to 15:04:57 — 130 outer-loop turns, `inflight=1 queued=0
    width=2` on every one of them, and `phase_progress` for node 3 in the same MINUTE as node 1's
    terminal). The pace was never the defect: `_occupancy_paced_creates` asks the SESSION's own
    producer lanes with the running Nodes masked, and for an ASHA-family policy
    `card_selection._speculative_selection` answered `[], []` to every masked query naming an
    unresolved rung-0 root — see `card_selection.py::_asha_mask_is_unsound`, which is now consulted
    at the one lane that reads a masked POLICY view rather than at the whole query. Replayed over
    every run on this box that had this pace: 8.03 starved hours, 5.94 of them that predicate,
    0.00 in every GreedyTree and EvolutionaryPolicy run. `tests/test_occupancy_pace_under_asha.py`
    drives it, and an AST check that this function is CALLED would have been green throughout.
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


# ---------------------------------------------------------------------------------- backfill (F1h)
#
# WHOSE PROBLEM THIS IS. `proposal_derived_width` settles the run's evaluation width from the WIDEST
# footprint any open proposal declares, so one card asking for two GPUs on a two-GPU box settles the
# width to 1 — correctly, because when that experiment runs it will need both devices. What it does
# NOT do is fill the gap in the meantime: measured on `runs/e5small-dr-unified-v4`, a node declaring
# `{"gpus": 1}` held one card for a nine-hour evaluation while the other idled the whole time,
# reserved for a two-GPU proposal nobody had started.
#
# THE LITERATURE'S ANSWER IS BACKFILLING, and the variant that fits here is the SLACK-BASED one:
# a waiting job may start on the free devices even if it delays the reservation, provided the delay
# stays inside a bound. Strict EASY backfilling — "may start only if it provably finishes before the
# reservation" — is the wrong bound for this workload and the arithmetic says why. With seven hours
# left on the running node and a nine-hour candidate, EASY refuses and spends SEVEN device-hours of
# idleness to avoid TWO hours of delay. The trade is 3.5:1 against it.
#
# So the rule below weighs the two directly rather than testing a deadline:
#
#     benefit = min(candidate, remaining)      device-hours the free device stops wasting
#     cost    = max(0, candidate - remaining)  hours the reservation is pushed back
#     admit   ⟺  benefit > lam * cost
#
# `lam` is the one knob and it is a PRICE, not a threshold: how many device-hours of idleness the
# operator will pay to avoid one hour of delay to reserved work. At 1.0 an hour of delay and a
# device-hour are worth the same, which admits the 7-vs-9 case (7 > 2) and refuses the hopeless one
# (one hour left, nine-hour candidate: 1 > 8 is false). Above 1.0 the reservation is protected more;
# below it, utilisation wins.
#
# WHY A PRICE AND NOT THE USUAL SLACK FACTOR. Slack-based backfilling normally caps the delay at a
# fraction of the reserved job's runtime, which is a bound on the WORST case and says nothing about
# what the delay buys. Here both sides are measurable in the same unit, so the comparison can be
# exact instead of conservative. That is only possible because the ETA is good: `LossTrajectory.
# eta_s` landed within 0.3 % at the halfway mark of a real 9.13-hour node, where the HPC literature
# is built around user estimates that overshoot by 2-3x (Tsafrir/Etsion/Feitelson, TPDS 2007). A
# rule this sharp would be reckless on those inputs and is merely honest on these.


def _backfill_terms(candidate_s, remaining_s):
    """`(benefit, cost)` in seconds, or None when the inputs cannot support a decision.

    ONE definition for both the predicate and the receipt. They were written as two copies first,
    and mutation caught what that costs: flipping `max(0.0, ...)` in the predicate turned NO test
    red, because the receipt kept its own correct copy and the predicate's answer happens not to
    change when `cost` goes negative. A term that only one of two spellings uses cannot be tested
    through the other, and two spellings of one rule is the drift this module keeps refusing.

    Refuses on any unknown or impossible input — a missing or non-finite ETA, a non-positive
    duration, a non-positive gap. `remaining_s <= 0` is a refusal and not a computation: the
    reservation can start NOW, so there is no gap to fill, and saying `unknown` is honest where
    "the delay exceeds the gain" would be a verdict about a trade that does not exist.
    """
    try:
        cand = float(candidate_s)
        rem = float(remaining_s)
    except (TypeError, ValueError):
        return None
    if cand != cand or rem != rem:                       # NaN
        return None
    if cand in (float("inf"), float("-inf")) or rem in (float("inf"), float("-inf")):
        return None
    if cand <= 0 or rem <= 0:
        return None
    return min(cand, rem), max(0.0, cand - rem)


def backfill_admits(candidate_s, remaining_s, *, lam: float = 1.0) -> bool:
    """Whether a candidate may take a free device that is reserved for wider work.

    `candidate_s` — how long the candidate will hold the device. `remaining_s` — how long until the
    reservation could have started anyway, i.e. the running work's own remaining time. Both in
    seconds; `lam` prices an hour of delay against a device-hour reclaimed.

    REFUSES ON ANY UNKNOWN, and that is the load-bearing half. A missing or non-finite ETA means the
    engine cannot say how long something will take, and admitting on a guess is how a scheduler
    turns one idle device into two late experiments. `remaining_s` of zero admits nothing either:
    the reservation can start NOW, so there is no gap to fill and no idleness to reclaim.

    A candidate that fits entirely inside the gap costs nothing and is always admitted — that is the
    EASY case, and it falls out of the arithmetic rather than being special-cased (`cost == 0`, and
    any positive benefit beats `lam * 0`).
    """
    try:
        price = float(lam)
    except (TypeError, ValueError):
        return False
    if price != price or price < 0:                      # NaN or a negative price
        return False
    terms = _backfill_terms(candidate_s, remaining_s)
    if terms is None:
        return False
    benefit, cost = terms
    return benefit > price * cost


def backfill_receipt(candidate_s, remaining_s, *, lam: float = 1.0) -> dict:
    """The decision plus the arithmetic that produced it, for the durable log.

    Recorded rather than acted on while the rule is being observed: the engine can write what it
    WOULD have admitted and let a real corpus accumulate before any admission changes. That is the
    same "measure first" shape `docs/BACKLOG.md` records for the deterministic stop gate, and it
    matters more here because the cost of a wrong admission is a real training run delayed.

    OPEN[backfill-receipt-unwired] nothing in the engine calls this (or `backfill_admits`), so
    the measure-first corpus the paragraph above depends on can never start accumulating.
    proof:absent:backfill_receipt(@looplab/engine/orchestrator.py
    REVIEW 2026-08-29 (P3 delivery): only tests import the pair; no site computes a candidate
    ETA/remaining pair and writes the row, so the F1h idle-device gap this was built for stays
    unmeasured while the rule reads as observed. Fix direction: stamp the receipt onto a
    diagnostic row where `_occupancy_paced_creates` already deliberates, then delete this marker.
    """
    admits = backfill_admits(candidate_s, remaining_s, lam=lam)
    row = {"admits": admits, "lam": round(float(lam), 4) if isinstance(lam, (int, float)) else None}
    terms = _backfill_terms(candidate_s, remaining_s)
    if terms is None:
        row["why"] = "unknown_duration"
        return row
    benefit, cost = terms
    row["candidate_s"] = round(float(candidate_s), 1)
    row["remaining_s"] = round(float(remaining_s), 1)
    row["benefit_s"] = round(benefit, 1)
    row["cost_s"] = round(cost, 1)
    row["why"] = ("fits_inside_the_gap" if cost == 0
                  else ("worth_the_delay" if admits else "delay_exceeds_the_gain"))
    return row
