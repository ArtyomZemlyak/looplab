"""Request-driven Card speculation (docs/23, Layers 5a/5b).

The append-only log remains the queue.  Background producer work — the isolated Card build and the
raw proposal — may only return an in-memory ``SpecBuildResult`` / ``SpecRawStageResult``; every
selection-affecting event that work leads to, and every speculative ``node_created``, is written by
the main engine task.  That is THIS LANE's rule, not the run's, and the sentence used to read as the
run's (review 2026-09-22, ES1-08): an evaluation child writes its own node's terminal (an anyio task
on the same loop, under ``_write_lock``), and the parallel build's worker threads append their OWN
node's rows — ``card_auto_dropped`` included, through ``node_build.py::_fail_reserved_build`` on a
build crash.  CLAUDE.md invariant #1 lists every typed exception.  The mixin is inert unless both
Card selection and a positive, run-pinned ``speculation_depth`` are enabled.
"""
from __future__ import annotations

import functools
import collections
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

import anyio

from looplab.core import tracing
from looplab.core.advisory_payloads import bounded_cross_run_advisory_receipt
from looplab.core.containment import refuse_budget_stop
from looplab.core.errors import budget_stop_leaf, deferrable_run_stop
from looplab.core.models import (
    Idea,
    NodeStatus,
    RunState,
    card_ownership_receipt,
    durable_idea_payload, is_developer_error, is_developer_stuck)
from looplab.core.llm_broker import in_llm_lane
from looplab.events.eventstore import EventStoreConcurrencyError, retry_tail_cas
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`.
from looplab.engine.shared import engine_fold as fold
from looplab.engine.node_build import developer_crash_records
from looplab.search.card_selection import unconsumed_card_inventory
from looplab.events.types import (
    DIAGNOSTIC_EVENTS,
    EV_CARD_ADDED,
    EV_CARD_BUILD_ATTEMPTED,
    EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED,
    EV_FORESIGHT_SELECTED,
    EV_LLM_COST,
    EV_LLM_USAGE,
    EV_NODE_BUILDING,
    EV_NODE_CREATED,
    EV_NODE_FAILED,
    EV_PAUSE,
    EV_POLICY_DECISION,
    EV_SPECULATION_DEPTH_SETTLED,
    PROGRESS_STAGE_BUILD,
    SETUP_THREAD_APPENDABLE,
)
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    META_CARD_ID,
    CardResourceEnvelope,
    SpeculativeSelectionContext,
    card_budget_used,
    card_lane_width,
    speculative_card_actions,
    speculative_card_is_fresh,
    speculative_raw_actions,
)

_LOG = logging.getLogger(__name__)


# WHY a paid build was closed without minting a node — the CLOSED vocabulary of
# `card_build_done.skipped_reason`. A registry rather than nine loose literals because this repo has
# paid for the alternative twice: `node_repaired.verified` (`inert` was an undiagnosed proxy for
# which bound ended a repair session, #82) and `TRIAGE_ACTIONS`, where a typo'd literal turns a stop
# into "keep repairing". Here a typo'd slug does not fail — it lands on a durable row and reads as a
# refusal nobody can look up. `tests/test_card_build_skip_reasons.py` re-derives this set from the
# claim path's own `ast.Return` constants, in BOTH directions.
#
# MEASURED, and the reason it exists at all: on `e5small-dr-unified-v9`, 3 of 12 builds closed
# `skipped: "stale"` having spent 41.4M tokens — 11.8 % of the run — and the record could not say
# which of these fired. `not_selected_now` (the board moved and the engine wants something else) and
# `card_action_changed` (the build no longer matches what it was built for) are different facts with
# different remedies, and one word covered both.
CARD_BUILD_SKIP_REASONS = (
    "search_epoch_rotated",      # the search epoch moved under the request
    "run_is_stopping",           # a terminal intent is pending
    "eval_budget_exhausted",     # total_eval_seconds crossed the run ceiling
    "commit_not_allowed",        # the outer gate refused the commit
    "selection_limit_reached",   # card_budget_used hit the selection limit
    "not_selected_now",          # selection no longer picks this card — the build is INTACT
    "card_gone",                 # the card is ABSENT from the board (merged away, or never folded)
    "card_dropped",              # the card is PRESENT and administratively dead (status=="dropped")
    "card_action_changed",       # the card's action is not the one this was built for
    "reservation_refused",       # the node reservation would not form
    "idea_changed",              # the reserved idea differs from the built one
    "commit_failed",             # the node commit raised
    "commit_not_ours",           # the committed node is not this build's
    # NO PRODUCER PAIR could be had for the head — no `role_factory`, or one that built no pair for
    # `_PRODUCER_PAIR_RETRY_TURNS` turns running — so no producer ever started and nothing was
    # billed. Closed `stale`, never `producer_failed`: the Card is not at fault and stays electable
    # (review 2026-09-22, ENG1-14 — doc 50 ES1-04; `_start_head_producer` has the account).
    "producer_unavailable",
    # The Strategist swapped the Developer backend while this build ran on the retired one (an
    # adopted build, width > 1, outlives the session the swap waits for). Nothing is wrong with the
    # Card; the build is simply not the treatment the run now uses.
    "builder_replaced",
)

# WHY a consumed raw proposal staged nothing BEFORE the staging fence could say — the two
# pre-staging paths of `_serve_raw_card_stage`, named beside `CARD_BUILD_SKIP_REASONS` because a
# bare slug on a warning nobody can look up is the defect both registries exist for. Deliberately
# NOT members of `CARD_STAGE_REFUSALS`: that tuple may only carry slugs `_stage_prepared_card`
# itself emits (`tests/test_card_stage_refusals.py` pins the set in both directions), and neither
# of these is a staging refusal. `unrecorded` stays the residual for a stager `None` that set no
# slug (entry validation, CAS exhaustion) — the signal a refusal path forgot to name itself.
RAW_STAGE_PRE_STAGING_REASONS = (
    "producer_failed",           # the paid propose raised — nothing reached the stager
    "proposal_refused",          # the propose completed but formed no idea (novelty/degraded gates)
)


@dataclass(frozen=True)
class SpecBuildResult:
    """One isolated producer result.  It is never serialized or treated as queue authority."""

    card_id: str
    generation: int
    action: dict[str, Any]
    success: bool
    idea: Optional[Idea] = None
    code: str = ""
    files: dict[str, str] = field(default_factory=dict)
    deleted: tuple[str, ...] = ()
    footprint_finalized: bool = False
    cross_run_receipt: dict[str, Any] = field(default_factory=dict)
    roles: Optional[tuple[Any, Any]] = field(default=None, compare=False, repr=False)
    error: str = ""
    # The trace id of the `card_build` span this result was produced under, carried back so the node
    # that eventually commits can NAME its own build (`_create_precoded_node`). It is diagnostic
    # provenance, never queue authority — `compare=False` because two results are the same result
    # whether or not tracing was wired, and an empty string is simply "no tracer".
    build_trace: str = field(default="", compare=False)
    # Set when this result is handed back out of `_spec_reusable` for a re-election: a SECOND
    # `not_selected_now` of a result that was already a reuse is not kept again
    # (`_serve_card_builds`). Bookkeeping, never queue authority — `compare=False`.
    reused: bool = field(default=False, compare=False)

    @property
    def key(self) -> tuple[str, int]:
        return self.card_id, self.generation


# One rendering of a producer fault, and one notification swallow (doc 25 EC-12).
#
# Both are bounded on purpose. The message is CAPPED because it is folded into a durable result an
# operator reads: a provider traceback repr can run to megabytes, and an unbounded one turns a single
# failed proposal into an unreadable log line. The type name is kept in front of it because the bare
# `str(exc)` of several provider errors is empty.
_PRODUCER_ERROR_CAP = 2_048


def _proposal_limiter():
    """Lazy hop to `novelty.proposal_limiter` — the same shape the monitors use for
    `evaluate._watch_limiter`, and it keeps the import graph one-directional at module load."""
    from looplab.engine.novelty import proposal_limiter
    return proposal_limiter()


def _card_build_limiter():
    """Lazy hop to `novelty.card_build_limiter`, for the reason `_proposal_limiter` gives."""
    from looplab.engine.novelty import card_build_limiter
    return card_build_limiter()


def producer_error_text(exc: BaseException, prefix: str = "") -> str:
    return f"{prefix}{type(exc).__name__}: {exc}"[:_PRODUCER_ERROR_CAP]


def notify_producer(notify, key) -> None:
    """Post a producer wake-up. Notifications are only HINTS.

    Every one of the three swallowed errors means the consumer is already gone or saturated
    (`WouldBlock` / `ClosedResourceError` / `BrokenResourceError`), and in each case the main task
    re-scans the durable result slots anyway. Letting any of them escape would tear down the task
    group during teardown — i.e. cancel live evaluations — over a hint nobody needed.
    """
    if notify is None:
        # NO SESSION IS LISTENING. Since the eval task group became run-scoped an evaluation child
        # can terminate between two sessions, with `Engine._eval_notify` cleared — the same "the
        # consumer is already gone" case as `ClosedResourceError` below, reached one step earlier.
        # It is still only a hint: the next session's first turn re-reads the log and re-derives
        # `eval_inflight` before it decides anything.
        return
    try:
        notify.send_nowait(key)
    except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
        pass


class _CurrentSessionNotify:
    """The wake-up stream of whichever Card session is CURRENT, resolved at send time.

    An adopted build (width > 1) outlives the session that started it, so the stream it was handed
    may be closed by the time it finishes; the one it must reach is the live session's, which
    `Engine._eval_notify` names — the same handle an adopted evaluation posts to. None between
    sessions: the next session re-scans the result slots on its first turn anyway."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def send_nowait(self, key) -> None:
        notify = getattr(self._engine, "_eval_notify", None)
        if notify is not None:
            notify.send_nowait(key)


def raw_stage_source(action: Mapping[str, Any]) -> str:
    """WHO authored this raw proposal — the engine's own merge operator, or the Researcher.

    One spelling, because the answer rides on the durable result an operator reads and the two
    sites that used to derive it (the worker and its failure path) had to agree: a proposal that
    failed would otherwise be attributed differently from the same proposal that succeeded.
    """
    return "engine" if action.get("kind") == "merge" else "researcher"


@dataclass(frozen=True)
class SpecRawStageResult:
    """One isolated raw-policy proposal awaiting a short main-task Card commit."""

    generation: int
    action: dict[str, Any]
    proposal_state: RunState = field(compare=False, repr=False)
    proposal_node_ceiling: int
    at_node: int
    source: str
    success: bool
    idea: Optional[Idea] = None
    steering_context: tuple[Any, ...] = ()
    cross_run_receipt: dict[str, Any] = field(default_factory=dict)
    audit_events: tuple[tuple[str, dict, Optional[str], Optional[str]], ...] = ()
    error: str = ""

    @classmethod
    def failure(cls, exc: BaseException, *, generation: int, action: Mapping[str, Any],
                proposal_state: RunState, proposal_node_ceiling: int, source: str,
                audit_events: tuple = ()) -> "SpecRawStageResult":
        """The CONSUMED, non-staged result of a raw proposal that raised (doc 25 EC-12).

        Both sites that build one — the worker's own guard and the wrapper around it — used to spell
        out ten or eleven keyword fields against this fourteen-field dataclass, and the fields they
        share are exactly the ones a copy cannot get wrong loudly: a proposal that raised is still
        `success=False` at the SAME ceiling and under the SAME `source` as the proposal that would
        have succeeded, or `_serve_raw_card_stage` consumes a result attributed to nobody.

        `audit_events` is the one field the two sites legitimately disagree about and it is therefore
        a parameter rather than a default the classmethod invents: the worker may already have
        buffered folded intents before it raised (they are published by the main task and must not be
        dropped), while the wrapper's guard fires when the worker never returned at all and has
        nothing to carry.  `at_node` is not a parameter for the opposite reason — a failed proposal
        is always AT the ceiling it was prepared against, and the two sites already agreed on that.
        """
        return cls(
            generation=generation,
            action=dict(action),
            proposal_state=proposal_state,
            proposal_node_ceiling=proposal_node_ceiling,
            at_node=proposal_node_ceiling,
            source=source,
            success=False,
            audit_events=tuple(audit_events),
            error=producer_error_text(exc),
        )


def needs_outer_rebuild(node) -> bool:
    """A pending Node whose rerun crosses the proposal/implementation boundary the outer loop owns."""

    return node.rerun_from in {"implement", "propose"}


@dataclass(frozen=True)
class CardSessionGates:
    """The three FOLD-DERIVED stop conditions of one session turn, derived ONCE per snapshot.

    They used to be spelled out four times per turn (doc 25 EC-02), each copy re-folding the log
    first, and every one of the four had to keep agreeing with the others about what "the outer loop
    owns the next decision" means.  A drift between two copies does not crash: it silently lets one
    phase start speculative work that the phase two lines below has already decided is stale.
    """

    terminal_gate: bool
    budget_exhausted: bool
    outer_rebuild: bool

    @property
    def stopping(self) -> bool:
        """True when the OUTER control/Strategist/cadence boundary owns the next decision."""

        return bool(self.terminal_gate or self.budget_exhausted or self.outer_rebuild)


@dataclass(slots=True)
class CardSession:
    """The mutable state of one ``_run_card_session`` turn loop, so its phases can be methods.

    ``slots=True`` on purpose: every field here used to be a ``nonlocal`` of a ~500-line closure, and
    the one mutation this decomposition could plausibly get wrong is a misspelled flag assignment
    (``session.yeild_outer = True``) that binds a NEW attribute and leaves the real gate open
    forever.  With slots that is an ``AttributeError`` at the first turn instead of a run that
    quietly never yields to the outer loop.
    """

    max_eval_seconds: Optional[float]
    wall_deadline: Optional[float]
    task_group: Any = None
    eval_task_group: Any = None
    bg_task_group: Any = None
    notify: Any = None
    eval_inflight: set[tuple[int, int]] = field(default_factory=set)
    research_spawned: bool = False
    boundary_owed: bool = False
    yield_outer: bool = False
    progressed: bool = False

    def budget_exhausted(self, state: RunState) -> bool:
        return bool(
            (self.max_eval_seconds is not None
             and state.total_eval_seconds >= self.max_eval_seconds)
            or (self.wall_deadline is not None and time.time() >= self.wall_deadline)
        )

    # TWO gates, not one — and the split IS the F1f fix (doc 33 / backlog F1f, F1g).
    #
    # There used to be ONE predicate, `open_for_new_work`, and both session flags closed it for
    # BOTH lanes:  `not (gates.stopping or consumer_completed or yield_outer)`.  `consumer_completed`
    # was set in the `finally` of EVERY eval child, so the FIRST terminal shut admission for every
    # remaining slot — while `_card_phase_decide_exit` still refused to return until the LAST eval
    # drained.  The session therefore stopped STARTING work at the first terminal and reached the
    # outer boundary no sooner than it would have anyway.  Measured across the six width-2 runs on
    # this box: 115.6 GPU-h of idle second slot against 164.4 GPU-h of work actually done — 82.6 %
    # of all second-slot time available while the box was busy.  Worst single window 41.8 h.
    #
    # The two flags never meant anything about the CONSUMER.  They mean "the outer
    # control/Strategist/cadence boundary is owed a turn" (`boundary_owed`, ex-`consumer_completed`)
    # and "the PRODUCER lane has nothing it may do without a fresh outer authority snapshot"
    # (`yield_outer`).  Both are answered by RETURNING, which the run-scoped eval task group now
    # lets this session do while its evals keep running.  Admission is gated by the FOLD-derived
    # half alone, so a freed slot is refilled on the same turn that observed the terminal.
    def open_for_admission(self, gates: CardSessionGates) -> bool:
        """May this turn still START an eval?  The fold-derived stop conditions, and nothing else.

        Deliberately NOT `or self.boundary_owed or self.yield_outer`: neither flag says anything
        about whether a pending, fresh, resource-fitting Node may run — that is what
        `_session_admissible` and the freshness machinery are for, and both still run downstream of
        this gate.  Un-latching does not mean dispatching stale work.
        """

        return not gates.stopping

    def open_for_production(self, gates: CardSessionGates) -> bool:
        """May this turn still START PRODUCER work — a Card build, or a paid raw proposal?

        Here the two flags keep their exact original meaning.  `boundary_owed` still closes this
        lane on the first terminal, because a producer started after a terminal would hold the
        session open for the whole of its paid provider call (`memory_pending` in
        `_card_phase_decide_exit`) and turn "the outer loop is owed a turn" into a fresh barrier of
        its own.  They are read LIVE rather than bundled into the gate snapshot because
        `boundary_owed` is transferred from the eval children at any checkpoint.
        """

        return not (gates.stopping or self.boundary_owed or self.yield_outer)


class SpeculationMixin:
    """Execution helpers inherited by :class:`looplab.engine.orchestrator.Engine`."""

    def _speculation_enabled(self) -> bool:
        return bool(
            getattr(self, "card_driven_selection", False)
            and int(getattr(self, "speculation_depth", 0) or 0) > 0
            and getattr(self, "_speculation_gate_admitted", False) is True
            and bool(getattr(self, "_speculation_gate_receipt_digest", ""))
        )

    # --------------------------------------------------------------- adaptive AUTO depth
    #
    # AUTO resolves the depth AT STARTUP from the settled eval width — "how many experiments can run
    # at once" — which answers a capacity question and not the one that decides whether a prefetch
    # PAYS. A prefetch exists to overlap the Developer's PROVIDER latency with a RUNNING evaluation.
    # When the evaluation finishes in 0.1 s there is nothing to overlap and there never was.
    #
    # MEASURED on `examples/classification_task.json` AS IT SHIPPED BEFORE 2026-08-05 (the flat
    # two-blob variant; that example is now the concentric-rings task, whose evaluations take
    # 0.05-0.6 s — still far under provider latency, so the conclusion is unchanged). Same
    # defaults, same command, both arms 8/8 nodes and the identical champion (node 7, metric 0.925):
    #
    #     AUTO -> depth 1 : 109 LLM calls, 1,265,911 tokens, 2348.8 s wall
    #     speculation_depth=0 :  75 LLM calls,   817,201 tokens, 2448.6 s wall
    #
    # 45% more calls and 55% more tokens for a 4% wall-clock saving, and the overhead was NOT waste
    # from wrong predictions (one stale prefetch in nine requests) — it is fixed Card-lane cost.
    #
    # This is the third case of a rule AUTO already applies twice: it settles ITSELF to off where a
    # prefetch cannot help (a build whose roles call no LLM has no provider latency to overlap; a
    # policy the Card scorer was never built against cannot be asked for the counterfactual). "The
    # evaluations are too short to hide a build behind" is the same argument from the other side, and
    # the only difference is that the evidence for it does not exist until the run has measured it.
    #
    # THE PROPERTY THIS MUST NOT BREAK is engine invariant #3, not "resolve once at startup". Pinning
    # the resolved integer was one cheap way to make a resume reproduce the treatment; the actual
    # requirement is that every side effect is gated on a durable event. So the depth is allowed to
    # move, and each move appends `speculation_depth_settled` carrying the RESOLVED integer plus the
    # evidence — the fold READS the outcome and re-measures nothing, so a resume on a different host
    # continues under the treatment this run chose (`replay.py::_on_speculation_depth_settled`).
    #
    # A ONE-WAY RATCHET, evaluated against a fully adaptive rule and chosen over it:
    #   * it cannot thrash. A symmetric rule oscillates with every slow-then-fast node, and each
    #     oscillation is a durable change to the run's SEARCH TREATMENT, not a tuning knob;
    #   * the harm is asymmetric. Prefetching on a fast task costs tokens for nothing (measured
    #     above); not prefetching on a slow one costs some wall clock and nothing else;
    #   * it bounds the log. The payload's resolved depth is `0` — the rule's whole finding is "there
    #     is nothing here to overlap", which has no smaller answer — so a run emits AT MOST ONE of
    #     these rows: the second call sees `current <= 0` and returns before measuring anything. (This
    #     comment claimed "at most `depth` transitions per run, each strictly smaller" until
    #     2026-08-06, describing a graduated ratchet the writer below never implemented. The fold is
    #     nevertheless written for many rows, and must stay that way: it is what makes a duplicated or
    #     replayed row inert.)
    # AUTO-ONLY, like every other AUTO settling rule here: a SPELLED depth is honoured as spelled.
    _ADAPTIVE_DEPTH_MIN_SAMPLES = 2
    # How much of a build one evaluation must be able to hide before the prefetch earns its fixed
    # cost. A depth-1 prefetch can save at best `min(build, eval)` per node, so at this ratio the
    # ceiling on the saving is ~9% of a node's wall clock — already about double the 4% actually
    # measured above, which is why anything below it is not a close call. Deliberately a RATIO of two
    # measured quantities and not an absolute number of seconds: "fast" only means anything relative
    # to the provider latency the overlap is supposed to hide, and a run whose builds are slow because
    # the endpoint is slow should keep prefetching at eval durations a fast endpoint would not justify.
    _ADAPTIVE_DEPTH_MIN_EVAL_FRACTION = 0.1

    @staticmethod
    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        middle = len(ordered) // 2
        if not ordered:
            return 0.0
        return (ordered[middle] if len(ordered) % 2
                else (ordered[middle - 1] + ordered[middle]) / 2.0)

    def _measured_build_seconds(self, events) -> list[float]:
        """Per-node build wall time, read off the log's own `node_building` -> `node_created` pair.

        MEDIAN, not mean, on both axes: one repaired node or one retried build is an outlier that a
        mean would let decide the run's treatment.
        """
        started: dict[int, float] = {}
        spans: list[float] = []
        for event in events:
            node_id = (event.data or {}).get("node_id")
            if type(node_id) is not int:
                continue
            if event.type == EV_NODE_BUILDING:
                started[node_id] = float(event.ts or 0.0)
            elif event.type == EV_NODE_CREATED and node_id in started:
                span = float(event.ts or 0.0) - started.pop(node_id)
                if span > 0:
                    spans.append(span)
        return spans

    def _settle_speculation_depth(self, state: RunState, events=None) -> bool:
        """Ratchet AUTO depth down once the run's own measurements say a prefetch cannot pay.

        Returns True when a durable settle landed (the caller must re-fold). See the block comment
        above for the rule, the measurement behind it and why it is a one-way ratchet.
        """
        # THE LOG'S ANSWER, NOT THE PROCESS'S. This used to read `self._speculation_depth_auto`,
        # which describes how THIS process resolved its own config — not how the run was launched.
        # Since the shipped default is `-1` (AUTO), any later `looplab run <existing dir>` set that
        # attribute True and could ratchet a run whose launch had SPELLED a depth, landing a durable
        # `speculation_depth_settled` that no flag lifts. The pin alone could not distinguish the two,
        # which is why `run_started` now records the flag and the fold carries it.
        # `state` is the folded log this call is deciding about, so it is the right place to ask.
        if not bool(getattr(state, "speculation_depth_auto", False)):
            return False                          # a SPELLED depth is honoured as spelled
        current = int(getattr(self, "speculation_depth", 0) or 0)
        if current <= 0:
            return False
        # NOT WITH SEVERAL PRODUCERS (2026-09-24). The ratchet's premise is that a prefetch pays
        # only by hiding a build behind a RUNNING evaluation; with a build width above one the
        # session is also where builds run side by side, and switching it off would put them back
        # in a single file exactly where evaluations are short and builds long — the case it measures.
        if self._speculative_producer_width(state) > 1:
            return False
        # QUIESCENT ONLY. Turning the depth to 0 makes `_speculation_enabled()` False, and with it the
        # whole lane that SERVES an outstanding prefetch: an open request head would keep its physical
        # node reservation forever with nothing left able to close it, which leaks the budget and
        # stalls the run — the same shape as the defect this change sits next to. So the ratchet may
        # only fire when there is no head request, no build marker and nothing in flight in this
        # process. Costs nothing: the loop reaches this point once per turn and a fast task is
        # quiescent between batches constantly.
        # …and an ADOPTED EVAL is now one of the things in flight.  Since F1f the outer loop turns
        # while evaluations run, and settling the depth to 0 makes `_speculation_enabled()` False —
        # which routes the very next `_run_card_session` into `_dispatch_evals`, a dispatcher that
        # knows nothing about `_eval_inflight` and would re-dispatch a node this process is already
        # training. Same shape as the leak the head-request clause above closes, one lane over.
        if (self._head_request(state) is not None
                or state.buildings
                or self._evals_inflight()
                or getattr(self, "_spec_build_inflight", None)
                or getattr(self, "_spec_builds", None)):
            return False
        evals = [float(node.eval_seconds or 0.0) for node in state.nodes.values()
                 if node.status is NodeStatus.evaluated and node.eval_seconds is not None]
        if len(evals) < self._ADAPTIVE_DEPTH_MIN_SAMPLES:
            return False                          # one fast node must not switch off the treatment
        events = self.store.read_all() if events is None else events
        builds = self._measured_build_seconds(events)
        if len(builds) < self._ADAPTIVE_DEPTH_MIN_SAMPLES:
            return False
        eval_median = self._median(evals)
        build_median = self._median(builds)
        if build_median <= 0:
            return False
        ratio = eval_median / build_median
        if ratio >= self._ADAPTIVE_DEPTH_MIN_EVAL_FRACTION:
            return False
        payload = {
            "depth": 0,
            "previous": current,
            "reason": (
                "measured evaluations are too short to overlap a build: a prefetch exists to hide "
                "the Developer's provider latency behind a RUNNING evaluation, and there is none "
                "here to hide it behind"),
            # The decision's whole input, so `looplab inspect`/the report can show WHY and the fold
            # never has to re-derive anything from the box it is replaying on.
            "evidence": {
                "eval_samples": len(evals),
                "build_samples": len(builds),
                "median_eval_seconds": round(eval_median, 6),
                "median_build_seconds": round(build_median, 6),
                "eval_fraction_of_build": round(ratio, 6),
                "min_eval_fraction": self._ADAPTIVE_DEPTH_MIN_EVAL_FRACTION,
            },
        }
        self.store.append(EV_SPECULATION_DEPTH_SETTLED, payload)
        self.speculation_depth = 0
        # SAY SO. A run whose depth silently drops has changed its SEARCH TREATMENT, and the operator
        # comparing two runs' token bills deserves to know which one prefetched. WARNING for the same
        # reason the GPU-pool lease wait is at WARNING: it is not an error, but a silent one gets
        # debugged as something else.
        #
        # THE ADVICE HAS TO WORK ON THE RUN DIRECTORY IT IS PRINTED FOR. This line used to end
        # "`-s speculation_depth=%d` keeps it on" with the PRE-settle depth, which was wrong twice
        # over: the settle is durable and the fold applies it on top of the pin, so a resume spelling
        # that depth still runs at 0 — and before 2026-08-06 the re-entry guard refused the resume
        # outright, so the engine's own printed advice was the faster of the two doors out of a
        # resumable run. Name the surface where the choice is actually available: LAUNCH, where a
        # spelled depth opts out of AUTO settling entirely (`_settle_speculation_depth` returns on
        # `_speculation_depth_auto`).
        _LOG.warning(
            "speculation depth %d -> 0: median evaluation %.3gs is only %.2g%% of a median build "
            "(%.3gs), so a prefetch has no provider latency to overlap. Recorded as "
            "speculation_depth_settled in the event log — a ONE-WAY ratchet for THIS run, which "
            "replay and resume both reproduce, so no resume flag lifts it. To keep the prefetch on, "
            "LAUNCH a run with the depth spelled (`-s speculation_depth=%d`): a spelled depth is "
            "never settled away.",
            current, eval_median, ratio * 100.0, build_median, current)
        return True

    @staticmethod
    def _proposal_authority_seq(events: list) -> int:
        """Latest selection-authority seq, ignoring everything that carries no selection authority.

        ONE CALLER SINCE 2026-08-20, and the scope matters: `card_reservation.py::_reserve_node_build`
        compares this for EQUALITY across the CAS RETRIES of one reservation, inside `_id_lock`. That
        window is microseconds long and nothing paid is at risk in it — a retry just re-plans.

        IT NO LONGER FENCES A PAID PROPOSAL. `_stage_prepared_card` used to take it and compare it
        across the whole slow `_prepare_node_idea` call, and that was the wrong comparison rather
        than the wrong exclusion list: "nothing at all happened" is a strict superset of "nothing
        this proposal's receipt asserts moved", and the difference is exactly the concurrent research
        task and the node terminals — the rows a multi-minute window is CERTAIN to contain. Measured
        over `/var/tmp/looplab-bench/runs-armb` (20 runs, 2026-08-20): 56 isolated raw proposals, 56
        discards, 0 Cards, $3.89, with `research_attempted` inside 100 % of the windows. Twice before
        this the answer was to widen the list below (the two LLM rows -> `DIAGNOSTIC_EVENTS` ->
        `SETUP_THREAD_APPENDABLE`); the list was never the defect. See
        `card_reservation.py::_proposal_receipt_fence` for what replaced it and what still refuses.
        `train_monitor_alert` and the two ASHA rows are ON by default and fire on a TIMER from
        concurrent evals, so they land in that window as a matter of course; measured, each moves the
        fence 1 -> 2. `deps_installed` and `full_retrain_charged` do the same from the attempt loop.
        None of them can change which action the policy would choose — that is what makes a
        `DIAGNOSTIC_EVENT` diagnostic, and it is the property this fence actually needs.

        This also retires a claim written in `types.py` and in CLAUDE.md's invariant 1: that a
        fold-ignored event is splice-neutral BY CONSTRUCTION. The FOLD is not the only reader. It was
        true of the fold and false of this fence, and a diagnostic row was silently costing paid
        proposals before this list was widened.

        `SETUP_THREAD_APPENDABLE` is the ONE folded pair excluded here, and the reason is not that it
        is convenient — it is that this is the only folded pair in the repo whose splice-position
        neutrality has been PROVEN (`tests/test_setup_thread_appendable.py`), because the fold keys
        `run_setup_open`/`run_setup_done` purely BY COMMAND: never by position, node or ordering
        against any other event. Neither can change which action the policy would choose, which is
        the property this fence actually needs. It became reachable when backlog F1f made the outer
        loop turn while adopted evaluations run: the pair is written from an eval WORKER THREAD and
        is therefore the only authority-bearing row that can land inside a main-task reservation's
        CAS window. **This is deliberately NOT a precedent for widening the set to node terminals.**
        A `node_evaluated` moves `best`, the parent snapshot and every Card score — it carries
        selection authority, which is exactly what the fence is for.
        """

        return max(
            (
                event.seq for event in events
                if event.type not in DIAGNOSTIC_EVENTS
                and event.type not in SETUP_THREAD_APPENDABLE
                and event.type not in {EV_LLM_USAGE, EV_LLM_COST}
                and type(event.seq) is int
            ),
            default=-1,
        )

    # The per-tail fold memo behind `_fold_current`.  A CLASS-level default so every entry point —
    # a session turn, the outer spine, a focused test calling one helper directly — shares one memo
    # without an initialization-order dependency; the first real fold binds an instance attribute.
    _spec_fold_memo: Optional[tuple[Any, int, int, RunState]] = None

    def _fold_current(self) -> tuple[list, RunState]:
        """Read the log and fold it, REUSING the previous fold while the tail has not moved.

        This is the caching the review annotation in `_run_card_session` asked for (doc 25 EC-02).
        Measured before it existed: ONE idle polling turn of the session rebuilt the entire RunState
        — every Card, every concept — nine times over byte-identical input (six with no request head
        outstanding), and did it again every 0.5s poll for the whole life of a long evaluation.

        Why this does not violate engine invariant 4 ("state is only observed via
        `fold(store.read_all())` — never cache derived state across loop iterations without
        re-folding"): the log is STILL read on every call, and the memo is consulted only when the
        freshly read prefix is unchanged in the only two ways an append-only log can change — its
        length, and its last logical sequence.  That pair is the same identity
        `EventStore.append(expected_last_seq=...)` already trusts to decide whether a caller's view
        of the log is current, so a hit is not derived state carried across a turn; it is the pure
        function `fold` not being recomputed on an input it has already seen.  An append by ANY
        writer — this task, an eval worker, the research task, an operator through the UI — moves the
        tail and forces a real fold on the very next call.  That is what makes "a phase that appends
        re-folds before the next phase reads" mechanical here instead of a discipline each phase has
        to remember.

        The memo also keys on the fold callable ITSELF, and it has to key on the one that will RUN.
        Tests steer what the Engine sees by swapping `orchestrator.fold` (the seam
        `shared.py::engine_fold` resolves at call time, `tests/test_engine_fold_seam.py`), and a memo
        that outlived the swap would serve the previous function's answer to the new one — the "test
        still runs but no longer measures anything" failure CLAUDE.md warns about. This used to key
        on this module's own `fold` and call `speculation.fold` a documented patch seam; no test
        patches that name, and since step 0 it is `engine_fold` — one stable object — so the key
        never moved when the real seam did (review 2026-09-22, ES1-08). The key is now the PAIR:
        this module's name (a direct patch of it) and the target it resolves to (a patch of the
        seam).

        A folded `RunState` served from this memo is treated as immutable by every consumer, and this
        session already hands ONE folded state to a background research task that outlives the turn.
        (It said "no engine or search module assigns to one of its attributes"; one does —
        `evaluate.py`'s workdir phase clears `a.node.rerun_stage` in place — but on the attempt's
        PRIVATE fold, `_eval_admit`'s own, never on a state this memo hands out; review 2026-09-22,
        ENG1-15.)  Sharing the
        object between two readers of the same tail is therefore exactly the guarantee two equal
        copies gave.  Written only from the MAIN task (the worker-thread folds in
        `_producer_card_reservation` / `_prepare_raw_card_stage` deliberately do not go through here).
        """

        events = self.store.read_all()
        tail = events[-1].seq if events else -1
        # Deferred for the reason `engine_fold`'s own import is: a module-level binding would
        # snapshot the seam's target and make a patch of it invisible again.
        from looplab.engine import orchestrator as _seam
        key = (fold, _seam.fold)
        memo = self._spec_fold_memo
        if memo is not None and memo[0] == key and memo[1] == tail and memo[2] == len(events):
            return events, memo[3]
        state = fold(events)
        self._spec_fold_memo = (key, tail, len(events), state)
        return events, state

    def _session_state(self) -> RunState:
        """`_fold_current` for the callers that need only the folded half."""

        return self._fold_current()[1]

    def _session_gates(self, state: RunState, session: CardSession) -> CardSessionGates:
        """The one computation of a turn's three fold-derived stop conditions, from ONE snapshot.

        A RUN STOP HELD FOR THE OWNER is a terminal intent here (review 2026-09-22): a spend ceiling
        an adopted evaluation or a Card producer parked on `_eval_budget_stop` ends the run as soon
        as the run loop's head raises it, so no turn before that may start a producer, commit a build
        or admit an evaluation. It closes the race between a producer parking the stop and the
        session's next turn transferring the boundary debt, in which a head with neither a live
        producer nor a result used to read as a dead producer's and close `producer_failed`.
        """

        return CardSessionGates(
            terminal_gate=self._terminal_intent(state) or self._eval_budget_stop is not None,
            budget_exhausted=session.budget_exhausted(state),
            outer_rebuild=any(needs_outer_rebuild(node) for node in state.pending_nodes()),
        )

    def _session_admissible(self, node, state: RunState, session: CardSession) -> bool:
        return bool(
            node.id not in {node_id for node_id, _generation in session.eval_inflight}
            and not self._developer_sentinel(node)
            and node.id not in state.aborted_nodes
            and not needs_outer_rebuild(node)
            # A speculative node is not consumer-owned until the matching durable done-link
            # exists. If its append raced, crash recovery keeps retrying the request head first.
            and (
                not node.speculative
                or node.attempt != 0
                or self._speculative_link_matches(state, node)
            )
        )

    def _ensure_speculation_state(self) -> None:
        # Focused tests often construct Engine through __new__; keep every live-only field lazy.
        if not hasattr(self, "_spec_builds"):
            self._spec_builds: dict[tuple[str, int], SpecBuildResult] = {}
        if not hasattr(self, "_spec_build_inflight"):
            self._spec_build_inflight: set[tuple[str, int]] = set()
        if not hasattr(self, "_spec_role_pair"):
            self._spec_role_pair: Optional[tuple[Any, Any]] = None
        if not hasattr(self, "_spec_role_pairs"):
            # Pairs 2..N of the producer pool (pair 1 is `_spec_role_pair`), and which producer holds
            # each: a request key for a build, "raw" for the raw-proposal lane.
            self._spec_role_pairs: list[tuple[Any, Any]] = []
        if not hasattr(self, "_spec_pair_leases"):
            self._spec_pair_leases: dict[object, int] = {}
        if not hasattr(self, "_spec_adopted"):
            # Build keys whose producer runs in the RUN-scoped group (width > 1) and so outlives
            # the session that started it, like an adopted evaluation.
            self._spec_adopted: set[tuple[str, int]] = set()
        if not hasattr(self, "_card_boundary_debt"):
            self._card_boundary_debt = False
        if not hasattr(self, "_spec_builder_generation"):
            # Bumped when the Strategist swaps the Developer backend (which drops every lease): a
            # build started under an older generation is never committed (`_serve_card_builds`).
            self._spec_builder_generation = 0
        if not hasattr(self, "_spec_raw_adopted"):
            # The raw proposal in flight runs in the run-scoped group (width > 1), not the session's.
            self._spec_raw_adopted = False
        if not hasattr(self, "_spec_reusable"):
            # Finished builds closed `not_selected_now` (width > 1), kept for a re-election of the
            # same Card at the same epoch (`_serve_card_builds`, `_start_request_producer`).
            self._spec_reusable: dict[tuple[str, int], SpecBuildResult] = {}
        if not hasattr(self, "_spec_request_builder"):
            self._spec_request_builder: dict[tuple[str, int], int] = {}
        if not hasattr(self, "_spec_raw_stage_inflight"):
            self._spec_raw_stage_inflight = False
        if not hasattr(self, "_spec_raw_stage_result"):
            self._spec_raw_stage_result: Optional[SpecRawStageResult] = None
        if not hasattr(self, "_spec_force_outer"):
            self._spec_force_outer = False
        if not hasattr(self, "_eval_inflight"):
            # RUN-scoped, not session-scoped (F1f).  Every `CardSession` is handed THIS object, so
            # the adopted set survives a session return; `_run_with_llm_broker` reads it to keep a
            # terminal gate, a depth ratchet or a freshness drain from acting as though the log were
            # quiescent while GPUs are still burning.
            self._eval_inflight: set[tuple[int, int]] = set()

    # Run-scoped eval plumbing, CLASS-level so every entry point sees a defined value without an
    # initialization-order dependency (same reasoning as `_spec_fold_memo`).  Only `_eval_inflight`
    # is per-instance, because it is mutable.
    #
    #   `_eval_task_group`     the run-scoped anyio group `_run_with_llm_broker` opens around its
    #                          whole turn loop; `None` outside a run, which makes a direct
    #                          `_run_card_session(...)` call fall back to session-scoped evals.
    #   `_eval_notify`         the CURRENT session's wake-up stream, or `None` between sessions.
    #   `_eval_boundary_owed`  set by an eval child's `finally`; consumed by the next session turn.
    #                          A BOOL, so it means "at least one terminal landed", not "one turn per
    #                          terminal" — see `_card_eval_one`'s `finally` for the three reasons
    #                          that is left as it is, and for what would change if it were not.
    #   `_eval_drain_requested`  set by a terminal gate that refused to finish over live evals; the
    #                          run loop drains on its next turn and the gate then succeeds.  A
    #                          FLAG rather than an inline wait because the gates are sync helpers
    #                          (`_finish_if_quiescent`, `_finish_with_report_if_quiescent`) reached
    #                          from five call sites, and making them async to hold one `await`
    #                          would move the finish contract instead of guarding it.
    #   `_eval_budget_stop`    the spend ceiling an adopted evaluation DEFERRED instead of raising it
    #                          into the run-scoped group (review 2026-09-22, ENG2-02) — the FIRST one;
    #                          admission refuses while it is held and `_raise_deferred_eval_budget_stop`
    #                          raises it once every sibling has landed.  `None`: nothing captured.
    #                          It is the RUN-LEVEL deferred-stop sink, so it also holds the run's
    #                          other ending that surfaces inside one evaluation — a refused run
    #                          setup (`RunSetupRefusal`, ENG2-08) — under the same rules.
    _eval_task_group: Any = None
    _eval_notify: Any = None
    _eval_boundary_owed: bool = False
    _eval_drain_requested: bool = False
    _eval_budget_stop: Optional[BaseException] = None
    #   `_outer_boundary_served_tail`  the log seq at which a session last handed back for a
    #                          RECURRING producer yield, so the same unchanged condition cannot hand
    #                          back again — see `_card_phase_decide_exit`'s rate-limit clause.
    _outer_boundary_served_tail: int = -2

    def _evals_inflight(self) -> bool:
        """Is any adopted evaluation still running in this process?

        The IN-MEMORY half only, and deliberately so: it answers "may this main-task decision assume
        a quiescent log?", which is a question about THIS process.  The durable half — "did a
        previous process leave an eval mid-training?" — is `node_eval_started`, and
        `_drop_stale_speculation` is where that one is read.
        """

        return bool(getattr(self, "_eval_inflight", ()))

    async def _drain_adopted_evals(self) -> None:
        """Wait until no adopted evaluation is running in this process.

        The ONE place the run pays a real barrier, and it pays it where the barrier is free: every
        caller is already committed to stopping work (a terminal gate that wants to append
        `run_finished`, or the run loop on its way into `finalize_run`).  There is no GPU to idle —
        the run is ending — and the alternative is finalization computing a champion, a budget
        summary and a paid report over a node that has not reported its metric yet.

        Deliberately a poll and not a join: the task group is owned by `Engine.run`, so this cannot
        `await` it, and every child clears its own `_eval_inflight` entry in a `finally` that runs
        even under cancellation.  `_eval_notify` is the CURRENT session's stream and there is no
        session here, so a wake-up channel would have to be invented for a wait that happens at most
        once per run.
        """

        self._eval_drain_requested = False
        # …and every ADOPTED build (width > 1): it outlives sessions the way an evaluation does,
        # and a run that finalizes or raises its ceiling over one would lose paid work or leave its
        # worker behind the teardown (`abandon_on_cancel=False`).
        adopted = getattr(self, "_adopted_producers", None)   # absent on eval-only stub hosts
        while (self._evals_inflight() or (adopted is not None and adopted())
               or (getattr(self, "_spec_raw_adopted", False)
                   and getattr(self, "_spec_raw_stage_inflight", False))):
            await anyio.sleep(0.05)

    async def _raise_deferred_eval_budget_stop(self) -> None:
        """Raise the spend ceiling an adopted evaluation DEFERRED — once every sibling has landed.

        THE OWNER'S HALF of the eval-child deferral (review 2026-09-22, ENG2-02); the child's half is
        `_card_eval_one`.  A ceiling crossed by one evaluation's own paid bookkeeping used to leave
        its child task into the RUN-scoped eval group, which cancels every sibling at its next
        checkpoint — after the sandbox wrote the score, before the terminal — and `Engine.run`'s
        drain, entered inside that already-cancelled scope, could not wait for any of them.  The
        child now parks the stop on `_eval_budget_stop` and returns; `_card_phase_admit_evals`
        admits nothing while it is held; and the run loop calls this at the head of every turn and
        after its final drain, so the stop is raised AFTER `_drain_adopted_evals` and the siblings
        the run had already paid for are in the log it stops over.

        THE STOP IS UNCHANGED in class and sentence — the accountant's own exception object — so
        `cli/run_cmds.py` still records `run_finished {"reason": "budget_exhausted"}`.  Nothing new
        can be bought while it waits: admission is refused, and a paid call inside a draining
        evaluation raises against the same ceiling and is deferred the same way, so the wait is
        bounded by work already started.  Cleared as it is raised, so an Engine that is run again
        does not inherit the stop of a run that already ended on it.

        A REFUSED RUN SETUP is raised from here too (review 2026-09-22, ENG2-08) — the sink holds
        whatever `core/errors.py::deferrable_run_stop` let a child park — and it is equally
        unchanged: the `RunSetupRefusal` an evaluation stopped on, so `cli/run_cmds.py` records
        `run_finished {"reason": "error"}` with the refusal's own sentence, which is the abort the
        guide promises.
        """
        stop = self._eval_budget_stop
        if stop is None:
            return
        await self._drain_adopted_evals()
        self._eval_budget_stop = None
        raise stop

    def _producer_role_pair(self) -> Optional[tuple[Any, Any]]:
        """Lease one non-primary pair from the Layer-2 role pool.

        ``_build_role_pairs(1)`` is intentionally not used: it returns the primary roles whose
        per-build output slots are shared with repairs and ordinary builds.  The surrounding Card
        session never overlaps a normal build batch, so the cached pool pair is exclusively leased
        for the session and can be safely reused by its single producer.

        THE LEASED RESEARCHER IS NOT THE PRIMARY'S STACK, BY DECISION (review 2026-09-22, SCJ-02).
        It carries the primary's FREE wrapper layer — the surrogate, under `surrogate_proposer` /
        `policy=bohb` — and NEITHER PAID one: no foresight panel, no k-NN researcher panel. Under the
        shipped `unified_agent=True` that makes it the bare facade while the primary is
        `ForesightPanelResearcher(UnifiedAgent)`, so a raw-lane proposal gets no predict-before-execute
        and no predicted board order (`_hyp_order`). Wrapping it would be a SPEND change nothing has
        authorised — at least three paid calls per prefetch where this lane pays one, on a prefetch
        the freshness gate discards whenever the board moves — and the ranking could not even be
        recorded: `_prepare_raw_card_stage` runs in a worker, where the board-wide ranking registers
        must not be appended, and discards role telemetry in its `finally`. So `_create_precoded_node`
        reads no researcher telemetry off this pair (the Developer's best-of-N pick it still reads);
        the build producer never proposes, and a value found there would be another proposal's.
        `search/researcher_stack.py` carries the full account and the proof the free layer is free.

        None ANSWERS TWO QUESTIONS, and only one of them is permanent: no `role_factory` is wired,
        or one is and `_build_role_pairs` built no usable pair on this call (it logs why). Nothing is
        cached on a None, so the next call asks the factory again — which is what lets
        `_start_head_producer` retry an open head instead of closing it (review 2026-09-22,
        ENG1-14).
        """

        self._ensure_speculation_state()
        if self._spec_role_pair is not None:
            return self._spec_role_pair
        if getattr(self, "role_factory", None) is None:
            return None
        pairs = self._build_role_pairs(2)
        if len(pairs) < 2:
            return None
        pair = pairs[1]
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or pair[0] is getattr(self, "researcher", None)
            or pair[1] is getattr(self, "developer", None)
        ):
            return None
        self._spec_role_pair = pair
        return pair

    def _speculative_producer_width(self, state: RunState) -> int:
        """How many speculative Card producers may run at once — the run's BUILD width.

        `llm_parallel` is the documented "concurrent node BUILDS" axis (docs/configuration.md, the
        widths table); until 2026-09-24 the Card session ignored it and ran exactly one producer, so
        a Card-mode run built one node at a time whatever it was given. Measured on a one-GPU
        MiniOneRec run: builds of 40 min–3 h one after another against 48-second evaluations, the GPU
        busy 0.74% of 12.7 h.

        THREE RULES keep every existing treatment where it was.
        * AUTO stays one producer. A launch that did not spell a build width (`llm_parallel=0`,
          the Settings default) resolves it from the eval width, and on a multi-GPU box that would
          silently widen a treatment nobody asked for; the speculation calibration profile pins 1.
        * The width the run LAUNCHED with (its `run_started` pin, else this process's resolution) is
          a ceiling nothing but the operator lifts: the Strategist and proposal re-pins may narrow
          the live `_llm_parallel`, and a Strategist that widens it to 8 must not buy eight
          concurrent Developer sessions the operator never authorised.
        * An operator's `budget_extend` of `llm_parallel`/`parallel_build` is honoured as given."""
        if getattr(self, "_llm_parallel_startup_auto", True):
            return 1
        try:
            live = max(1, int(getattr(self, "_llm_parallel", 1) or 1))
        except (TypeError, ValueError):
            return 1
        overrides = getattr(state, "budget_overrides", None) or {}
        if any(key in overrides for key in ("parallel_build", "llm_parallel")):
            return live
        pin = getattr(state, "llm_parallel", 0)
        launched = (pin if type(pin) is int and pin >= 1
                    else getattr(self, "_llm_parallel_launched", 1))
        try:
            return max(1, min(live, int(launched)))
        except (TypeError, ValueError):
            return 1

    def _producer_pair_for(self, holder: object, width: int) -> Optional[tuple[Any, Any]]:
        """Lease a free isolated pair to `holder` (a request key, or "raw"), or None.

        Pair 1 is `_producer_role_pair` — at width 1 it is the only pair and this returns it, exactly
        as the single producer always used it. Wider, pairs 2..N are built once from the same Layer-2
        pool (`_build_role_pairs(N+1)[1:]`: never the primary pair, whose output slots belong to
        repairs and ordinary builds) and each is held by one producer at a time, because a pair's
        Developer carries per-build output slots (`last_files`) two concurrent builds would cross."""
        self._ensure_speculation_state()
        if holder in self._spec_pair_leases:
            index = self._spec_pair_leases[holder]
            return self._spec_role_pair if index == 0 else self._spec_role_pairs[index - 1]
        if holder == "raw" and width > 1:
            # THE RUN-AHEAD LANE'S OWN PAIR (2026-09-25): pair N+1, never one a build can lease, so
            # a proposal runs while every build producer is busy (`_card_phase_request_build`).
            if self._ensure_producer_pool(width + 1) <= width:
                return None
            self._spec_pair_leases[holder] = width
            return self._spec_role_pairs[width - 1]
        pool = self._ensure_producer_pool(width)
        held = set(self._spec_pair_leases.values())
        for index in range(pool):
            if index not in held:
                self._spec_pair_leases[holder] = index
                return self._spec_role_pair if index == 0 else self._spec_role_pairs[index - 1]
        return None

    def _ensure_producer_pool(self, width: int) -> int:
        """Build pairs up to `width` (pair 1 first, the historical single pair) and return how many
        the session can use now — 0 when not even the first could be built. A pool the factory could
        only partly build caps the producer count at what exists; the next call asks again."""
        self._ensure_speculation_state()
        first = self._producer_role_pair()
        if first is None:
            return 0
        if width > 1 and len(self._spec_role_pairs) < width - 1:
            pairs = self._build_role_pairs(width + 1)
            extra = [pair for pair in pairs[2:]
                     if isinstance(pair, tuple) and len(pair) == 2
                     and pair[0] is not getattr(self, "researcher", None)
                     and pair[1] is not getattr(self, "developer", None)
                     and pair is not first]
            if len(extra) > len(self._spec_role_pairs):
                self._spec_role_pairs = extra[:width - 1]
        return min(max(1, width), 1 + len(self._spec_role_pairs))

    def _producer_capacity(self, state: RunState) -> int:
        """Producers the session may run now: the build width, capped by the pairs that exist — and
        never below one. With no pair at all the single-producer path decides what happens (the
        election declines, the raw lane yields to the outer loop), exactly as it always did; a zero
        here would short-circuit both and leave the session unable to hand back mid-eval."""
        return max(1, self._ensure_producer_pool(self._speculative_producer_width(state)))

    def _release_producer_pair(self, holder: object) -> None:
        self._ensure_speculation_state()
        self._spec_pair_leases.pop(holder, None)

    def _adopted_producers(self) -> set:
        self._ensure_speculation_state()
        return set(self._spec_adopted) & set(self._spec_build_inflight)

    def _drop_producer_pool(self) -> None:
        """Forget every pooled producer pair and lease, and retire the builds running on them.

        Called where the Strategist swaps the Developer backend (`strategy.py`). A single producer
        could only be leased inside a session, and the swap runs between sessions, so nulling
        `_spec_role_pair` was enough. Pairs 2..N and producers adopted into the run-scoped group
        outlive the session, so they are dropped here too, and the generation bump makes
        `_serve_card_builds` close — never commit — a result built by the retired backend."""
        self._ensure_speculation_state()
        self._spec_role_pair = None
        self._spec_role_pairs = []
        self._spec_pair_leases = {}
        self._spec_builder_generation += 1
        for key in list(getattr(self, "_spec_reusable", {})):
            self._discard_spec_result(self._spec_reusable.pop(key, None))

    def _busy_producers(self, state: RunState) -> int:
        """Producers occupied now: every open request (built, building or waiting for a pair), every
        build still running for a request already closed, and the raw-proposal lane."""
        self._ensure_speculation_state()
        keys = {key for request in self._outstanding_requests(state)
                if (key := self._request_key(request)) is not None}
        keys |= set(self._spec_build_inflight)
        if self._speculative_producer_width(state) > 1:
            return len(keys)            # the run-ahead proposal lane holds no build slot
        raw = bool(self._spec_raw_stage_inflight or self._spec_raw_stage_result is not None)
        return len(keys) + int(raw)

    @staticmethod
    def _request_key(request: object) -> Optional[tuple[str, int]]:
        if not isinstance(request, Mapping):
            return None
        card_id = request.get("card_id")
        generation = request.get("generation")
        if (
            not isinstance(card_id, str)
            or not card_id
            or type(generation) is not int
            or generation < 0
        ):
            return None
        return card_id, generation

    @staticmethod
    def _outstanding_positions(state: RunState) -> list[tuple[int, dict]]:
        """Every open request with its queue POSITION, in position order. With one producer only the
        head is ever open; with several, a position closed ahead of the cursor is skipped."""
        done = max(0, min(int(state.card_builds_done), len(state.card_build_requests)))
        closed_ahead = set(getattr(state, "card_builds_done_ahead", None) or ())
        return [(index, dict(request))
                for index, request in enumerate(state.card_build_requests)
                if index >= done and index not in closed_ahead and isinstance(request, Mapping)]

    @classmethod
    def _outstanding_requests(cls, state: RunState) -> list[dict]:
        return [request for _index, request in cls._outstanding_positions(state)]

    @classmethod
    def _request_position(cls, state: RunState, key) -> Optional[int]:
        """The queue position of the open request with this exact key, or None when it is closed.
        Keys are unique among open requests: the election excludes every Card already requested."""
        if key is None:
            return None
        return next((index for index, request in cls._outstanding_positions(state)
                     if cls._request_key(request) == key), None)

    @classmethod
    def _head_request(cls, state: RunState) -> Optional[dict]:
        outstanding = cls._outstanding_requests(state)
        return outstanding[0] if outstanding else None

    @staticmethod
    def _developer_sentinel(node) -> bool:
        # BOTH SPELLINGS. `empty_build_refusal` moved from `(developer error:` to
        # `(developer stuck:` on 2026-08-28 (c11251a1) and only `engine/orchestrator.py` was taught
        # the new one, so this predicate stopped recognising a build that wrote nothing. Measured
        # the same morning on dsFix1 node 2: the refusal fired, this returned False, the sentinel
        # fell through as if it were solution code, and the engine committed a node with
        # `files: {}` and spent 36.1 s evaluating the untouched `raise NotImplementedError`
        # template for a 0.0 -- one node slot of three. A sentinel is only as safe as its LEAST
        # aware reader.
        return bool(
            node is not None
            and isinstance(getattr(node, "code", None), str)
            and (is_developer_error(node.code) or is_developer_stuck(node.code))
        )

    @staticmethod
    def _has_exact_developer_pause(
        events,
        *,
        node_id: int,
        generation: int,
        after_seq: int,
    ) -> bool:
        """Whether this exact failed lifecycle has already owned an auto-pause.

        Raw history, rather than folded ``state.paused``, is authoritative: a later resume clears
        the folded pause but must not make recovery append the same scoped pause again. A pause
        before the terminal is not an acknowledgement because replay rejects it while the Node is
        pending, hence the strict sequence boundary.
        """

        return any(
            event.type == EV_PAUSE
            and event.seq > after_seq
            and isinstance(event.data, Mapping)
            and type(event.data.get("node_id")) is int
            and event.data.get("node_id") == node_id
            and type(event.data.get("generation")) is int
            and event.data.get("generation") == generation
            for event in events
        )

    def _resource_envelope(self) -> CardResourceEnvelope:
        ids = list(getattr(self, "_gpu_ids", []) or [])
        memory_map = getattr(self, "_gpu_mem", {}) or {}
        memory = tuple(
            int(memory_map[gpu]) for gpu in ids
            if type(memory_map.get(gpu)) is int and memory_map[gpu] >= 0
        )
        return CardResourceEnvelope(
            gpu_count=len(ids),
            gpu_memory_mib=memory if len(memory) == len(ids) else (),
        )

    @staticmethod
    def _speculative_link_matches(state: RunState, node) -> bool:
        if node is None or getattr(node, "speculative", False) is not True:
            return False
        generation = getattr(node, "card_build_generation", None)
        link = state.speculative_nodes.get(node.id)
        return bool(
            node.attempt == 0
            and type(generation) is int
            and isinstance(link, Mapping)
            and link.get("card_id") == node.idea.card_id
            and link.get("generation") == generation
        )

    @classmethod
    def _speculative_pending_nodes(cls, state: RunState) -> list:
        return [
            node for node in state.pending_nodes()
            if cls._speculative_link_matches(state, node)
        ]

    @classmethod
    def _speculation_depth_used(
        cls,
        state: RunState,
        *,
        consumed_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> int:
        """Count prefetched work not already being consumed by this exact eval session.

        The public depth contract counts outstanding requests plus committed/unevaluated speculative
        Nodes.  During the live overlap window, however, a Node already admitted to the consumer is no
        longer prefetch inventory: retaining it in the count makes depth=1 strictly serial.  Subtract
        only exact ``(id, attempt)`` pairs whose Nodes also carry the durable speculative marker+done
        link; arbitrary pending ids can never relax the outer or resume gate.
        """

        consumed = {
            key for key in consumed_inflight
            if (isinstance(key, tuple) and len(key) == 2
                and type(key[0]) is int and type(key[1]) is int)
        }
        pending = sum(
            1 for node in cls._speculative_pending_nodes(state)
            if (node.id, node.attempt) not in consumed
        )
        return len(cls._outstanding_requests(state)) + pending

    @classmethod
    def _unadmitted_prefetch(
        cls,
        state: RunState,
        *,
        consumed_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> int:
        """Committed speculative Nodes no evaluation has taken yet — the INVENTORY half of
        `_speculation_depth_used`, without the requests still being built.

        The election weighs the two halves separately (review 2026-09-24): the ceiling bounds what
        is HELD, because that is what the freshness gate discards, while how many builds run at once
        is the producer width. With one producer an election needs no open request at all, so this
        is exactly the number the single-producer election compared."""
        return cls._speculation_depth_used(
            state, consumed_inflight=consumed_inflight,
        ) - len(cls._outstanding_requests(state))

    @classmethod
    def _prefetch_supply_used(
        cls,
        state: RunState,
        *,
        consumed_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> int:
        """What the PRODUCING gates must weigh: outstanding work plus the inventory already staged.

        Separate from `_speculation_depth_used` because the ceiling answers two different questions
        and only one of them may count the board. The consuming gate (`_request_card_build`) asks
        "may I turn an existing Card into a build?" — charging it for the very Card it is about to
        consume makes consumption impossible, which a first version of this fix did: six tests in
        `test_card_speculation_engine.py` went red on `_request_card_build() is False`, and they were
        right. The producing gates ask "may I buy MORE?", and there the board is exactly the thing
        that should stop them.

        THE BUG THIS CLOSES. The raw lane stages a CARD and deliberately owns no Node slot ("Author
        their concrete Ideas and durable Cards now, but deliberately leave every Node slot unowned"),
        while `_speculation_depth_used` counts outstanding requests plus speculative NODES. The thing
        the gate bought was invisible to the number that limits the buying. Measured live on
        `runs/e5small-dr-unified-v4`: it returned 1 against a pinned ceiling of 2 while 88 cards sat
        unbuilt — `1 < 2` on every turn, without end. Nothing drained them either: while any node is
        pending, `card_next_actions` returns `forced_card_actions` (an `evaluate` naming the running
        node) and never reaches card SELECTION, so a staged card could not become the Node the
        counter was waiting for. Minting stayed legal precisely because consuming was impossible.
        """
        return cls._speculation_depth_used(
            state, consumed_inflight=consumed_inflight,
        ) + unconsumed_card_inventory(state, exclude=cls._speculative_card_ids(state))

    def _speculative_prefetch_ceiling(self) -> int:
        """How many unconsumed prefetches this run may HOLD — the depth, narrowed by the lane width.

        THE TWO CEILINGS WERE DIFFERENT NUMBERS AND ONLY ONE OF THEM DECIDED ANYTHING.  AUTO depth is
        the settled eval width ("one speculative prefetch per concurrent evaluation lane",
        `_resolve_speculation_depth`), because a prefetch exists to have a node ready when a GPU lane
        frees.  But what the FRESHNESS gate keeps is set MEMBERSHIP in
        `speculative_card_selection_set`, and that set is `card_lane_width` wide — 1 for `greedy`,
        which `speculation_gate.py` makes the only policy speculation ever runs under.  So on a
        two-lane box the election was licensed to buy two prefetches while the gate was contractually
        obliged to keep one, and `_drop_stale_speculation` terminalized the loser 0.06 s after its
        `card_build_done` — a full Developer call, never evaluated.

        MEASURED over every run in `runs/` that emits the `node_eval_started` admission receipt
        (`rubert-dr-0805`, `rubert-dr-0807`, v2, v6, v7): an election made while the width-1 lane
        already held an unadmitted prefetch produced 3 builds and superseded 3 of them; elections made
        with the lane empty produced 30 builds and superseded 2 (both genuine — the board moved during
        the build, which is what the gate is FOR).  On `rubertlite-dr-unified-v7` the two losers cost
        ~1 h of Developer wall-clock each and retired their Cards, whose ideas then never ran.

        This narrows only the ELECTION, which decides what happens next.  The discard is a RECORD
        event and its refund stays exactly as deterministic as it was: nothing here is read by
        `_drop_stale_speculation`, by `speculative_card_is_fresh` or by the fold.  Nor does it cost
        overlap — the surplus prefetch was destroyed on arrival, so refusing to buy it removes a
        payment and no inventory.  A freed eval lane admits the held prefetch, which drops it out of
        `_speculation_depth_used`'s unconsumed count, and the very next turn elects again.
        """

        return min(int(self.speculation_depth), card_lane_width(self.policy))

    @classmethod
    def _speculative_card_ids(cls, state: RunState) -> set[str]:
        ids = {
            key[0] for request in cls._outstanding_requests(state)
            if (key := cls._request_key(request)) is not None
        }
        ids.update(
            node.idea.card_id for node in state.pending_nodes()
            if isinstance(node.idea.card_id, str)
        )
        return ids

    @staticmethod
    def _terminal_intent(state: RunState) -> bool:
        return state.halted

    def _discard_spec_result(self, result: Optional[SpecBuildResult]) -> None:
        if result is None or result.roles is None:
            return
        self._discard_node_build_telemetry(
            researcher=result.roles[0], developer=result.roles[1],
        )

    def _discard_orphaned_spec_results(self, state: RunState) -> None:
        """Release role side channels for buffers whose durable request has already closed."""

        self._ensure_speculation_state()
        outstanding = {
            key for request in self._outstanding_requests(state)
            if (key := self._request_key(request)) is not None
        }
        for key in list(self._spec_builds):
            if key not in outstanding:
                self._discard_spec_result(self._spec_builds.pop(key, None))
        for key in list(self._spec_reusable):
            card = state.cards.get(key[0])
            if (key[1] != state.search_epoch or card is None
                    or card.status == "dropped" or card.merged_into is not None):
                self._discard_spec_result(self._spec_reusable.pop(key, None))

    @classmethod
    def _acknowledged_pending_ids(cls, state: RunState) -> set[int]:
        """Pending work owned by the session consumer, not a license to erase it from budget/cadence."""

        return {
            node.id for node in state.pending_nodes()
            if not cls._developer_sentinel(node)
        }

    def _producer_card_reservation(self, request: Mapping[str, Any]):
        """Purely reconstruct the exact requested Card/Idea; append no event."""

        key = self._request_key(request)
        if key is None:
            return None, None, {}
        card_id, generation = key
        events = self.store.read_all()
        state = fold(events)
        if self._request_position(state, key) is None or generation != state.search_epoch:
            return None, None, {}
        card = state.cards.get(card_id)
        if card is None:
            return None, None, {}
        from looplab.search.card_selection import card_action
        action = card_action(card)
        if action is None or action.get(META_CARD_ID) != card_id:
            return None, None, {}
        reservation = self._prepare_existing_card_claim(
            events,
            state,
            action,
            card,
            self._node_id_ceiling(events, state),
        )
        receipt = {}
        registrations = [
            event.data for event in events
            if event.type == EV_CARD_ADDED and event.data.get("id") == card_id
        ]
        if len(registrations) == 1:
            registration = registrations[0]
            # Use the same canonical immutable action projection as the exact claim boundary. Keeping
            # a second hand-written projection here would silently lose proposal provenance whenever
            # either receipt schema gained a field.
            ownership_action = self._card_claim_receipt_action(card)
            expected = card_ownership_receipt(
                card_id, card.seed_statement, ownership_action,
            )
            if (
                expected is not None
                and registration.get("statement") == card.seed_statement
                and registration.get("ownership_receipt") == expected
                and card.identity.action_digest == expected["action_digest"]
            ):
                # The proposal may have happened in an earlier process. Recover provenance only
                # from its unique durable ownership registration; a live role attribute would be
                # both lossy on resume and vulnerable to stale producer state.
                receipt = bounded_cross_run_advisory_receipt(
                    registration.get("cross_run_receipt")
                )
        return action, reservation, receipt

    @in_llm_lane("build")
    def _build_requested_card(
        self,
        request: Mapping[str, Any],
        roles: tuple[Any, Any],
    ) -> SpecBuildResult:
        """Worker-thread producer: compute only, with no folded event writes."""

        key = self._request_key(request)
        if key is None:
            return SpecBuildResult("", 0, {}, False, error="malformed request")
        # ONE named span for the whole producer turn. Without it this work was invisible to every
        # trace consumer, not merely unattributed: the helpers in `core/tracing.py` key off the
        # `_current_tracer` contextvar, which ONLY a live `Tracer.span` sets, and this method runs on
        # a worker thread that `_start_head_producer` spawns from the main loop with no span open —
        # so every `generation()` the Developer opened inside it silently no-opped. Measured on two
        # real runs: the entire Card build vanished from `spans.jsonl` (238 s of one 28-minute run,
        # 21 min of one 37-minute run) while the cost ledger billed every call it made, which is what
        # made `looplab timings` account for ~13% of the wall clock. The serial path has had this
        # since the beginning (`node_build.py::_create_node` opens `create_node`); speculation is
        # the path that never got it, and speculation now ships on.
        # `_op_span` (new_trace=True), not a child span, exactly like `propose` in the sibling
        # producer `_prepare_raw_card_stage`: an `anyio.to_thread` worker inherits a COPY of the
        # spawning context, so a child span would splice a background producer into whatever
        # unrelated operation the main task happened to hold open. No `node_id` either — the node
        # does not exist yet, which is precisely why this cost is run-level and not per-node.
        #
        # It STAYS run-scoped, and a node reaches it through a claim pointing the other way. The id
        # this producer could compute is `_node_id_ceiling`, i.e. a PREDICTION: the authoritative one
        # is re-derived by `_claim_requested_card_build` after this span has already closed, and this
        # build may be refused (stale / budget / superseded) and mint no node at all. What IS true at
        # open is the request's own identity, so the span carries that instead of nothing — until
        # 2026-08-14 it carried an EMPTY attribute map, which left the run's single most expensive
        # trace unaddressable by any key at all (measured on `runs/rubertlite-dr-unified-v7`: three
        # `card_build` traces, 1,312 of the run's 2,637 spans, `attributes={}` on every root).
        # The node names this trace afterwards — see `_create_precoded_node` and
        # `traceview.claimed_build_traces`.
        card_id, build_generation = key
        # …AND THE SAME PHASE-HANDOFF SCOPE the serial build opens (`node_build.py::_create_node`).
        # Without it `run_phase` had no ledger to write to, so a speculative build's plan handed its
        # steps nothing: measured 2026-09-25 on MiniOneRec inf12, 20 plan and 58 plan_step sessions
        # ran under `card_build` with no brief, against 4 and 15 on the serial path.
        from looplab.agents.agent import handoff_scope
        with self._op_span("card_build", card_id=card_id,
                           card_build_generation=build_generation) as span, \
                handoff_scope(enabled=self._phase_handoff_summary):
            # Read the id from the ACTIVE span rather than from the handle: `_op_span` degrades to a
            # null context when no tracer is wired, and a build with no trace must carry no claim.
            build_trace = tracing.current_ids()[0] if span is not None else None
            result = self._produce_requested_card(request, key, roles)
        return (result if not isinstance(build_trace, str) or not build_trace
                else replace(result, build_trace=build_trace))

    def _produce_requested_card(
        self,
        request: Mapping[str, Any],
        key: tuple[str, int],
        roles: tuple[Any, Any],
    ) -> SpecBuildResult:
        """The producer turn itself, split out only so `_build_requested_card` is its traced shell."""

        card_id, generation = key
        researcher, developer = roles
        # The isolated pair is reused sequentially. Clear every per-build side channel before even
        # validating the durable request so a stale predecessor can never annotate this Card.
        self._discard_node_build_telemetry(researcher=researcher, developer=developer)
        action, reservation, cross_run_receipt = self._producer_card_reservation(request)
        if action is None or reservation is None or reservation.idea is None:
            return SpecBuildResult(
                card_id, generation, {}, False, roles=roles,
                error="requested Card is no longer buildable",
            )
        state = reservation.state
        idea = reservation.idea.model_copy(deep=True)
        kind = reservation.kind
        try:
            # THE ENVELOPE (doc 52 row 12): the build's outputs are read off the `DeveloperResult`
            # the call returned, never off the instance afterwards — see `agents/roles.py`.
            if kind == "draft":
                built = self._implement_result(
                    self._directed_idea(idea.model_copy(deep=True), state),
                    developer=developer, state=state)
            elif kind == "merge":
                parents = [state.nodes[node_id] for node_id in reservation.parent_ids]
                directed = self._directed_idea(idea.model_copy(deep=True), state)
                # An ensemble seeds from the primary parent and SEES the others (doc 52 row 18):
                # `co_parents` are the lineages it must recombine, code and traces.
                built = self._implement_result(
                    directed,
                    parents[0] if self._merge_mode == "ensemble" and parents else None,
                    developer=developer, state=state,
                    co_parents=parents[1:] if self._merge_mode == "ensemble" else ())
            elif kind == "debug":
                parent = state.nodes[action["parent_id"]]
                repair = getattr(developer, "repair", None)
                if callable(repair) and parent.error and (
                    parent.code or parent.files or self._repo_spec
                ):
                    error = self._repair_error_context(
                        parent.error_reason, parent.error, state=state, node=parent,
                    )
                    built = self._repair_result(parent, error, state, developer=developer)
                else:
                    built = self._implement_result(
                        self._directed_idea(idea.model_copy(deep=True), state),
                        parent,
                        developer=developer,
                        state=state,
                    )
            else:
                parent = state.nodes[action["parent_id"]]
                built = self._implement_result(
                    self._directed_idea(idea.model_copy(deep=True), state),
                    parent,
                    developer=developer,
                    state=state,
                )
            code = built.code
            idea, finalized = self._finalize_developer_footprint(
                idea, developer, code, footprint=built.last_footprint)
            files = dict(built.last_files)
            deleted = tuple(built.last_deleted)
            return SpecBuildResult(
                card_id=card_id,
                generation=generation,
                action=dict(action),
                success=True,
                idea=idea,
                code=code,
                files=files,
                deleted=deleted,
                footprint_finalized=bool(finalized),
                # This Card may have been authored in an earlier process; its unique durable
                # registration, not the current producer role, owns the advisory provenance.
                cross_run_receipt=cross_run_receipt,
                roles=roles,
            )
        except Exception as exc:  # noqa: BLE001 — one producer failure must become an explicit give-up result
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            # …but NOT the run's spend ceiling (review 2026-09-22, the census' `FUNNEL_BACKLOG`):
            # as a give-up result it became `card_build_done {skipped: "producer_failed"}`, which
            # bars this Card from speculative election for the rest of the run, and the run kept
            # turning until some other paid call raised the stop. It leaves the worker, bare or
            # wrapped, and `_run_isolated_producer` parks it for the run's owner to raise.
            refuse_budget_stop(exc)
            return SpecBuildResult(
                card_id, generation, dict(action), False, roles=roles,
                error=producer_error_text(exc),
            )

    def _research_origin_for_node(self, state: RunState, node_id: int) -> Optional[dict]:
        if not state.research:
            return None
        memo = state.research[-1]
        at_node = memo.get("at_node") if isinstance(memo, Mapping) else None
        if type(at_node) is not int or not at_node <= node_id < at_node + 2:
            return None
        from looplab.core.advisory_payloads import valid_advisory_ref
        memo_id = memo.get("memo_id")
        return {
            "at_node": at_node,
            "trigger": memo.get("trigger"),
            **({"memo_id": memo_id} if valid_advisory_ref(memo_id, "memo") else {}),
        }

    def _create_precoded_node(
        self,
        action: dict,
        reserved,
        result: SpecBuildResult,
        *,
        max_eval_seconds: Optional[float] = None,
    ) -> None:
        """Main-task-only commit of one producer result through the ordinary Node lifecycle."""

        if (
            reserved is None
            or not result.success
            or result.idea is None
            or result.roles is None
            or reserved.card_id != result.card_id
            or result.idea.card_id != result.card_id
            or reserved.kind != action.get("kind")
            or type(result.generation) is not int
            or result.generation < 0
        ):
            if reserved is not None:
                self._fail_reserved_build(
                    node_id=reserved.node_id,
                    card_id=reserved.card_id,
                    generation=0,
                    error="invalid precoded Card result",
                    reason="superseded",
                    # Every discard on this method's path is pre-dispatch by construction: the
                    # producer result is rejected before `_emit_node_created`, or the created node is
                    # closed in the same turn, and no evaluation is ever scheduled for it. Stamp the
                    # durable receipt so the L5 refund is proven, not inferred (a bare reservation
                    # owns no Node row and is simply not refundable — see
                    # `refunded_card_budget_node_ids`).
                    never_evaluated=True,
                )
            if result.roles is not None:
                self._discard_node_build_telemetry(
                    researcher=result.roles[0], developer=result.roles[1],
                )
            return

        researcher, developer = result.roles
        state = reserved.state
        node_id = reserved.node_id
        idea = result.idea.model_copy(deep=True)
        # THE NODE NAMES ITS OWN BUILD. `_build_requested_card`'s `card_build` trace is run-scoped by
        # necessity — it ran before this id was reserved — so the whole Developer construction (plan,
        # stages, every tool call and generation) is unreachable from this node's trace unless
        # something joins the two. This span is where both facts exist at once: the committed
        # `node_id` and the exact trace that produced it. The claim is recorded AFTER the fact and is
        # therefore never a guess; the reading half is `traceview.claimed_build_traces`, and the
        # attribute is deliberately not `node_id` on the build itself (see `_build_requested_card`).
        with self.tracer.span(
                "materialize_node", node_id=node_id, operator=reserved.kind,
                **({"build_trace": result.build_trace} if result.build_trace else {})):
            def _plan(events, tail) -> str:
                latest = fold(events)
                latest_card = latest.cards.get(result.card_id)
                # Separate a GENUINE supersession (epoch bump, abort, the Card itself dropped/merged, or
                # a parent invalidated) from a TRANSIENT freeze (run paused/finished/stopped, or the
                # eval-budget crossed between the node_building CAS and this revalidation). The pre-claim
                # path (_serve_card_builds) preserves the Card for the transient set — a later resume or
                # add_nodes extension rebuilds it — so a mid-build pause/budget crossing must NOT reach
                # _fail_reserved_build's drop_card=True default and permanently card_auto_drop the Card
                # (losing its hypothesis). Only real supersession drops the Card.
                # The parent half is `node_build.py::parent_generations_current` — the one
                # spelling every creation site shares (review 2026-09-22, ENG1-11: this was its last
                # inline copy, a negated `any` over the same four clauses).
                from looplab.engine.node_build import parent_generations_current
                superseded = (
                    latest.search_epoch != result.generation
                    or node_id in latest.aborted_nodes
                    or latest_card is None
                    or latest_card.dropped_reason is not None
                    or latest_card.merged_into is not None
                    or not parent_generations_current(latest, reserved.parent_generations)
                )
                transient = (
                    latest.halted
                    or (
                        max_eval_seconds is not None
                        and latest.total_eval_seconds >= max_eval_seconds
                    )
                )
                if superseded or transient:
                    self._fail_reserved_build(
                        node_id=node_id,
                        card_id=reserved.card_id,
                        generation=0,
                        error=("speculative build became stale before commit" if superseded
                               else "speculative build frozen before commit (pause/stop/budget)"),
                        reason="superseded" if superseded else "frozen",
                        drop_card=superseded,
                        never_evaluated=True,
                    )
                    self._discard_node_build_telemetry(
                        researcher=researcher, developer=developer,
                    )
                    # Already closed by `_fail_reserved_build`: the caller must not fall through to
                    # the post-creation checks, which would fail a node that was never created.
                    return "closed"
                self._emit_node_created(
                    node_id=node_id,
                    parent_ids=list(reserved.parent_ids),
                    operator=idea.operator,
                    idea=durable_idea_payload(idea),
                    code=result.code,
                    files=dict(result.files),
                    deleted=list(result.deleted),
                    research_origin=self._research_origin_for_node(state, node_id),
                    cross_run_receipt=dict(result.cross_run_receipt),
                    **({"parent_generations": reserved.parent_generations}
                       if reserved.parent_generations else {}),
                    **({"footprint_finalized": True}
                       if result.footprint_finalized else {}),
                    speculative=True,
                    card_build_generation=result.generation,
                    # Every new lifecycle carries this public activity boundary. This speculative
                    # subset additionally relies on it for the stricter budget-refund proof: the
                    # admission below appends `node_eval_started` before any sandbox work, and
                    # `is_unevaluated_speculative_discard` refuses a refund without both receipts.
                    # See `events/types.py::EV_NODE_EVAL_STARTED`.
                    eval_start_boundary=True,
                    expected_last_seq=tail,
                )
                return "created"

            # Three outcomes, not two: the plan either created the node, found it already closed by
            # a supersession/freeze it handled itself, or never landed at all.
            outcome = retry_tail_cas(self.store, _plan, on_exhaust=lambda: "lost")
            if outcome == "closed":
                return
            if outcome != "created":
                self._fail_reserved_build(
                    node_id=node_id,
                    card_id=reserved.card_id,
                    generation=0,
                    error="speculative node commit lost its event-tail CAS",
                    reason="superseded",
                    never_evaluated=True,
                )
                self._discard_node_build_telemetry(
                    researcher=researcher, developer=developer,
                )
                return
            created = fold(self.store.read_all()).nodes.get(node_id)
            if (
                created is None
                or created.idea.card_id != result.card_id
                or created.speculative is not True
                or created.card_build_generation != result.generation
            ):
                self._fail_reserved_build(
                    node_id=node_id,
                    card_id=reserved.card_id,
                    generation=0,
                    error="speculative node creation was rejected during replay",
                    reason="superseded",
                    never_evaluated=True,
                )
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            if is_developer_stuck(result.code):
                # The model ran out of moves on THIS card; the run is not in trouble. Terminalize
                # the build and let the next speculative action proceed -- no crash record, no
                # circuit breaker, which is the distinction `core/models.py::DEVELOPER_STUCK_PREFIX` draws and which
                # the crash branch below would erase.
                # `created.attempt`, and NOT a bare `generation`: the only binding of that name in
                # this method was a generator-expression variable a hundred lines up (the parent
                # check, since folded into `parent_generations_current`), which Python 3
                # scopes to the comprehension, so the name was UNBOUND here and this branch raised
                # `NameError` instead of writing the terminal it exists to write. On the shipped
                # default (`card_driven_selection`), a Developer session that produced no code
                # reached it through `empty_build_refusal` and the reserved node got no terminal at
                # all — an unexplained engine crash on the one path the stuck/crash split was added
                # for. `created` is bound thirteen lines up and its `attempt` is the node's real
                # generation, which is what the crash branch below already identifies it by.
                # `never_evaluated`, LIKE EVERY OTHER DISCARD ON THIS METHOD'S PATH — the rule is
                # written a hundred lines up ("the created node is closed in the same turn … Stamp
                # the durable receipt so the L5 refund is PROVEN, not inferred") and this terminal
                # was the one that did not carry it. `is_unevaluated_speculative_discard` failed on
                # that clause alone (the reason is `developer_stuck`, not the `superseded`/freshness
                # pair), so the refund was denied and `max_nodes` was spent on an experiment that
                # never dispatched: no `eval_started`, zero eval seconds, no `stage_finished`. One
                # stuck card per budget slot, repeatedly, on the shipped `card_driven_selection`.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": created.attempt,
                    "error": result.code, "reason": "developer_stuck", "eval_seconds": 0.0,
                    "never_evaluated": True,
                })
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            if is_developer_error(result.code):
                # The terminal and its circuit-breaker are one event-log transaction. A process
                # crash may leave the preceding node_created durable, but can never leave a new
                # developer_crash terminal without its matching pause. Tail CAS keeps a concurrent
                # operator control either wholly before or wholly after the pair.
                def _plan_terminal(terminal_events, tail) -> None:
                    terminal_state = fold(terminal_events)
                    terminal_node = terminal_state.nodes.get(node_id)
                    if (
                        terminal_node is None
                        or terminal_node.attempt != created.attempt
                        or not self._developer_sentinel(terminal_node)
                        or terminal_node.status is not NodeStatus.pending
                    ):
                        return None
                    records = developer_crash_records(
                        node_id, terminal_node.attempt, result.code,
                        "auto-paused: a Developer session crashed (LLM unreachable or a hard "
                        "error, unresolved within the node) — resume once it's fixed",
                    )
                    # `terminal_state` is the exact prefix this CAS appends onto, so the rank the
                    # terminal takes is decidable before it lands: below
                    # `developer_crash_pause_after` the transaction is the terminal alone.
                    pause_due = self._developer_crash_pause_due(terminal_state, node_id)
                    self.store.append_many(records if pause_due else records[:1],
                                           expected_last_seq=tail)
                    if pause_due:
                        self._create_paused = True
                    return None

                # A crash terminal that could not land leaves the node pending: the ordinary
                # crash-repair path still owns it, so exhaustion is simply "not this turn".
                retry_tail_cas(self.store, _plan_terminal, on_exhaust=lambda: None)
        try:
            self._emit_agent_report(node_id, developer=developer)
            # THE DEVELOPER HALF ONLY (review 2026-09-22, SCJ-02). This used to call
            # `_emit_hypothesis_ranked` and both halves of `_emit_foresight_selected` on the pooled
            # researcher, which cannot hold THIS node's ranking: the build producer implements a
            # Card an earlier proposal minted and never proposes (it clears the pair's telemetry
            # first), and the pooled researcher carries no panel that could rank (see
            # `_producer_role_pair`). The reads were dead on every real pair — and on the leased pair
            # a value there would belong to ANOTHER proposal, i.e. a cross-wired receipt. Best-of-N's
            # pick on the pooled Developer is this build's own, so it is still published.
            self._emit_role_telemetry(
                developer, "last_foresight_pick", EV_FORESIGHT_SELECTED, node_id, 0)
        finally:
            # `_emit_agent_report` does not consume `last_report`; make pair reuse explicit.
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)

    def _append_card_build_done(
        self,
        request: Mapping[str, Any],
        *,
        node_id: Optional[int] = None,
        skipped: Optional[str] = None,
        skipped_reason: Optional[str] = None,
    ) -> bool:
        """Close only this exact open request, retrying a moving tail without skipping requests.

        The head closes positionally, byte for byte the row one producer always wrote. Any other
        open request — a build that finished before one opened earlier — names its queue `index`,
        which is what lets the fold close it without advancing past the head still building."""

        key = self._request_key(request)
        if key is None or (node_id is None) == (skipped is None):
            return False
        card_id, generation = key
        if skipped is not None and skipped not in {"producer_failed", "stale"}:
            return False
        payload: dict[str, Any] = {"card_id": card_id, "generation": generation}
        if skipped is not None:
            payload["skipped"] = skipped
            # ADDITIVE and fold-ignored (invariant #5): which of the nine refusals fired. The coarse
            # word stays exactly what it was, so every existing reader is byte-identical on it.
            if isinstance(skipped_reason, str) and skipped_reason.strip():
                payload["skipped_reason"] = skipped_reason.strip()[:64]
        else:
            payload.update({"node_id": node_id, "speculative": True})
        def _plan(events, tail) -> bool:
            state = fold(events)
            position = self._request_position(state, key)
            if position is None:
                # Another main-task path may already have closed it.
                return not self._outstanding_positions(state)
            row = (payload if position == int(state.card_builds_done)
                   else {**payload, "index": position})
            with self._id_lock:
                self.store.append(EV_CARD_BUILD_DONE, row, expected_last_seq=tail)
            return True

        # The head stays open, so the queue is unchanged and the next serve pass re-closes it.
        return retry_tail_cas(self.store, _plan, on_exhaust=lambda: False)

    def _record_card_build_attempt(self, state: RunState,
                                   request: Mapping[str, Any]) -> None:
        """Receipt ONE physical producer start against the current head, before it can call a provider.

        The durable request identifies LOGICAL work ("build this Card at this epoch") and is what
        survives a kill — but it says nothing about whether a provider call for that work was already
        accepted and billed. Recovery therefore used to start a second producer for the same head with
        no evidence that the first had spent anything. This row is that evidence; `_serve_card_builds`
        quarantines a head that carries one from a dead process.

        Best-effort and unlocked: an attempt row is bookkeeping, never a gate the fold advances, so a
        refused append must not block the build. Its absence simply restores the pre-receipt behavior.
        """
        key = self._request_key(request)
        if key is None:
            return
        position = self._request_position(state, key)
        try:
            self.store.append(EV_CARD_BUILD_ATTEMPTED, {
                "card_id": key[0], "generation": key[1],
                # The queue position this request occupies — see `_on_card_build_attempted`.
                "index": int(state.card_builds_done) if position is None else position})
        except Exception:  # noqa: BLE001 — see the docstring: never block a build on its receipt
            pass

    @staticmethod
    def _head_has_unreconciled_attempt(state: RunState,
                                       key: tuple[str, int]) -> bool:
        """Does this open request (the head, or with several producers any open request) already carry a
        producer attempt from a dead process?

        Position-exact on purpose: the same (card_id, generation) can legitimately be re-elected after
        an earlier request for it was closed, and that older — fully reconciled — attempt must not
        quarantine the new head. Callers must first rule out an attempt this process itself started
        (`_spec_build_inflight` / a present `_spec_builds` result).
        """
        position = SpeculationMixin._request_position(state, key)
        index = int(state.card_builds_done) if position is None else position
        return any(
            isinstance(attempt, dict)
            and attempt.get("index") == index
            and attempt.get("card_id") == key[0]
            and attempt.get("generation") == key[1]
            for attempt in state.card_build_attempts
        )

    def _matching_created_speculation(
        self, state: RunState, request: Mapping[str, Any],
    ):
        key = self._request_key(request)
        if key is None:
            return None
        card_id, generation = key
        matches = [
            node for node in state.nodes.values()
            if node.id not in state.speculative_nodes
            and node.idea.card_id == card_id
            and node.speculative is True
            and node.card_build_generation == generation
        ]
        return min(matches, key=lambda node: node.id) if matches else None

    def _speculative_selection_node_limit(
        self,
        state: RunState,
        *,
        consume_request: bool = False,
        request_index: Optional[int] = None,
    ) -> int:
        """Compensate the pure selector for request slots already removed from the live denominator.

        Engine's translated ``policy.max_nodes`` excludes every unmaterialized durable request so the
        Strategist and ordinary selectors cannot advertise an owned slot. The pure speculative selector
        independently subtracts excluded requests (and a claim temporarily reopens its exact head), so
        add those receipts back at this call boundary to avoid charging them twice.

        A claim passes the request it converts here AND to `_refresh_speculation_budget`: that
        denominator no longer charged it, so it is not added back either, and the limit stays the
        one its election used — only the policy object's own ``max_nodes`` moves.
        """

        return max(0, int(self.policy.max_nodes)) + self._unmaterialized_card_reservations(
            state, consume_request=consume_request, request_index=request_index)

    @staticmethod
    def _producer_failed_card_ids(state: RunState) -> set[str]:
        """Replay-accepted give-ups that must next use the serial compatibility path."""

        return {
            card_id for card_id in state.card_build_producer_failed
            if isinstance(card_id, str) and card_id
        }

    def _election_excluded_card_ids(self, state: RunState) -> set[str]:
        """The counterfactual-election exclusion set shared by `_request_card_build` and the freshness
        gate: committed speculative cards UNION durable producer-failed ids. A producer-failed card is
        serial-fallback-only (never speculatively buildable); left in the counterfactual set it would
        outrank the subject (elected first) and falsely supersede a committed speculative node. One
        helper so the three call sites can't drift (each gets a FRESH mutable set to `.discard` from)."""
        excluded = self._speculative_card_ids(state)
        excluded.update(self._producer_failed_card_ids(state))
        return excluded

    def _card_requires_serial_fallback(self, card_id: object) -> bool:
        state = fold(self.store.read_all())
        return bool(
            isinstance(card_id, str)
            and card_id in self._producer_failed_card_ids(state)
        )

    def _request_card_build(
        self,
        *,
        consumed_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> bool:
        """Main-task election + durable compute gate, with all slow scoring outside ``_id_lock``."""

        if not self._speculation_enabled() or self._producer_role_pair() is None:
            return False
        events = self.store.read_all()
        state = fold(events)
        if (
            state.halted
            or self._busy_producers(state) >= self._producer_capacity(state)
            or self._unadmitted_prefetch(state, consumed_inflight=consumed_inflight)
            >= self._speculative_prefetch_ceiling()
        ):
            return False
        self._refresh_speculation_budget(state)
        if self._node_reservation_slots_remaining(state, events=events) < 1:
            return False
        # The node-OPEN floor, before a build is elected: a prefetch is a node cycle bought early,
        # and one the ceiling would discard is not worth requesting (`_refuse_node_open_below_floor`).
        self._refuse_node_open_below_floor("a speculative Card build")
        excluded = self._election_excluded_card_ids(state)
        actions = speculative_card_actions(
            state,
            self.policy,
            self._speculative_selection_node_limit(state),
            context=SpeculativeSelectionContext(
                scoring=getattr(self, "_card_scoring", None),
                excluded_card_ids=excluded,
                ignored_pending_node_ids=self._acknowledged_pending_ids(state),
                resource_envelope=self._resource_envelope(),
            ),
        )
        if not actions:
            return False
        action = actions[0]
        card_id = action.get(META_CARD_ID)
        if not isinstance(card_id, str) or not card_id:
            return False
        tail = events[-1].seq if events else -1
        try:
            # The lock protects only the short CAS append.  Fold, policy and role calls above are all
            # outside it, so a producer/parallel-build/reset stress cannot stall the event loop here.
            with self._id_lock:
                self.store.append(
                    EV_CARD_BUILD_REQUESTED,
                    {"card_id": card_id, "generation": state.search_epoch},
                    expected_last_seq=tail,
                )
            return True
        except EventStoreConcurrencyError:
            return False

    def _claim_requested_card_build(
        self,
        request: Mapping[str, Any],
        result: SpecBuildResult,
        max_eval_seconds: Optional[float] = None,
    ) -> tuple[str, Optional[int]]:
        """Reserve and commit the result of an exact open request; never consult the ready-only serial claim."""

        key = self._request_key(request)
        if key is None or result.key != key or not result.success or result.idea is None:
            return "producer_failed", None
        card_id, generation = key
        events = self.store.read_all()
        state = fold(events)
        position = self._request_position(state, key)
        if position is None:
            return "closed", None
        # SPLIT INTO THREE NAMED REFUSALS, not because the branch was wrong but because the RECORD
        # was: all three wrote the one word `stale`, and on `e5small-dr-unified-v9` three builds
        # worth 41.4M tokens (11.8 % of the run) were discarded with nothing to say which of the
        # nine `stale` exits below fired. Same illness `node_repaired.verified` had one package over
        # (`inert` was an undiagnosed proxy for which bound ended the session), same remedy.
        if generation != state.search_epoch:
            return "stale:search_epoch_rotated", None
        if self._terminal_intent(state):
            return "stale:run_is_stopping", None
        if max_eval_seconds is not None and state.total_eval_seconds >= max_eval_seconds:
            return "stale:eval_budget_exhausted", None
        # The policy is asked with THIS request's slot still free: the question its election asked,
        # before the request existed. Charged here, the last slot made `next_actions` answer
        # "budget spent", the claim chose another Card and the election re-chose this one, forever
        # (`_refresh_speculation_budget` has the measurement).
        self._refresh_speculation_budget(state, consume_request=True, request_index=position)
        # The exact request head already owns one durable future slot. Convert that ownership into
        # node_building without double-charging it, but never cross a ceiling that was already full
        # when the request arrived (legacy/corrupt prefixes remain pending for budget_extend).
        if self._node_reservation_slots_remaining(
            state, events=events, consume_request=True, request_index=position,
        ) < 1:
            return "budget", None
        selection_limit = self._speculative_selection_node_limit(
            state, consume_request=True, request_index=position)
        if card_budget_used(state) >= selection_limit:
            return "stale:selection_limit_reached", None

        excluded = self._election_excluded_card_ids(state)
        # ...but never exclude the exact card being claimed now: its head result is committing, so it
        # must stay selectable even if a prior speculative attempt marked it producer-failed. Discard
        # AFTER the union so the claim wins over the serial-fallback exclusion for this one id.
        excluded.discard(card_id)
        selected_actions = speculative_card_actions(
            state,
            self.policy,
            selection_limit,
            context=SpeculativeSelectionContext(
                scoring=getattr(self, "_card_scoring", None),
                excluded_card_ids=excluded,
                ignored_pending_node_ids=self._acknowledged_pending_ids(state),
                resource_envelope=self._resource_envelope(),
            ),
        )
        selected_action = next(
            (
                action for action in selected_actions
                if action.get(META_CARD_ID) == card_id
            ),
            None,
        )
        # The board MOVED and this card is no longer what selection would choose. Distinct from
        # every other exit here: the build is intact and the engine simply wants something else.
        if selected_action is None:
            return "stale:not_selected_now", None
        card = state.cards.get(card_id)
        if card is None:
            return "stale:card_gone", None
        from looplab.search.card_selection import card_action
        current_action = card_action(card)
        if current_action is None or current_action != result.action:
            return "stale:card_action_changed", None
        commit_action = {
            **current_action,
            **{
                name: value for name, value in selected_action.items()
                if isinstance(name, str) and name.startswith("_") and name != META_CARD_ID
            },
        }
        node_id = self._node_id_ceiling(events, state)
        reservation = self._prepare_existing_card_claim(
            events, state, commit_action, card, node_id,
        )
        if reservation is None or reservation.idea is None:
            return "stale:reservation_refused", None
        if (
            reservation.idea.card_id != result.idea.card_id
            or reservation.idea.operator != result.idea.operator
            or reservation.idea.params != result.idea.params
            or reservation.idea.space != result.idea.space
            or reservation.idea.eval_profile != result.idea.eval_profile
            or reservation.idea.eval_timeout != result.idea.eval_timeout
        ):
            return "stale:idea_changed", None

        tail = events[-1].seq if events else -1
        try:
            with self._id_lock:
                self.store.append(
                    EV_NODE_BUILDING,
                    {
                        "node_id": reservation.node_id,
                        "operator": reservation.kind,
                        "parent_ids": reservation.parent_ids,
                        "card_id": reservation.card_id,
                        "speculative": True,
                        "card_build_generation": generation,
                    },
                    expected_last_seq=tail,
                )
        except EventStoreConcurrencyError:
            # The request is still OPEN, so the slot it owns goes back into the denominator: the
            # credit above is true only once this claim commits or closes, and every other exit
            # does one or the other (`budget` credits nothing, the ceiling being full either way).
            self._refresh_speculation_budget(state, events=events)
            return "retry", None
        if "_scores" in commit_action:
            self.store.append(EV_POLICY_DECISION, {
                "scores": commit_action["_scores"],
                "chosen": commit_action.get("_chosen"),
                "reason": commit_action.get("_reason"),
            })
        self._append_rung_promotion(commit_action)
        try:
            self._create_node(
                commit_action,
                reserved=reservation,
                precoded=result,
                precoded_max_eval_seconds=max_eval_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — a telemetry failure after node_created still means the durable build committed; see below
            # A telemetry failure after node_created still means the durable build committed.  A
            # pre-create exception owns a bare marker and must close it before the request advances.
            latest = fold(self.store.read_all())
            committed = latest.nodes.get(reservation.node_id)
            if (
                committed is not None
                and committed.idea.card_id == card_id
                and committed.speculative is True
                and committed.card_build_generation == generation
            ):
                return "created", committed.id
            if reservation.node_id in latest.buildings:
                self._fail_reserved_build(
                    node_id=reservation.node_id,
                    card_id=reservation.card_id,
                    generation=0,
                    error=producer_error_text(exc, "speculative node commit failed: "),
                    reason="build_interrupted",
                )
            return "stale:commit_failed", None
        committed = fold(self.store.read_all()).nodes.get(reservation.node_id)
        if (
            committed is None
            or committed.idea.card_id != card_id
            or committed.speculative is not True
            or committed.card_build_generation != generation
        ):
            return "stale:commit_not_ours", None
        return "created", committed.id

    def _serve_card_builds(
        self,
        max_eval_seconds: Optional[float] = None,
        *,
        allow_commit: bool = True,
        request: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Crash-recovery-first main-task service of one durable request: the head, or the named open
        request when several producers hold requests at once."""

        self._ensure_speculation_state()
        state = self._session_state()
        if request is None:
            request = self._head_request(state)
        elif self._request_position(state, self._request_key(request)) is None:
            return False
        key = self._request_key(request)
        if request is None or key is None:
            return False
        recovered = self._matching_created_speculation(state, request)
        if recovered is not None:
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return self._append_card_build_done(request, node_id=recovered.id)
        budget_exhausted = bool(
            max_eval_seconds is not None
            and state.total_eval_seconds >= max_eval_seconds
        )
        # A LIVE PRODUCER IS NEVER STRANDED BY `commit_not_allowed` (2026-08-29), and the asymmetry
        # between the four reasons is the whole point. Three of them are facts about the WORLD and a
        # build made for the old one is worth nothing: the epoch rotated, the run is stopping, there
        # is no eval time left to run the node. `commit_not_allowed` is not that. It is
        # `CardSession.open_for_production`, whose own docstring says it answers "may this turn still
        # START PRODUCER work" and whose justification is that "a producer started after a terminal
        # would hold the session open for the whole of its paid provider call" — and that argument is
        # simply false about a producer ALREADY RUNNING. Committing it starts nothing, makes no
        # provider call, and holds the session open for no latency at all.
        #
        # MEASURED on `e5small-dr-unified-v10` (2026-08-29), the first run whose `skipped_reason`
        # could name this: node 2's terminal set `boundary_owed` at 10:25:29, this branch closed
        # card-4's head as `stale`/`commit_not_allowed`, and the producer went on running and CLOSED
        # ITS SPAN AT 10:27:59 — 38.4 min, 248 provider calls, 12,112,124 tokens, 3.2 % of the whole
        # run — one second before `card_build_requested` asked for the identical card again at
        # 10:28:00. All four of that run's committed builds closed their span at or before their
        # `card_build_done`; card-4 is the only one closed out from under a live producer.
        #
        # So the head is LEFT OPEN while `_spec_build_inflight` owns it, which is this file's own
        # rule twenty lines down ("Never strand a live producer: skip while one is in-flight") applied
        # to the one close that did not consult it. Returning False services no head, which is exactly
        # what the caller wants here — `boundary_owed` is asking the session to RETURN, and the next
        # turn commits the finished build against a fresh authority snapshot.
        #
        # It cannot wedge finalization: `_terminal_intent` is tested BEFORE this and wins the reason
        # ladder, so a stopping run still closes the head as `run_is_stopping` with a producer live.
        commit_refused_this_turn = not allow_commit
        world_moved = (key[1] != state.search_epoch
                       or self._terminal_intent(state)
                       or budget_exhausted)
        # …AND LEFT OPEN WHEN THE BUILD HAS ALREADY FINISHED (2026-09-24). The rule above held the
        # head only while the producer ran; the session then waited the build out (a live producer
        # holds `_card_phase_decide_exit`), the result arrived under the same refused commit, and
        # this branch closed it `commit_not_allowed` — a paid build discarded to buy the outer loop
        # its turn. Measured on a MiniOneRec run: 5 of 19 finished builds. The result now waits in
        # `_spec_builds`, the session returns (`_card_phase_decide_exit` no longer counts a finished
        # result as work to wait for), the outer loop runs its cadences, and the next session commits
        # it through `_claim_requested_card_build`, which re-checks epoch, freshness, budget and the
        # Card itself — the boundary 8d9952a1 asked for is honoured, just not paid for with the build.
        if (commit_refused_this_turn and not world_moved
                and (key in self._spec_build_inflight or key in self._spec_builds)):
            return False
        if world_moved or commit_refused_this_turn:
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return self._append_card_build_done(
                request, skipped="stale",
                skipped_reason=("search_epoch_rotated" if key[1] != state.search_epoch else
                                "run_is_stopping" if self._terminal_intent(state) else
                                "eval_budget_exhausted" if budget_exhausted else "commit_not_allowed"))
        result = self._spec_builds.get(key)
        if result is None:
            # Quarantine before recovery even asks whether the Card is still alive: this head carries a
            # producer attempt that no live in-process producer owns, so a provider call for it may
            # already have been accepted and billed by the process that died. Restarting a producer
            # here would buy the identical Developer/Researcher work a second time with nothing in the
            # log to show for the first. `producer_failed` is the exact disposition wanted — it closes
            # the head, keeps the Card buildable on the SERIAL path, and permanently bars this Card
            # from being speculatively re-elected — and reusing it means the replay vocabulary and the
            # quality denominator stay unchanged. The `card_build_attempted` row is what says WHY.
            if (key not in self._spec_build_inflight
                    and self._head_has_unreconciled_attempt(state, key)):
                closed = self._append_card_build_done(request, skipped="producer_failed")
                if closed:
                    self._spec_force_outer = True
                return closed
            # Crash-recovery wedge: a kill between node_building and node_created leaves the
            # interrupted build's Node id permanently spent (it still counts against the physical ceiling
            # via `_node_id_ceiling`) AND recovery drops its Card, yet the durable request survives at head
            # with no in-memory result. Capacity is then zero, so `_start_head_producer`'s slot gate never
            # starts a producer and this method returns False forever — the session polls indefinitely with
            # `outstanding` still true and no exit boundary reachable. Recognize a head whose Card was
            # dropped or merged (by recovery or an operator) as permanently unbuildable and close it
            # `stale`, so the outstanding request clears and the loop can reach its exit boundary. Never
            # strand a live producer: skip while one is in-flight (its eventual result is released by
            # `_discard_orphaned_spec_results` once the request closes), and leave an ALIVE card's request
            # open so a producer can still be started for it.
            # Two DEAD shapes: a DROPPED Card stays PRESENT with status=="dropped" (its reason MAY be
            # None); a MERGED Card is folded OUT of `state.cards` (ABSENT) and recorded only in its
            # canonical's `aliases` — the fold never assigns `Card.merged_into`, so a merged head
            # resolves via alias membership (a PROVEN merge receipt), not a present `merged_into` row.
            # An absent id that is NOT a known alias is a corrupt/partial chain — leave it open (do not
            # force-close on an unproven receipt), matching the counterfactual path's fail-closed stance.
            card = state.cards.get(key[0])
            merged_away = card is None and key[0] in {
                alias for c in state.cards.values()
                for alias in (getattr(c, "aliases", None) or [])
                if isinstance(alias, str) and alias
            }
            # Key the dropped case on FOLDED status=="dropped", NOT `dropped_reason`: a valid reason-less
            # `card_dropped` folds to status=="dropped" with dropped_reason=None, so a reason-keyed check
            # would leave this head outstanding forever after a crash. Matches the selection guard
            # `_card_administratively_dead`. (`merged_into` stays a defensive disjunct; it is never set.)
            if key not in self._spec_build_inflight and (
                (card is not None
                 and (card.status == "dropped" or card.merged_into is not None))
                or merged_away
            ):
                # THE TWO DEAD SHAPES ARE TWO REASONS, because the comment above already treats
                # them as two facts and a post-mortem reader needs the same split. A DROPPED card is
                # PRESENT and administratively dead — somebody or something ended it, and the build
                # was discarded for a decision made about the card. A MERGED-AWAY card is ABSENT: its
                # work now belongs to a canonical, and the same request against that canonical is
                # the thing to look for. `card_gone` keeps the meaning it already has one function
                # up (`card is None`); `card_dropped` is the new registered word for the other.
                #
                # This close is the one crash-recovery rows land on, i.e. exactly the rows read
                # after the fact — which is why it was the last bare `stale` left and why leaving it
                # bare was worse here than anywhere else.
                return self._append_card_build_done(
                    request, skipped="stale",
                    skipped_reason="card_gone" if merged_away else "card_dropped")
            return False
        if self._spec_request_builder.get(key, self._spec_builder_generation) != (
                self._spec_builder_generation):
            self._discard_spec_result(self._spec_builds.pop(key, None))
            closed = self._append_card_build_done(
                request, skipped="stale", skipped_reason="builder_replaced")
            if closed:
                self._spec_request_builder.pop(key, None)
            return closed
        if not result.success:
            self._discard_spec_result(self._spec_builds.pop(key, None))
            closed = self._append_card_build_done(request, skipped="producer_failed")
            if closed:
                self._spec_force_outer = True
            return closed
        outcome, node_id = self._claim_requested_card_build(
            request, result, max_eval_seconds,
        )
        # The refusal SLUG rides beside the coarse word. `_append_card_build_done` validates the
        # coarse one against its closed vocabulary exactly as before; the slug is additive record.
        outcome, _, stale_reason = outcome.partition(":")
        if outcome == "retry":
            return False
        if outcome == "closed":
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return True
        if outcome == "budget":
            # Keep both the durable head and its isolated result alive. A later add_nodes extension can
            # commit the exact paid result without rebuilding it or acknowledging the request as stale.
            return False
        # The result is dropped only AFTER the close is durable. Popping first meant that when
        # `_append_card_build_done` exhausted its CAS retries the head was left open with no
        # in-memory result and no inflight marker — so the next service turn saw
        # `_head_has_unreconciled_attempt` and closed it as "producer_failed", permanently barring
        # the Card from speculative election even though this process's producer had SUCCEEDED and
        # the claim was merely stale. Keeping the result until the close lands preserves the
        # intended disposition and lets the retry reuse the paid work.
        if outcome == "created" and node_id is not None:
            closed = self._append_card_build_done(request, node_id=node_id)
        else:
            closed = self._append_card_build_done(
                request, skipped="stale", skipped_reason=stale_reason or None)
        if closed:
            result = self._spec_builds.pop(key, None)
            if (stale_reason == "not_selected_now" and result is not None and result.reused):
                # REFUSED TWICE, and the second time as a reuse: the election chose this Card again
                # and its claim refused it again, so they disagree about the board, and keeping the
                # result a third time would make the next cycle as free as this one. That is the
                # shape of the last-slot livelock (2026-09-26: 22 of 50 toy runs spun at ~97 % CPU
                # inside ONE `_run_card_session` call, where the run loop's `no_mint_turns` bound
                # cannot see it). The result is released and the session yields to the outer loop,
                # so a disagreement that persists becomes an outer turn that bound counts.
                self._discard_spec_result(result)
                self._spec_force_outer = True
            elif (stale_reason == "not_selected_now"
                    and self._speculative_producer_width(state) > 1 and result is not None):
                # "the board moved and the build is INTACT" (`CARD_BUILD_SKIP_REASONS`). With several
                # producers the claim and the election can disagree for a turn: measured 2026-09-24
                # on MiniOneRec inf12, card-8's finished build was closed here because a Card staged
                # seconds earlier outranked it, and the freed producer re-elected card-8 in the same
                # turn and rebuilt it from scratch. Keep the result; a re-election of the same Card at
                # the same epoch takes it instead of paying for the build again, and its commit still
                # goes through every check of `_claim_requested_card_build`.
                # Kept ONCE: a result that was already a reuse and is refused again takes the
                # branch above instead.
                self._spec_reusable[key] = result
            else:
                self._discard_spec_result(result)
        return closed

    def _commit_ready_builds_before_cadence(self, state: RunState,
                                            max_eval_seconds: Optional[float]) -> bool:
        """Commit every FINISHED build a session handed back with, before the outer cadence pass.

        A session owed the boundary leaves a finished result in `_spec_builds` and sets
        `_card_boundary_debt`; the outer loop used to run its whole cadence pass first. 8d9952a1's rule
        concerns STARTING work across the boundary, and the claim (`_claim_requested_card_build`)
        re-checks epoch, freshness, budget and the Card, so the commit need not wait (critic review,
        2026-09-25). Not while the run is stopping or an operator's fork/inject waits for the slot.
        True when anything was served."""
        self._ensure_speculation_state()
        if (not getattr(self, "_card_boundary_debt", False) or state.halted
                or self._operator_node_request_ready(state)):
            return False
        served = False
        for _index, request in self._outstanding_positions(state):
            if self._request_key(request) in self._spec_builds:
                served = self._serve_card_builds(
                    max_eval_seconds, allow_commit=True, request=request) or served
        return served

    def _close_card_build_before_terminal_gate(
        self,
        state: RunState,
        max_eval_seconds: Optional[float] = None,
    ) -> bool:
        """Attempt to settle one durable request before a pause/finish decision.

        The return value means a head existed, not that this single CAS attempt succeeded. Callers
        must restart the outer loop either way, so tail churn can never let finalization overtake an
        unacknowledged request. A crash prefix with an already-created Node records the success link;
        every other terminal-gated head is explicitly skipped.
        """

        if not self._speculation_enabled() or self._head_request(state) is None:
            return False
        for _index, request in self._outstanding_positions(state):
            self._serve_card_builds(max_eval_seconds, allow_commit=False, request=request)
        return True

    async def _run_isolated_producer(
        self,
        worker,
        *,
        on_failure,
        store,
        release,
        notify,
        notify_key,
        limiter=None,
    ) -> None:
        """Run ONE isolated producer to a stored result, then release its slot (doc 25 EC-12).

        The two producers differ in their result type and in what "release the slot" means (a key
        discarded from a set beside a superseded result; a bool flag beside role telemetry), so those
        three are callbacks.  What is NOT negotiable is the shape around them, and each clause of it
        fails silently in a different direction if a copy drops it:

        * `abandon_on_cancel=False` makes pause/abort wait for the entire blocking
          Developer/provider call even after the main task has durably closed this request as
          stale. The session's exit gate still counts _spec_build_inflight, so an unavailable
          provider can make an operator stop take the full transport timeout. Use a genuinely
          cancellable producer or quarantine/abandon this isolated role pair after cancellation.
        * a raising worker still STORES a result. The main task advances the durable gate off the
          stored slot, so a producer that stored nothing is indistinguishable from one still running
          — the session would wait out its whole exit gate on a fault that already happened.
        * the release and the notification are in `finally`, in that order. Releasing after the
          wake-up would let the consumer re-scan the slots while the flag still says "inflight".

        The wrapper returns nothing on purpose: the result is reachable only through `store`, which
        is the same durable-slot discipline the main task re-scans.

        THE ONE EXCEPTION TO "a raising worker still stores a result" is the run's spend ceiling
        (review 2026-09-22 — the last two `FUNNEL_BACKLOG` rows of the containment census: the build
        and the raw-proposal worker, which now let it through `refuse_budget_stop`). Stored as a
        give-up it was the Card's fault on the durable record (`producer_failed` bars the Card from
        speculative election) while the run went on turning. So it is PARKED on the run-level
        deferred-stop sink an adopted evaluation parks its ceiling on (`_eval_budget_stop`, first one
        wins — the accountant's own exception, found through any wrapping), the outer boundary is
        owed a turn, and NOTHING is stored: the session's gates read a held stop as a terminal intent
        (`_session_gates`), so it starts no producer, commits no build and closes the open head
        `stale` — never `producer_failed` — and the run loop's head raises the stop
        (`_raise_deferred_eval_budget_stop`) once the adopted evaluations have landed. The release
        and the wake-up below still run.
        """
        try:
            try:
                result = await anyio.to_thread.run_sync(
                    worker, abandon_on_cancel=False, limiter=limiter)
            except Exception as exc:  # noqa: BLE001 — the main task must still advance the durable gate
                stop = budget_stop_leaf(exc)
                if stop is not None:
                    if self._eval_budget_stop is None:
                        self._eval_budget_stop = stop
                    self._eval_boundary_owed = True
                    return
                result = on_failure(exc)
            store(result)
        finally:
            release()
            # Notifications are only hints. Never let a full/closing stream block task-group teardown.
            notify_producer(notify, notify_key)

    async def _produce_card_build(
        self,
        request: Mapping[str, Any],
        roles: tuple[Any, Any],
        notify,
    ) -> None:
        key = self._request_key(request)
        if key is None:
            return

        def _store(result: SpecBuildResult) -> None:
            self._discard_spec_result(self._spec_builds.get(key))
            self._spec_builds[key] = result

        await self._run_isolated_producer(
            # `_start_head_producer` already appended this attempt's `card_build_attempted`
            # receipt, so a kill anywhere below leaves the head quarantined on resume instead
            # of silently re-issuing possibly-charged work (see `_serve_card_builds`).
            functools.partial(self._build_requested_card, dict(request), roles),
            on_failure=lambda exc: SpecBuildResult(
                key[0], key[1], {}, False, roles=roles,
                error=producer_error_text(exc),
            ),
            store=_store,
            release=lambda: (self._spec_build_inflight.discard(key),
                             self._spec_adopted.discard(key),
                             self._release_producer_pair(key)),
            notify=notify,
            notify_key=("producer", key),
            # Its OWN pool, not anyio's shared default -- see `novelty.card_build_limiter` for why
            # one token is the derivation and why it is not the proposal pool.
            limiter=_card_build_limiter(),
        )

    @in_llm_lane("build")
    def _prepare_raw_card_stage(
        self,
        action: Mapping[str, Any],
        proposal_events: list,
        proposal_state: RunState,
        proposal_node_ceiling: int,
        roles: tuple[Any, Any],
    ) -> SpecRawStageResult:
        """Worker-only proposal half: no selection-affecting event may escape this call."""

        raw_action = dict(action)
        generation = proposal_state.search_epoch
        researcher, developer = roles
        source = raw_stage_source(raw_action)
        self._discard_node_build_telemetry(researcher=researcher, developer=developer)
        audit_events: list[tuple[str, dict, Optional[str], Optional[str]]] = []
        try:
            # The Layer-5 speculative producer's proposal. It is a background worker doing a full
            # paid Researcher call, so it is invisible for exactly as long as the foreground one is —
            # and the beacon must NOT ride the `_capture_proposal_events` sink around it, which
            # buffers until the main task publishes. See `SharedEngineMixin._progress` for why a
            # DIAGNOSTIC row may be appended straight from this worker.
            # `_paid_progress`: the comment above already says this is a full paid Researcher
            # call. A beacon alone opens no span, so its money was attributable to nothing.
            with self._paid_progress(PROGRESS_STAGE_BUILD, "propose",
                                     node_id=proposal_node_ceiling, prospective=True,
                                     speculative=True, operator=raw_action.get("kind")), \
                    self._capture_proposal_events() as captured:
                idea = self._prepare_node_idea(
                    raw_action,
                    proposal_state,
                    researcher=researcher,
                    prospective_node_id=proposal_node_ceiling,
                    source=source,
                    proposal_events=proposal_events,
                    drop_repeated_duplicate=True,
                )
                audit_events.extend(captured)
            steering = tuple(getattr(researcher, "_steering_context", []) or [])
            receipt = bounded_cross_run_advisory_receipt(
                getattr(researcher, "_cross_run_advisory_receipt", {}) or {}
            )
            return SpecRawStageResult(
                generation=generation,
                action=raw_action,
                proposal_state=proposal_state,
                proposal_node_ceiling=proposal_node_ceiling,
                at_node=proposal_node_ceiling,
                source=source,
                # `success` is "the paid propose RETURNED" — `SpecRawStageResult.failure` is the only
                # other constructor and it is the RAISED case. A refused proposal (novelty gate, card
                # planner) returns `idea=None` with success, and `_serve_raw_card_stage` names it
                # `proposal_refused` off that pair. This used to be `success=idea is not None`, which
                # sent every refusal down the fault branch: the E2E sweep of 2026-09-23 logged two
                # `novelty_rejected {card_duplicate}` rows and warned "abandoned a prepared proposal:
                # producer_failed" for both, while the provider had answered every call.
                success=True,
                idea=idea,
                steering_context=steering,
                cross_run_receipt=receipt,
                audit_events=tuple(audit_events),
                error="proposal rejected" if idea is None else "",
            )
        except Exception as exc:  # noqa: BLE001 — one raw proposal fault yields a consumed, non-staged result rather than tearing down the task group
            # The run's spend ceiling is not "one raw proposal fault" (review 2026-09-22, the census'
            # `FUNNEL_BACKLOG`): it leaves the worker and `_run_isolated_producer` parks it for the
            # run's owner, rather than becoming a give-up the session then yields around while the
            # run turns on. The `finally` below still discards this turn's telemetry.
            refuse_budget_stop(exc)
            # The intents captured BEFORE the fault ride along: they are already-folded audit rows the
            # main task publishes, and dropping them loses the record of a paid proposal that ran.
            return SpecRawStageResult.failure(
                exc, generation=generation, action=raw_action, proposal_state=proposal_state,
                proposal_node_ceiling=proposal_node_ceiling, source=source,
                audit_events=tuple(audit_events),
            )
        finally:
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)

    async def _produce_raw_card_stage(
        self,
        action: Mapping[str, Any],
        proposal_events: list,
        proposal_state: RunState,
        proposal_node_ceiling: int,
        roles: tuple[Any, Any],
        notify,
    ) -> None:
        def _on_failure(exc: BaseException) -> SpecRawStageResult:
            # Mirror the request-driven producer guard: one raw proposal fault yields a consumed,
            # non-staged result instead of tearing down the task group and cancelling live evals.
            try:
                self._discard_node_build_telemetry(
                    researcher=roles[0], developer=roles[1],
                )
            except Exception:  # noqa: BLE001 — telemetry discard is best-effort inside a failure path
                pass
            # No `audit_events`: this guard fires when the worker never returned, so nothing was
            # buffered here. The worker's own guard carries what it had captured.
            return SpecRawStageResult.failure(
                exc, generation=proposal_state.search_epoch, action=action,
                proposal_state=proposal_state, proposal_node_ceiling=proposal_node_ceiling,
                source=raw_stage_source(action),
            )

        def _store(result: SpecRawStageResult) -> None:
            self._spec_raw_stage_result = result

        def _release() -> None:
            self._release_producer_pair("raw")
            self._spec_raw_adopted = False
            self._spec_raw_stage_inflight = False

        await self._run_isolated_producer(
            functools.partial(
                self._prepare_raw_card_stage,
                dict(action),
                proposal_events,
                proposal_state,
                proposal_node_ceiling,
                roles,
            ),
            on_failure=_on_failure,
            store=_store,
            release=_release,
            notify=notify,
            notify_key=("raw_proposal", proposal_state.search_epoch),
            # The proposal pool, not anyio's default — see `novelty.proposal_limiter`.
            limiter=_proposal_limiter(),
        )

    def _serve_raw_card_stage(self) -> tuple[bool, bool, Optional[str]]:
        """Main-task-only commit of one prepared proposal and its buffered audit intents.

        The third member says WHY a consumed result staged nothing, from the path that knows:
        `RAW_STAGE_PRE_STAGING_REASONS` for the two paths that never reach the stager, the staging
        fence's own `CARD_STAGE_REFUSALS` slug (or `unrecorded`) when `_stage_prepared_card`
        refused, and `None` both for a staged Card and for the attach HANDOFF — which is not an
        abandonment: the serial boundary builds that node (see the attach branch below). The
        caller used to re-derive this by reading `self._card_stage_refusal`, which only
        `_stage_prepared_card` writes — so on the pre-staging paths the attribute still held
        whatever slug the LAST staging call anywhere recorded (the create lane's, possibly turns
        earlier), and a producer crash was warned as e.g. `best_moved`: a specific-looking wrong
        cause an operator then greps the fences for.
        """
        result = self._spec_raw_stage_result
        if result is None:
            return False, False, None
        self._spec_raw_stage_result = None
        if not result.success:
            # PUBLISHED, not dropped. The producer ran a full paid Researcher call under
            # `_capture_proposal_events`; whatever novelty/governance receipts it buffered before it
            # raised describe work that really happened and was really paid for. Until 2026-09-02
            # both of these branches returned without publishing, so the Layer-5 lane was the WORST
            # of the three discard paths: the receipt was captured and then thrown away.
            #
            # This is deliberately NOT the same case as the stale-fence refusal below, and the
            # difference is stated there: a moved fence abandons a SUCCESSFUL proposal that will be
            # re-made from the same state, so dropping keeps the log from carrying two receipts for
            # one eventual card. Here nothing is re-made from anything — the call failed or the
            # planner refused it — and the receipts are the only record that this lane paid.
            self._publish_proposal_events(result.audit_events)
            return True, False, "producer_failed"
        if result.idea is None:
            # The refusal case bd182357 exists for, on the third lane. `_prepare_node_idea`
            # returning None IS the novelty gate or the card planner refusing a paid proposal, and
            # the receipt explaining which is in `audit_events`.
            self._publish_proposal_events(result.audit_events)
            return True, False, "proposal_refused"
        card_id = self._stage_prepared_card(
            result.action,
            result.idea,
            proposal_state=result.proposal_state,
            proposal_node_ceiling=result.proposal_node_ceiling,
            at_node=result.at_node,
            source=result.source,
            steering_context=result.steering_context,
            cross_run_receipt=result.cross_run_receipt,
        )
        if card_id is None:
            if getattr(self, "_card_stage_attached_to", None) is not None:
                # THE ONE REFUSAL THIS LANE CANNOT WAIT OUT. Every other `None` from
                # `_stage_prepared_card` is a moved fence — the epoch, the best anchor, the tail —
                # and re-proposing next turn is the right answer to all of them. An attach refusal is
                # not: the proposal is a repair of a question a live Card already owns, staging can
                # never publish it as inventory, and no amount of waiting changes that. What builds it
                # is the outer boundary — `_card_phase_serve_raw_stage` yields there for any consumed
                # result that staged nothing — i.e. `_handle_create_actions` -> `_create_node` ->
                # `_reserve_node_build(retry_attach=True)`, the one site that can commit an attach.
                #
                # Its audit prefix is COMMITTED rather than dropped, and that is the difference
                # between this branch and the fall-through below it. The proposal really happened and
                # its novelty/governance receipts describe a real paid call; on a stale-fence refusal
                # the whole proposal is being abandoned and re-made, so dropping them keeps the log
                # honest, but here the work is being handed to the serial spine and the receipts are
                # the only record that this lane paid for it at all. No abandon reason for the same
                # reason: handed-on work is not abandoned work.
                self._publish_proposal_events(result.audit_events)
                return True, False, None
            return True, False, str(getattr(self, "_card_stage_refusal", "") or "unrecorded")
        # OPEN[raw-stage-card-and-audit-are-separate-appends] the Card commit above
        # (`card_reservation.py::_stage_prepared_card`: one `card_added`, alone, under its own tail
        # CAS) and these proposal-audit events are separate appends. A crash or append failure after
        # EV_CARD_ADDED leaves an executable durable Card whose novelty/governance audit prefix was
        # silently lost; `_spec_raw_stage_result` was already cleared, so resume cannot repair it.
        # Commit the Card and its bounded audit intents in one tail-fenced append_many, or add a
        # durable proposal receipt plus recovery gate that keeps the Card non-selectable until it
        # is closed. Indexed by review 2026-09-22 (ES1-08); it stays open while the staging commit
        # is that lone append:
        # proof:`present:self.store.append(EV_CARD_ADDED, plan.payload, expected_last_seq=tail)@looplab/engine/card_reservation.py`
        self._publish_proposal_events(result.audit_events)
        return True, True, None

    async def _close_developer_sentinel_once(self) -> bool:
        """Recover one sentinel lifecycle without ever re-pausing an acknowledged crash."""

        events, state = self._fold_current()
        pending = next(
            (candidate for candidate in state.pending_nodes()
             if self._developer_sentinel(candidate)),
            None,
        )
        records: list[tuple[str, dict[str, Any]]]
        if pending is not None:
            node = pending
            if is_developer_stuck(node.code):
                # STUCK IS NOT A CRASH, and the sweep is the one place that conflated them.
                # `_developer_sentinel` recognises BOTH spellings on purpose — it answers "did this
                # build return a sentinel", and a stuck build that lost its terminal to a process
                # death needs recovering exactly as much as a crashed one. What it must not decide
                # is WHICH terminal to write: filing a stuck build as `developer_crash` says "the
                # LLM is unreachable or hit a hard error", auto-pauses the run at the default
                # `developer_crash_pause_after=1` on a provider that was fine, and inflates
                # `developer_crash_rank` so the NEXT real crash mis-computes its own threshold. The
                # live site three hundred lines up draws this distinction ("no crash record, no
                # circuit breaker"); the sweep now writes the same terminal it would have.
                records = [(EV_NODE_FAILED, {
                    "node_id": node.id, "generation": node.attempt, "error": node.code,
                    "reason": "developer_stuck", "eval_seconds": 0.0, "never_evaluated": True,
                })]
            else:
                records = developer_crash_records(
                    node.id, node.attempt, node.code,
                    "auto-paused: recovered a Developer crash before GPU dispatch")
                # The terminal this sweep appends takes the next crash rank; below the run's
                # `developer_crash_pause_after` it owns no pause, exactly as the live sites decide.
                pause_due = self._developer_crash_pause_due(state, node.id)
                if not pause_due:
                    records = records[:1]
        else:
            # A legacy writer (or a crash in the old two-append path) may already have made the
            # sentinel terminal while losing only its pause. Folded ``paused`` cannot distinguish
            # that gap from a pause which was appended and then explicitly resumed, so inspect the
            # exact node/generation history after the terminal sequence.
            node = next(
                (
                    candidate for candidate in state.nodes.values()
                    if self._developer_sentinel(candidate)
                    and candidate.status is NodeStatus.failed
                    and candidate.error_reason == "developer_crash"
                    and candidate.id not in state.aborted_nodes
                    and not candidate.tombstoned
                    and type(candidate.terminal_event_seq) is int
                    # A crash whose log position is below `developer_crash_pause_after` never
                    # owed a pause: a missing one there is the DESIGN, not a lost append.
                    and self._developer_crash_pause_due(state, candidate.id)
                    and not self._has_exact_developer_pause(
                        events,
                        node_id=candidate.id,
                        generation=candidate.attempt,
                        after_seq=candidate.terminal_event_seq,
                    )
                ),
                None,
            )
            if node is None:
                return False
            # Pause ONLY: this node is already terminal, and a second terminal would break the
            # one-terminal-per-node invariant.
            records = developer_crash_records(
                node.id, node.attempt, node.code,
                "auto-paused: recovered a terminal Developer crash", terminal=False)
            pause_due = True
        tail = events[-1].seq if events else -1
        try:
            async with self._write_lock:
                self.store.append_many(records, expected_last_seq=tail)
            if pause_due:
                self._create_paused = True
            return True
        except EventStoreConcurrencyError:
            return True

    async def _drop_stale_speculation(
        self,
        *,
        eval_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> bool:
        """Drop at most one stale speculative node from a fresh fold.

        "Stale" and "never ran" are two different questions and only the first decides the DROP. A
        prefetch whose sandbox already started is still stale when selection moves, and is still
        terminalized here; what it does not get is the `never_evaluated` receipt that refunds its
        node-budget slot. See the append below.
        """

        if not self._speculation_enabled():
            return False
        events, state = self._fold_current()
        if self._terminal_intent(state):
            return False
        self._refresh_speculation_budget(state)
        # Match `_request_card_build`'s election set exactly: exclude committed speculative cards AND
        # durable producer-failed ids. A producer-failed card is serial-fallback-only (never
        # speculatively buildable); if it stayed in the counterfactual set here it would outrank the
        # subject (it was elected first, so it usually does) and drop a committed speculative node as
        # superseded. `_reserved_speculative_slots` documents that `excluded_card_ids` also carries
        # producer-failed ids.
        excluded = self._election_excluded_card_ids(state)
        ignored_pending = self._acknowledged_pending_ids(state)
        envelope = self._resource_envelope()
        for node in self._speculative_pending_nodes(state):
            if (node.id, node.attempt) in eval_inflight:
                continue  # burn-to-terminal once GPU dispatch has started
            card_id = node.idea.card_id
            if not isinstance(card_id, str):
                continue
            if speculative_card_is_fresh(
                state,
                self.policy,
                self._speculative_selection_node_limit(state),
                card_id=card_id,
                node_id=node.id,
                context=SpeculativeSelectionContext(
                    scoring=getattr(self, "_card_scoring", None),
                    excluded_card_ids=excluded,
                    ignored_pending_node_ids=ignored_pending,
                    resource_envelope=envelope,
                    consumed_inflight=eval_inflight,
                ),
            ):
                continue
            tail = events[-1].seq if events else -1
            # Durable proof for the L5 node-budget refund, and it is READ here, not assumed. This
            # loop only reaches a node still `pending` on a FRESH fold and not in `eval_inflight` —
            # but `eval_inflight` is IN-MEMORY, so a process that resumed after a kill starts with an
            # empty one and this node may be a prefetch whose sandbox burned real GPU minutes before
            # its process died. `Node.eval_started` is the durable half of that same question
            # (`events/types.py::EV_NODE_EVAL_STARTED`), so it survives the crash the in-memory set
            # cannot. A node that entered the sandbox is still stale and is still terminalized here —
            # it just does NOT get the marker, so it keeps the slot its compute already spent.
            never_evaluated = getattr(node, "eval_started", False) is not True
            payload = {
                "node_id": node.id,
                "generation": node.attempt,
                "error": CARD_FRESHNESS_SUPERSEDED_ERROR,
                "reason": "superseded",
                "eval_seconds": 0.0,
            }
            if never_evaluated:
                payload["never_evaluated"] = True
            try:
                async with self._write_lock:
                    self.store.append(
                        EV_NODE_FAILED,
                        payload,
                        expected_last_seq=tail,
                    )
                return True
            except EventStoreConcurrencyError:
                return True  # force a fresh fold before any scorer consult
        return False

    # How many CONSECUTIVE session turns a durable head may wait for a producer pair that could not
    # be BUILT before it is released `stale:producer_unavailable` (`_start_head_producer`). The
    # `_CARD_CLAIM_RETIRE_AFTER` argument one module over, for the same shape: one is too few — the
    # failure this retry exists for is a transient one while constructing roles, and giving up on it
    # at once is the defect — and the bound is small because an open head holds the session open.
    _PRODUCER_PAIR_RETRY_TURNS = 3
    # `(head, consecutive misses)` for that bound. The head is its request key PLUS its queue
    # position, so a later request for the same Card at the same epoch starts again at zero.
    # CLASS-level and immutable like the `_eval_*` plumbing above: a stub that never ran `__init__`
    # reads a defined value, and every write replaces the tuple.
    _producer_pair_misses: tuple = (None, 0)

    def _start_head_producer(self, current: RunState, session: CardSession) -> bool:
        """Start the producer for the durable head — `_start_request_producer` on the head."""
        return self._start_request_producer(current, session)

    def _start_request_producers(self, current: RunState, session: CardSession) -> bool:
        """Start a producer for every open request that has none, in queue order. With one producer
        the head is the only open request and this is `_start_head_producer`."""
        started = False
        for position, (_index, request) in enumerate(self._outstanding_positions(current)):
            live = self._session_state()
            # The head goes through `_start_head_producer`, the seam every single-producer test and
            # caller already names; only the requests behind it need the explicit form.
            if (self._start_head_producer(live, session) if position == 0
                    else self._start_request_producer(live, session, request)):
                started = True
        return started

    def _start_request_producer(self, current: RunState, session: CardSession,
                                request: Optional[Mapping[str, Any]] = None) -> bool:
        """Start the exact durable request (the head unless one is named) in the same turn that
        elected it.

        Waiting for the next loop turn leaves a request visible but not yet executing.
        A fast admitted eval can then cross the search-epoch boundary first and make a
        depth-one prefetch spuriously stale. Registering the producer before the next
        checkpoint preserves the documented live-backlog overlap without changing the
        durable request/commit authority.

        Returns True when it started a producer or closed the head, False when it did neither —
        including "no producer pair could be BUILT this turn", which leaves the head open for the
        next turn (see the no-pair branch below).
        """

        head = self._head_request(current) if request is None else dict(request)
        key = self._request_key(head)
        index = self._request_position(current, key)
        if (
            head is None
            or key is None
            or index is None
            or key in self._spec_build_inflight
            or key in self._spec_builds
        ):
            return False
        # recovery may have terminalized this head's interrupted
        # node_building after it consumed the final physical Node id. The request then
        # has no result but capacity remains zero, so no worker can close it and this
        # session polls forever. Close recovered unbuildable heads before this gate.
        if self._node_reservation_slots_remaining(
            current, consume_request=True, request_index=index,
        ) < 1:
            return False
        width = self._speculative_producer_width(current)
        reusable = self._spec_reusable.pop(key, None)
        if reusable is not None:
            if key[1] == current.search_epoch and self._spec_request_builder.get(
                    key, self._spec_builder_generation) == self._spec_builder_generation:
                # A paid build of this exact Card at this epoch is already in hand: no producer, no
                # attempt receipt (nothing new is billed), and the commit re-checks everything.
                self._spec_builds[key] = replace(reusable, reused=True)
                return True
            self._discard_spec_result(reusable)
        roles = self._producer_pair_for(key, width)
        if roles is not None:
            # The build pool is sized for ONE producer (`novelty.py::_CARD_BUILD_THREADS`); a wider
            # session widens it, never narrows it, or its second build would queue behind the first.
            limiter = _card_build_limiter()
            if limiter.total_tokens < width:
                limiter.total_tokens = width
        if roles is None:
            # NO PAIR IS NOT A PRODUCER FAILURE (review 2026-09-22, ENG1-14 — doc 50 ES1-04). This
            # branch closed the head `producer_failed`, the word for "the producer RAN and gave up",
            # and the fold turns that word into `card_build_producer_failed`: the Card was barred
            # from speculative election for the rest of the run and routed through the serial lane,
            # for a pair that merely could not be BUILT this turn — a factory that raised once, with
            # no producer ever started and nothing billed. Reachable on a RESUME: the log carries an
            # open head and the new process has no pool yet. (Everywhere else the election asks for
            # the pair FIRST — `_request_card_build` — so a head only exists once a lease does, and
            # the two things that drop a lease, a Developer swap and a BOHB switch, run in the outer
            # loop, which a session never returns to while a head is open.) Now:
            #
            # * a factory is wired but gave no pair: RETRY. Return False with the head still open,
            #   so the next turn asks the pool again (`_build_role_pairs` logs each failure with its
            #   exception). Bounded by `_PRODUCER_PAIR_RETRY_TURNS` turns running against THIS head,
            #   because an open head holds the session open (`_card_phase_decide_exit` counts it as
            #   producer work) and a factory that never recovers must not become a session that
            #   never returns;
            # * no factory at all, or the bound is spent: CLOSE it `stale` with the registered
            #   `producer_unavailable` reason — never `producer_failed`, since the Card is not at
            #   fault and must stay electable. Nothing can re-elect it until a pair exists
            #   (`_request_card_build` refuses election without one), so the release cannot spin,
            #   and the outer serial lane builds it meanwhile, as it builds any Card while the pool
            #   is down.
            position = (key, index)
            previous, misses = self._producer_pair_misses
            misses = misses + 1 if previous == position else 1
            factory = getattr(self, "role_factory", None)
            if factory is not None and misses < self._PRODUCER_PAIR_RETRY_TURNS:
                self._producer_pair_misses = (position, misses)
                return False
            self._producer_pair_misses = (None, 0)
            _LOG.warning(
                "no producer pair for the Card-build head %s (%s); releasing it to the serial lane "
                "as stale:producer_unavailable — the Card stays electable", key[0],
                "no role_factory is wired" if factory is None
                else f"the pool built none in {misses} consecutive turns")
            if self._append_card_build_done(
                head, skipped="stale", skipped_reason="producer_unavailable",
            ):
                session.yield_outer = True
                return True
            return False
        self._producer_pair_misses = (None, 0)
        self._spec_build_inflight.add(key)
        # Receipt BEFORE the producer can reach a provider, and after the inflight
        # marker so a main-task service turn in between cannot mistake this process's
        # own fresh attempt for a dead process's unreconciled one.
        self._record_card_build_attempt(current, head)
        self._spec_request_builder[key] = self._spec_builder_generation
        # WIDER THAN ONE, THE BUILD IS ADOPTED: it runs in the run-scoped group (`_eval_task_group`,
        # opened by `Engine.run`) and outlives this session, so a session owed the outer boundary
        # returns at once instead of holding the Strategist and every cadence behind hours of
        # Developer work. Its wake-up goes to whichever session is CURRENT (`_eval_notify`). At
        # width 1 the build stays in the session's group exactly as it always did.
        adopt = width > 1 and self._eval_task_group is not None
        group = self._eval_task_group if adopt else session.task_group
        notify = _CurrentSessionNotify(self) if adopt else session.notify
        if adopt:
            self._spec_adopted.add(key)
        try:
            group.start_soon(
                self._produce_card_build,
                dict(head),
                roles,
                notify,
            )
        # ACCEPTED asymmetry, stated. The rollback below discards only the in-memory
        # `_spec_build_inflight`; the DURABLE `card_build_attempted` receipt appended
        # just above is NOT undone, so if the producer never started, the next service
        # turn sees an unreconciled attempt (no inflight marker, no result) and closes
        # the head `producer_failed` — barring an unbilled Card from speculative
        # re-election. It stands because `start_soon` raises only during task-group
        # TEARDOWN: the process is already stopping, nothing else will consume that
        # head this run, and the degrade is conservative (a Card is skipped, never
        # double-built). Undoing it would mean a compensating durable append on the
        # shutdown path — more machinery, and more failure surface, than the edge it
        # closes.
        except BaseException:
            self._spec_build_inflight.discard(key)
            self._spec_adopted.discard(key)
            self._release_producer_pair(key)
            raise
        return True

    async def _card_eval_one(
        self,
        node_id: int,
        generation: int,
        reservation: Optional[dict],
        max_eval_seconds: Optional[float],
    ) -> None:
        """One adopted evaluation child.  Owned by the RUN-scoped eval task group, not a session.

        It deliberately takes no `CardSession`.  The task group that runs it outlives the session
        that admitted it, so a child holding a session reference would, after that session returned,
        set a flag nobody reads and post its wake-up into a closed stream — the successor session
        would never learn that a slot had come free.  Everything it has to publish is therefore
        engine-level: the inflight set, the boundary debt, and the CURRENT session's wake-up stream.

        A SPEND CEILING IS DEFERRED, never raised into the run-scoped group (review 2026-09-22,
        ENG2-02).  `_evaluate` re-raises a `BudgetExceeded` crossed by this node's own post-score
        bookkeeping (after landing the node's own terminal, `_land_terminal_before_ceiling`), and a
        child that let it escape cancelled EVERY sibling in the group at its next checkpoint — the
        measured loss `Engine._drain_inflight_evaluation` documents, which that drain cannot repair
        from inside the scope the child just cancelled.  So the child parks it on
        `_eval_budget_stop` (the first one wins) and returns normally; the `finally` below still
        runs, and the boundary debt it owes is what hands the session back to the run loop, whose
        head raises the stop once the siblings have landed (`_raise_deferred_eval_budget_stop`).
        Everything else — a cancellation above all — propagates exactly as before
        (`core/errors.py::deferrable_budget_stop` says what may be deferred and why).

        A REFUSED RUN SETUP IS DEFERRED THE SAME WAY (review 2026-09-22, ENG2-08): it is the run's
        ending too, it surfaces in whichever evaluation ran the setup, and every child queued behind
        that setup stops on the same latched refusal — raised into the group, it would cancel the
        siblings and the host body and end the run on a group of N copies. Parked here, the first
        wins and the owner raises exactly one (`core/errors.py::deferrable_run_stop`).
        """

        try:
            await self._evaluate(node_id, anyio.CapacityLimiter(1), max_eval_seconds)
        except BaseException as exc:  # noqa: BLE001 — re-raised unless it is a pure run-ending stop
            stop = deferrable_run_stop(exc)
            if stop is None:
                raise
            if self._eval_budget_stop is None:
                self._eval_budget_stop = stop
        finally:
            # This is the resolution of the `CODEX AGENT` TODO that used to sit here: "this
            # session-wide first-completion fence prevents the Card path from refilling a freed GPU
            # while unrelated long-running siblings finish. Preserve the outer cadence boundary
            # without turning one terminal child into head-of-line blocking for every remaining
            # slot; add an unequal-duration refill regression."
            #
            # A terminal owes the outer control/Strategist/cadence boundary a turn.  That is all it
            # ever meant, and it is now all it does: the debt closes the PRODUCER lane and asks
            # `_card_phase_decide_exit` to return, and the session CAN return, because the eval task
            # group is run-scoped and the next session adopts whatever is still burning.  It no
            # longer closes admission, so the slot this child just freed is refilled by the very
            # turn that observes the terminal.  The regression the TODO asked for is
            # `tests/test_card_refill_unequal_durations.py`.
            #
            # IT IS A BOOL, NOT A COUNT, and the debt is therefore "at least one terminal has landed
            # since the session last handed back" — not "one turn per terminal".  At width > 1 two
            # children finishing inside one poll window collapse into a SINGLE owed turn, so the
            # one-turn-per-terminal reading of the line above is stronger than the code (backlog
            # F1g, 2026-08-14).  Left a bool deliberately, on three grounds.  (a) It costs no work:
            # the outer loop is not rationed by this flag — it keeps turning until nothing is
            # starved — and the occupancy pace it feeds is WIDTH-complete
            # (`_occupancy_paced_creates` asks for every free slot, not for one), so one hand-back
            # refills as many slots as the collapse freed.  (b) It costs no cadence: two terminals
            # inside one poll window are at the same node count, and every cadence is node-count
            # paced and at_node-idempotent, so the second turn would have decided exactly what the
            # first one did.  (c) A counter would have to be decremented by a consumer that can
            # crash between the read and the decrement, which is a durability question this flag
            # does not currently have.  Driven by
            # `test_two_terminals_in_one_window_owe_one_turn_and_still_refill_every_freed_slot`.
            self._eval_boundary_owed = True
            # The eval-second allowance this lane committed at admission (ENG2-05) goes back FIRST,
            # before the inflight entry and the wake-up below: the next admission fill asks whether
            # one more lane fits, and it must ask with this lane's worst case already handed back —
            # its REAL cost is in the log by now and `total_eval_seconds` charges it.
            from looplab.engine.eval_dispatch import _release_eval_time
            _release_eval_time(self, node_id, generation)
            if reservation is not None:
                self._settle_eval_resource_reservation(node_id, generation, reservation)
            self._eval_inflight.discard((node_id, generation))
            notify_producer(self._eval_notify, ("eval", (node_id, generation)))

    def _card_phase_serve_raw_stage(self, session: CardSession) -> None:
        """Commit one prepared raw proposal, then — where the session may still produce — elect and
        start its producer in the same turn."""

        raw_consumed, raw_staged, abandon_reason = self._serve_raw_card_stage()
        if not raw_consumed:
            return
        if not raw_staged and abandon_reason is not None:
            # A PREPARED PROPOSAL WAS ABANDONED, and until 2026-08-31 that left no trace of any kind.
            # Measured on v12: node 2's card took FIVE propose phases — four speculative ones
            # completed `ok: true` and staged nothing (604.8 + 317.7 + 139.8 + 524.5 s = 26.5 min of
            # its 44.6-minute bill) before the fifth minted `card-2`. The run has zero
            # `novelty_rejected` / `card_auto_dropped` rows and its console had zero `refused` lines.
            #
            # THE RECEIPT DROP ABOVE IS DELIBERATE AND IS NOT WHAT THIS FIXES. `_consume_prepared_
            # raw_stage` republishes the audit prefix only on an ATTACH refusal, because "on a
            # stale-fence refusal the whole proposal is being abandoned and re-made, so dropping
            # them keeps the log honest" — republishing novelty rows for work about to be repeated
            # would double-count it. A COUNTED LINE carries no novelty rows, so it cannot.
            #
            # `_stage_card_creates` has counted its refusals since 6262f3a1; that counter is on the
            # CREATE lane and this one reaches `_stage_prepared_card` by another route, so it had
            # none. The reason rides on the serve's own RETURN — the staging fence's
            # `CARD_STAGE_REFUSALS` slug (or `unrecorded`) when the stager refused, one of
            # `RAW_STAGE_PRE_STAGING_REASONS` when the producer crashed or the proposal formed no
            # idea, and no reason at all for the attach handoff, which is handed on and built
            # rather than abandoned. It is NOT read off `_card_stage_refusal` here: only
            # `_stage_prepared_card` writes that attribute, so on the pre-staging paths it still
            # held an unrelated earlier call's slug and this warning misattributed a producer
            # crash to a fence that never fired. The DURATION is deliberately not repeated here:
            # it is already on this phase's `phase_progress` row, and one number in two places is
            # how they drift.
            reason = abandon_reason
            counter = getattr(self, "_spec_raw_stage_abandoned", None)
            if counter is None:
                counter = self._spec_raw_stage_abandoned = collections.Counter()
            counter[reason] += 1
            _LOG.warning(
                "the speculative lane abandoned a prepared proposal: %s (%d so far this run; the "
                "seconds it cost are on its own phase_progress row)", reason, counter[reason])
        session.progressed = True
        # THE COMMIT ABOVE IS DELIBERATELY UNGATED, and `gates.stopping` is the gate it is ungated
        # against.  A prepared raw stage is already PAID FOR, and `_spec_raw_stage_result` counts in
        # `_card_phase_decide_exit`'s `memory_pending`: a stopping session that declined to drain it
        # would hold itself open over a result no other turn can adopt, and throw away a proposal on
        # the way out.  Committing it is what lets a stopping run finish cleanly.
        #
        # WHAT FOLLOWS THE COMMIT IS NEW PRODUCER WORK, and it takes the ordinary gate.  Electing a
        # durable Card and starting its head producer is exactly the pair `_card_phase_request_build`
        # refuses two phases below under `open_for_production`, and this site used to hand-roll two
        # of that predicate's three conjuncts — `boundary_owed` and `yield_outer` — while dropping
        # `gates.stopping`.  A terminal intent, an exhausted budget or a pending outer rebuild would
        # then still buy a paid build, which `producer_inflight` holds the session open for the whole
        # of.  The gate reads a snapshot taken AFTER the commit, because the commit APPENDED.
        if raw_staged and session.open_for_production(
                self._session_gates(self._session_state(), session)):
            if self._request_card_build(consumed_inflight=session.eval_inflight):
                # The election above APPENDED, so this snapshot re-folds: `_fold_current` serves the
                # memo only while the observed tail is unmoved.
                self._start_request_producers(self._session_state(), session)
            else:
                # A durable request, not Card reuse alone, is the success boundary.
                # Return to the outer selector instead of repeating a paid proposal.
                session.yield_outer = True
        else:
            # Nothing was staged. Yield rather than propose again — for a stale fence because the
            # outer loop is where a fresh authority snapshot comes from, and for the PERMANENT
            # attach refusal (`_stage_prepared_card`'s `attach` branch) because the outer loop is
            # the only place that can build a repair at all.
            # A stopping session lands here too, having staged its Card durably: yielding is what it
            # was already going to do, and the Card stays on the board for the outer turn to select.
            session.yield_outer = True

    async def _card_phase_drop_stale(self, session: CardSession) -> bool:
        """Release orphaned buffers, acknowledge one aborted node, drain the stale prefix.

        Returns True when the turn must RESTART — the gate drops one Node per CAS, and a later Card
        scorer consult must never see a partially-clean selection state.
        """

        current = self._session_state()
        self._discard_orphaned_spec_results(current)
        aborted = next(
            (
                node for node in current.pending_nodes()
                if node.id in current.aborted_nodes
                and node.id not in {
                    node_id for node_id, _generation in session.eval_inflight
                }
            ),
            None,
        )
        if aborted is not None and self._skip_if_aborted(
            {"node_id": aborted.id}, current,
        ):
            session.progressed = True

        if (
            # An eval terminal closes this admitted batch.  Leave its already-built next
            # Node untouched for the outer control/Strategist/cadence boundary; freshness
            # will re-run from that fresh outer turn.  A pre-decided serial fallback has
            # the same boundary semantics while its admitted eval burns to terminal.
            #
            # PRODUCTION's gate, deliberately, even though F1f un-latched ADMISSION one phase below.
            # This SESSION-WIDE drain terminalizes an already-built Node, which is a selection act,
            # and the outer turn — with its cadences, its Strategist and its own
            # `_drop_stale_speculation` — is where that decision has always been taken after a
            # terminal.  Running it here instead would move the discard EARLIER by one turn for no
            # gain and would change which snapshot decided it.  Admission is not thereby left
            # unguarded: `_card_phase_admit_evals` re-checks `speculative_card_is_fresh` for the
            # exact candidate immediately before the GPU child starts, and drains on a miss — so an
            # un-latched consumer still cannot dispatch a stale prefetch.
            #
            # The gate reads its OWN snapshot rather than the `current` above, because
            # `_skip_if_aborted` may have appended between them.  Asking `_fold_current` again is
            # free when nothing was appended (the tail is unmoved) and correct when something was,
            # so there is no "remember to refresh" line here for anyone to delete later.
            session.open_for_production(
                self._session_gates(self._session_state(), session))
            and await self._drop_stale_speculation(
                eval_inflight=session.eval_inflight,
            )
        ):
            # The gate drops one Node per CAS. Drain the whole stale prefix before any
            # later Card scorer consult sees a partially-clean selection state.
            await anyio.sleep(0)
            return True
        return False

    def _card_phase_serve_head(self, session: CardSession) -> None:
        """Service every open durable request in queue order — commit a finished build, close a dead
        one — then start the producer for each open request that has none.

        With one producer the head is the only open request, so this is the historical "serve the
        head, else start its producer". With several, a build that finishes before one opened
        earlier commits at once (its `card_build_done` names its position) instead of waiting
        behind the slower head while its GPU slot idles."""

        current = self._session_state()
        open_requests = self._outstanding_positions(current)
        if not open_requests:
            return
        # Recovery still links an already-created exact Node before consulting this flag. Once the
        # admitted batch closes, every other request is acknowledged stale without another scorer
        # consult/claim crossing the outer cadence boundary.
        allow_commit = session.open_for_production(self._session_gates(current, session))
        served = False
        for _index, request in open_requests:
            if self._serve_card_builds(
                session.max_eval_seconds, allow_commit=allow_commit, request=request,
            ):
                served = True
        if served:
            session.progressed = True
            if self._spec_force_outer:
                session.yield_outer = True
                self._spec_force_outer = False
            return
        # Nothing closed: start the producer for every open request that has none. A request the
        # election just appended already has one (`_card_phase_request_build`); this is the
        # recovery path — a resumed process whose log carries open requests and no producers.
        current = self._session_state()
        if session.open_for_production(self._session_gates(current, session)):
            if self._start_request_producers(current, session):
                session.progressed = True
    async def _card_phase_admit_evals(self, session: CardSession) -> bool:
        """Admit fresh, resource-fitting pending Nodes up to the live consumer width.

        Returns True when the turn must RESTART because selection moved under the admission scan.
        """

        current = self._session_state()
        if not session.open_for_admission(self._session_gates(current, session)):
            return False
        selection_changed = False
        while len(session.eval_inflight) < max(1, int(self._eval_parallel)):
            # A SPEND CEILING an adopted evaluation deferred (review 2026-09-22, ENG2-02) admits
            # nothing more: the deferral only lets evaluations ALREADY paid for finish, and the run
            # loop raises the stop once they have (`_raise_deferred_eval_budget_stop`).  Asked every
            # fill, because the stop is set by a child task on this loop and can land across any
            # `await` a previous admission took.
            if self._eval_budget_stop is not None:
                break
            current = self._session_state()
            # `.stopping` and `open_for_admission` are now the SAME predicate, and the asymmetry
            # this comment used to describe is gone with the defect: re-reading the terminal latch
            # here would have let the first sibling to terminate truncate the batch its own
            # siblings were still being admitted into — a width-4 consumer that silently admits
            # three, the "speculation quietly went serial" failure this subsystem has already paid
            # for once.  That was the SAME mistake as F1f, one scope smaller, and it was fixed
            # here first.  Both spellings are kept because they answer different questions: this
            # one is the inner fill, the gate above is the batch BOUNDARY.
            if self._session_gates(current, session).stopping:
                break
            candidates = [node for node in current.pending_nodes()
                          if self._session_admissible(node, current, session)]
            if not candidates:
                break
            chosen = None
            reservation = None
            for candidate in candidates:
                got = self._try_reserve_node_resources(
                    candidate,
                    resource_pin=self._card_resource_pin_for_node(
                        current, candidate),
                )
                if got is not None:
                    chosen, reservation = candidate, got
                    break
            if chosen is None:
                break
            admission = self._session_state()
            live = admission.nodes.get(chosen.id)
            if (
                # Same asymmetry as the fill gate above: the fold-derived half only.
                self._session_gates(admission, session).stopping
                or live is None
                or live.attempt != chosen.attempt
                or live.status is not NodeStatus.pending
                or not self._session_admissible(live, admission, session)
            ):
                self._release_gpus(reservation.get("gpu_ids"))
                break
            current = admission
            chosen = live
            if not self._node_resource_reservation_is_current(
                current, chosen, reservation,
            ):
                # An operator may change the Card pin between the fit scan and this
                # fresh admission fold. Never launch with a reservation formed for
                # the old quantities; release it and rescan against current truth.
                self._release_gpus(reservation.get("gpu_ids"))
                session.progressed = True
                selection_changed = True
                break
            # Freshness was checked above, but a resource wait/earlier admission may
            # have moved selection. Re-check immediately before the GPU child starts.
            if self._speculative_link_matches(current, chosen):
                fresh = speculative_card_is_fresh(
                    current,
                    self.policy,
                    self._speculative_selection_node_limit(current),
                    card_id=chosen.idea.card_id,
                    node_id=chosen.id,
                    context=SpeculativeSelectionContext(
                        scoring=getattr(self, "_card_scoring", None),
                        excluded_card_ids=self._speculative_card_ids(current)
                        | self._producer_failed_card_ids(current),
                        ignored_pending_node_ids=(
                            self._acknowledged_pending_ids(current)),
                        resource_envelope=self._resource_envelope(),
                        consumed_inflight=session.eval_inflight,
                    ),
                )
                if not fresh:
                    self._release_gpus(reservation.get("gpu_ids"))
                    # DO NOT START IT — that is the whole point of this re-check, and it holds
                    # unconditionally.  Whether to TERMINALIZE it is a different question, and it
                    # belongs to whoever owns the next selection decision.  Once the outer boundary
                    # is owed a turn (`open_for_production` false: a terminal landed, or the
                    # producer yielded), the discard is the outer loop's — it runs its own
                    # `_drop_stale_speculation` after the cadences, from a snapshot those cadences
                    # may have moved, which is exactly where this decision was taken before F1f
                    # un-latched admission.  Dropping it here instead would move a selection act one
                    # turn earlier and onto a different snapshot, for no gain: the slot is freed
                    # either way, and the node is unstartable either way.
                    if session.open_for_production(
                        self._session_gates(current, session),
                    ) and await self._drop_stale_speculation(
                        eval_inflight=session.eval_inflight,
                    ):
                        session.progressed = True
                        selection_changed = True
                    break
            # THE EVAL-SECOND ALLOWANCE, asked here exactly as `_dispatch_evals` asks it (review
            # 2026-09-22, ENG2-05): the SAME `eval_dispatch.py::_eval_time_admission_refused` over the
            # same in-flight reservation ledger. This path used to ask nothing, so with one second of
            # `max_eval_seconds` left every free speculative lane read "there is time" and started —
            # the "ceiling times N" `resources.py::eval_time_admission_blocked` was written to refuse,
            # on the path speculation runs. The first lane with nothing in flight is still admitted
            # whatever its worst case (that rule's deadlock guard). Deferred import: the helpers live
            # beside the dispatcher that owns them, and this mixin is imported by the orchestrator.
            from looplab.engine.eval_dispatch import _eval_time_admission_refused
            if _eval_time_admission_refused(self, current, chosen, session.max_eval_seconds):
                self._release_gpus(reservation.get("gpu_ids"))
                break
            if not session.research_spawned:
                # Latch on the SPAWN, never on the ask. `_spawn_research` answers "was research due
                # AND started?", and a session that asked at n=1 and got NO (as it did under the
                # pre-2026-08-07 `deep_research_every`=3, and still does whenever an operator spells
                # a positive window) must keep asking as it admits n=2, n=3, … — see that method's
                # docstring for the measured cost of latching on the ask instead. Once it does start,
                # the latch still holds for the rest of the window, so there is never a second
                # overlap loop.
                session.research_spawned = bool(
                    self._spawn_research(session.bg_task_group, current))
            self._register_eval_resource_reservation(
                chosen.id, chosen.attempt, reservation,
            )
            # The DURABLE half of `eval_inflight`, written by the MAIN task at the
            # dispatch decision itself. `eval_inflight` is in-memory, so a process
            # that resumed after a kill starts with an empty one and cannot tell a
            # prefetch that never ran from one whose sandbox burned GPU minutes;
            # this row can. It belongs HERE and not in the worker because
            # `_request_card_build` elects under a tail CAS, and a worker-written
            # row inside that window makes every election lose it (see
            # `_record_eval_start_boundary`).
            self._record_eval_start_boundary(chosen)
            session.eval_inflight.add((chosen.id, chosen.attempt))
            # …and the TIME this lane will charge, committed at the admission decision so the NEXT
            # fill of this loop is asked against it (ENG2-05). Taken after the durable boundary, so
            # a store error there cannot leak it; released in `_card_eval_one`'s `finally` beside
            # the devices, or on the failed-spawn path below, where nothing ran to release it.
            from looplab.engine.eval_dispatch import _release_eval_time, _reserve_eval_time
            _reserve_eval_time(self, chosen.id, chosen.attempt, chosen)
            try:
                # The RUN-scoped group (`session.eval_task_group`), not the session-owned one.
                # `_record_eval_start_boundary` above is unchanged and still runs HERE, on the main
                # task at the dispatch decision, exactly where engine invariant #1 says to keep it —
                # widening the child's LIFETIME moves no writer.
                session.eval_task_group.start_soon(
                    self._card_eval_one, chosen.id, chosen.attempt, reservation,
                    session.max_eval_seconds,
                )
            except BaseException:
                _release_eval_time(self, chosen.id, chosen.attempt)
                session.eval_inflight.discard((chosen.id, chosen.attempt))
                self._clear_eval_resource_reservation(
                    chosen.id, chosen.attempt,
                )
                self._release_gpus(reservation.get("gpu_ids"))
                raise
            session.progressed = True
        if selection_changed:
            await anyio.sleep(0)
            return True
        return False

    async def _card_phase_request_build(self, session: CardSession) -> bool:
        """Own the counterfactual next action: elect a durable Card, or propose a raw one.

        Returns True when the turn must RESTART because the freshness drain moved selection.
        """

        current = self._session_state()
        if self._operator_node_request_ready(current):
            # A queued fork/inject owns the next build slot: electing another Card here is what
            # starved v9's inject behind card after card. The exit decision hands it to the outer
            # loop once the producer lane is idle.
            return False
        consumer_active = bool(
            session.eval_inflight
            or any(self._session_admissible(node, current, session)
                   for node in current.pending_nodes())
        )
        # WIDER THAN ONE, A FREE PRODUCER IS REASON ENOUGH (2026-09-24). "Consumer active" is the
        # prefetch's premise — hide a build behind a RUNNING evaluation — and at width one it stays
        # the gate. With several producers the session is where builds run side by side; measured on
        # MiniOneRec inf12 (48-second evaluations, builds of hours), gating on a running eval meant
        # at most one election per evaluation window and the second producer idle for hours.
        wide = self._speculative_producer_width(current) > 1
        # THE RUN-AHEAD LANE (2026-09-25, critic review of "K raw lanes"): wider than one, the raw
        # proposal lane holds its own pair and no build slot, so it may propose the NEXT Card while
        # every build producer is busy. Measured on MiniOneRec inf12: counting it as a build slot meant
        # no Card was proposed while both producers built, and a freed producer then waited a whole
        # 15-30 min proposal before its next build.
        raw_lane_free = (wide and not self._spec_raw_stage_inflight
                         and self._spec_raw_stage_result is None)
        if not ((consumer_active or wide)
                and session.open_for_production(self._session_gates(current, session))):
            return False
        # Asked only past the two cheap gates: it builds the producer pool, i.e. calls the role
        # factory, and a dead factory must be asked once per turn, not once per question.
        can_elect = self._busy_producers(current) < self._producer_capacity(current)
        if not (
            # A free producer: every open request, running build and (at width 1) the raw lane holds
            # one, and the width is `llm_parallel` (`_speculative_producer_width`). At width 1 this is
            # the historical "no head, nothing in flight, no raw proposal".
            (can_elect or raw_lane_free)
            and self._unadmitted_prefetch(
                current,
                consumed_inflight=session.eval_inflight,
            ) < self._speculative_prefetch_ceiling()
        ):
            return False
        # `_request_card_build` consults the Card scorer. Drain any newly-stale
        # speculative prefix immediately before that consult, not just per session turn.
        if await self._drop_stale_speculation(
            eval_inflight=session.eval_inflight,
        ):
            await anyio.sleep(0)
            return True
        requested = can_elect and self._request_card_build(
            consumed_inflight=session.eval_inflight,
        )
        if not requested:
            # No durable Card owns the counterfactual next action. Propose and stage
            # that raw lane in the main task while GPU children continue in worker
            # threads; then request the exact receipt from a fresh fold. Card staging
            # owns its own tail/generation/parent CAS and may safely decline a stale
            # proposal if an eval changes the search state during the paid call.
            # Selection and proposal share one immutable log snapshot.  A second
            # read here would let an old raw action inherit a newer epoch/parent
            # receipt fence and make the main-task commit validate the wrong authority.
            # Deliberately NOT `_fold_current`: this pair is the proposal's OWN authority snapshot,
            # handed whole to a worker that outlives the turn, and its explicit read/fold pairing is
            # what `test_raw_action_selection_and_worker_share_one_proposal_snapshot` reads.
            proposal_events = self.store.read_all()
            proposal_state = fold(proposal_events)
            if (
                (wide or self._busy_producers(proposal_state)
                 < self._producer_capacity(proposal_state))
                # ONE raw proposal at a time, whatever the build width: the lane has ONE result slot
                # (`_spec_raw_stage_result`), ONE lease ("raw") and ONE in-flight flag. Measured
                # 2026-09-24 on MiniOneRec inf12 at width 2: counting the lane as one busy producer
                # and nothing more let a free build slot start a second, third and fourth proposal
                # on the SAME pair while the first ran — four concurrent Researcher sessions, their
                # results overwriting one slot, one staged Card in an hour.
                and not self._spec_raw_stage_inflight
                and self._spec_raw_stage_result is None
                # The SAME ceiling as the durable election above, and this half matters most: a
                # refusal there falls through to here, so leaving the raw lane on the bare depth
                # would turn "do not buy a prefetch the gate must discard" into "buy a Researcher
                # proposal and a staged Card instead" — the identical spend one lane over.
                and self._prefetch_supply_used(
                    proposal_state,
                    consumed_inflight=session.eval_inflight,
                ) - len(self._outstanding_requests(proposal_state))
                < self._speculative_prefetch_ceiling()
            ):
                raw_actions = speculative_raw_actions(
                    proposal_state,
                    self.policy,
                    self._speculative_selection_node_limit(proposal_state),
                    context=SpeculativeSelectionContext(
                        scoring=getattr(self, "_card_scoring", None),
                        excluded_card_ids=self._speculative_card_ids(
                        proposal_state),
                        ignored_pending_node_ids=self._acknowledged_pending_ids(
                        proposal_state),
                        resource_envelope=self._resource_envelope(),
                    ),
                )
                roles = (self._producer_pair_for(
                    "raw", self._speculative_producer_width(proposal_state) if wide
                    else self._producer_capacity(proposal_state)) if raw_actions else None)
                if raw_actions and roles is not None:
                    proposal_node_ceiling = self._node_id_ceiling(
                        proposal_events, proposal_state,
                    )
                    # Rolled back on a failed spawn, exactly like
                    # `_start_head_producer` does with its inflight key. If
                    # `start_soon` raises (the task group is already closing) the
                    # `finally` in `_produce_raw_card_stage` never runs, and
                    # `_ensure_speculation_state` only initializes MISSING attrs —
                    # so this flag would stay True forever, every session-exit gate
                    # below would keep counting it in `memory_pending`, and the
                    # NEXT `_run_card_session` could never reach a break condition.
                    self._spec_raw_stage_inflight = True
                    # Wider than one the proposal is ADOPTED like a build: the run-scoped group runs
                    # it and the current session serves its result, so a session owed the outer
                    # boundary is not held open by a 15-30 min Researcher call (measured: card-14,
                    # card-15 and card-8 waited 11, 21 and 59 min for exactly that, GPU idle).
                    adopt = wide and self._eval_task_group is not None
                    self._spec_raw_adopted = adopt
                    try:
                        (self._eval_task_group if adopt else session.task_group).start_soon(
                            self._produce_raw_card_stage,
                            dict(raw_actions[0]),
                            proposal_events,
                            proposal_state,
                            proposal_node_ceiling,
                            roles,
                            _CurrentSessionNotify(self) if adopt else session.notify,
                        )
                    except BaseException:
                        self._spec_raw_stage_inflight = False
                        self._spec_raw_adopted = False
                        self._release_producer_pair("raw")
                        raise
                    session.progressed = True
                else:
                    # Unsupported raw interception (or no isolated pair) must
                    # degrade at the outer serial boundary, never poll/re-propose.
                    session.yield_outer = True
        if requested:
            # The election APPENDED, so this re-folds (see `_fold_current`).
            self._start_request_producers(self._session_state(), session)
            session.progressed = True
        return False

    def _card_phase_decide_exit(self, session: CardSession) -> bool:
        """The ONE session-exit decision.  True means break out of the turn loop."""

        events, current = self._fold_current()
        self._discard_orphaned_spec_results(current)
        gates = self._session_gates(current, session)
        pending_ready = any(
            self._session_admissible(node, current, session)
            for node in current.pending_nodes()
        )
        outstanding = bool(self._outstanding_requests(current))
        building = bool(current.buildings)
        memory_pending = bool(
            self._spec_build_inflight
            or self._spec_builds
            or self._spec_raw_stage_inflight
            or self._spec_raw_stage_result is not None
            or getattr(self, "_inject_lanes_inflight", 0)
        )
        # PRODUCER work this session owns and no other turn can adopt: a durable request head it
        # elected, a `node_building` marker, an isolated build/raw-stage worker holding an
        # in-memory result slot.  Evals are deliberately NOT in here any more — see below.
        producer_inflight = bool(any((outstanding, building, memory_pending)))
        if not producer_inflight and self._operator_node_request_ready(current):
            # The outer loop serves a queued fork/inject (`_serve_forced_requests`) before any
            # speculation; with the producer lane idle nothing here may hold it off any longer.
            return True
        # What a CLOSING session must still wait for: work this session alone can finish. A finished
        # result waits in `_spec_builds` for the next session's commit (`_serve_card_builds` leaves
        # its request open), and a build running in the run-scoped group is adopted by the next
        # session the way an evaluation is — neither is a reason to keep the outer loop waiting.
        ready = {key for key in self._spec_builds}
        adopted = self._adopted_producers()
        waiting = {
            key for request in self._outstanding_requests(current)
            if (key := self._request_key(request)) is not None
            and key not in ready and key not in adopted
        }
        closing_holds = bool(
            building
            or (set(self._spec_build_inflight) - adopted)
            or (self._spec_raw_stage_inflight and not self._spec_raw_adopted)
            or self._spec_raw_stage_result is not None
            or getattr(self, "_inject_lanes_inflight", 0)
            or waiting
        )
        if session.open_for_production(gates):
            # Still open for work, so a ready pending Node or a running eval keeps the session
            # alive — there is nothing to hand back to and a slot may free at any moment.
            return not (producer_inflight or session.eval_inflight or pending_ready)
        # Closing.  The outer control/Strategist/cadence boundary is owed a turn (a terminal
        # landed, the producer yielded, or a fold-derived stop condition fired) — so RETURN and let
        # it have one.  Waiting for `session.eval_inflight` here is precisely the F1f barrier: the
        # wait bought nothing, because the boundary this session is holding itself open to reach
        # does not arrive until the LAST eval lands, while the debt was incurred by the FIRST.  The
        # run-scoped eval task group means the children survive the return and the next session
        # adopts them from `self._eval_inflight` (and, across a crash, from the durable
        # `node_eval_started` boundary `_drop_stale_speculation` already reads).
        #
        # …ONCE PER DEBT, though, and this clause is what makes that true.  `yield_outer` is set on a
        # CONDITION, not on an event: `_card_phase_request_build` re-derives "no durable Card owns
        # the next action and the raw lane has nothing to propose" every turn, and while a long
        # evaluation runs that answer is usually the same one.  Without this the outer loop and a
        # fresh session would ping-pong for the whole evaluation — each round trip a full
        # `read_all()` + `fold()` of the run's entire log, several times a second, for hours, on the
        # network mount a run directory usually lives on.  A hand-back is owed only when something
        # the outer loop could ACT on has changed, and on an append-only log that is exactly "the
        # tail moved" — by ANY writer, including the cadences the previous hand-back ran.  A terminal
        # (`boundary_owed`) and every fold-derived stop still hand back unconditionally; only the
        # recurring producer yield is rate-limited, and only while an adopted evaluation is still
        # running, which is what this session then stays alive FOR.
        #
        # THE SAME ONCE-PER-DEBT RULE WHEN ADOPTED PRODUCER WORK IS WHAT IS RUNNING (2026-09-26).
        # The eval half alone needs `not producer_inflight`, so it never covered a session whose only
        # work is a build (or raw proposal) in the run-scoped group: `closing_holds` is False for it,
        # the session set the debt and returned, the outer loop paid a cadence pass and came straight
        # back. Measured on MiniOneRec inf13 (width 2): the run's first build, 8 minutes with no eval
        # and no pending node, turned the outer loop ~5 times a second — 2,534 cadence passes, each a
        # full read and fold of the log. On an unmoved tail that session now polls instead, and it
        # drops the latched `yield_outer` as it does so: a latched one keeps `open_for_production`
        # False, and the build finishing under it would be closed `commit_not_allowed` rather than
        # committed (`_serve_card_builds`). The eval half keeps it latched, as it always has: it
        # holds no producer work a latched flag could refuse.
        adopted_only = producer_inflight and not closing_holds
        if (
            session.yield_outer
            and not session.boundary_owed
            and not gates.stopping
            and ((session.eval_inflight and not producer_inflight) or adopted_only)
        ):
            tail = events[-1].seq if events else -1
            if tail == self._outer_boundary_served_tail:
                if adopted_only:
                    session.yield_outer = False
                return False              # nothing new to hand back; poll instead of ping-ponging
            self._outer_boundary_served_tail = tail
        if closing_holds:
            return False
        if self._outstanding_requests(current):
            # Requests stay open across the hand-back (a result to commit, a build still running):
            # the outer loop owes ONE cadence pass before it re-enters a session for them, or its
            # head-open re-entry (`orchestrator.py`) would skip the very boundary this is returning for.
            self._card_boundary_debt = True
        return True

    async def _run_card_session(
        self,
        evals: list,
        state: RunState,
        max_es: Optional[float],
        wall_deadline: Optional[float] = None,
    ) -> None:
        """Continuously overlap the folded-log consumer with one isolated Card producer.

        The turn loop is six named phases over ONE folded snapshot per phase (doc 25 EC-02).  Each
        phase re-derives its own snapshot through `_fold_current`, which serves the previous fold
        only while the observed log tail is unmoved — so a phase that appends is re-folded for the
        next phase BY CONSTRUCTION rather than by remembering to, and the session folds once per
        OBSERVED TAIL instead of six-to-nine times per turn.
        """

        if not self._speculation_enabled():
            await self._dispatch_evals(evals, state, max_es)
            return
        self._ensure_speculation_state()
        send, receive = anyio.create_memory_object_stream(256)
        session = CardSession(
            max_eval_seconds=max_es,
            wall_deadline=wall_deadline,
            notify=send,
            # ENGINE-level, shared by every session in the run.  A session that returns while its
            # evals burn hands the successor the same set object, so the successor's width fill and
            # its `_session_admissible` exclusion both see the adopted children without any
            # handover step to forget.
            eval_inflight=self._eval_inflight,
        )
        # The CURRENT session's wake-up stream, for children that outlive the session that admitted
        # them.  Cleared on exit so a late terminal posts into a closed stream that
        # `notify_producer` already swallows, rather than into a stream a LATER session is reading
        # — which would be a wake-up nobody could interpret.
        self._eval_notify = send

        async with anyio.create_task_group() as bg_tg:
            session.bg_task_group = bg_tg
            if evals:
                # Same rule as the admission latch below: a session entered with pending evals gets
                # its one prompt research ask here, but a NOT-DUE answer must not close the window.
                session.research_spawned = bool(self._spawn_research(bg_tg, state))
            try:
                async with send, receive, anyio.create_task_group() as task_group:
                    # TWO groups with two different lifetimes.  `task_group` is session-owned and
                    # still joins on return: it runs the PRODUCERS (`_produce_card_build`,
                    # `_produce_raw_card_stage`), whose in-memory result slots only this session can
                    # drain, and `_card_phase_decide_exit` refuses to leave while one is open.
                    # `eval_task_group` is the RUN-scoped one the spine owns, so an evaluation
                    # outlives the session that admitted it.  The fallback keeps a direct
                    # `_run_card_session(...)` call (tests, embedders) behaving exactly as before:
                    # the evals then land in the session group and are joined on return.
                    session.task_group = task_group
                    session.eval_task_group = self._eval_task_group or task_group
                    while True:
                        session.progressed = False
                        # Transfer the boundary DEBT the eval children publish engine-level.  The
                        # child cannot write it onto a session it may outlive, and consuming it here
                        # is what makes it "one terminal owes ONE outer turn" rather than a latch a
                        # later session inherits.
                        if self._eval_boundary_owed:
                            self._eval_boundary_owed = False
                            session.boundary_owed = True
                        if await self._close_developer_sentinel_once():
                            session.progressed = True
                        self._card_phase_serve_raw_stage(session)
                        if await self._card_phase_drop_stale(session):
                            continue
                        self._card_phase_serve_head(session)
                        if await self._card_phase_admit_evals(session):
                            continue
                        # An operator inject beside the producer when the LIVE build width has a
                        # free slot (`forced_requests.py`, "controls ON THE FLY").
                        if self._card_phase_serve_operator_inject(session):
                            continue
                        if await self._card_phase_request_build(session):
                            continue
                        if self._card_phase_decide_exit(session):
                            break

                        if session.progressed:
                            await anyio.sleep(0)
                            continue
                        # Notifications are only wake-ups.  The next turn always re-READS the log and
                        # derives truth again — re-folding it whenever the tail moved, see
                        # `_fold_current`; a finite poll also observes operator events, which do not
                        # write into this process-local wake-up stream.
                        with anyio.move_on_after(0.5):
                            await receive.receive()
            finally:
                self._eval_notify = None
                if getattr(self, "_concurrent_research_repeat", False):
                    bg_tg.cancel_scope.cancel()
