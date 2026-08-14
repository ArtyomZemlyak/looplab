"""Request-driven Card speculation (docs/23, Layers 5a/5b).

The append-only log remains the queue.  Background producer work may only return an in-memory
``SpecBuildResult``; every selection-affecting event and every speculative ``node_created`` is written
by the main engine task.  The mixin is inert unless both Card selection and a positive, run-pinned
``speculation_depth`` are enabled.
"""
from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

import anyio

from looplab.core import tracing
from looplab.core.advisory_payloads import bounded_cross_run_advisory_receipt
from looplab.core.models import (
    Idea,
    NodeStatus,
    RunState,
    card_ownership_receipt,
    durable_idea_payload, is_developer_error)
from looplab.core.llm_broker import in_llm_lane
from looplab.events.eventstore import EventStoreConcurrencyError, retry_tail_cas
from looplab.events.replay import fold
from looplab.engine.node_build import developer_crash_records
from looplab.events.types import (
    DIAGNOSTIC_EVENTS,
    EV_CARD_ADDED,
    EV_CARD_BUILD_ATTEMPTED,
    EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED,
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
    speculative_card_actions,
    speculative_card_is_fresh,
    speculative_raw_actions,
)

_LOG = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class SpecRawStageResult:
    """One isolated raw-policy proposal awaiting a short main-task Card commit."""

    generation: int
    action: dict[str, Any]
    proposal_state: RunState = field(compare=False, repr=False)
    proposal_authority_seq: int
    proposal_node_ceiling: int
    at_node: int
    source: str
    cue_fence: bytes
    success: bool
    idea: Optional[Idea] = None
    steering_context: tuple[Any, ...] = ()
    cross_run_receipt: dict[str, Any] = field(default_factory=dict)
    audit_events: tuple[tuple[str, dict, Optional[str], Optional[str]], ...] = ()
    error: str = ""


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

        EVERY DIAGNOSTIC EVENT, not just the two LLM accounting rows this used to name. The fence is
        captured before the slow paid `_prepare_node_idea` and compared for EQUALITY at commit
        (`card_reservation.py`), so any row appended in that window discards a proposal the run has
        already paid a Developer call for — reported as "a control/research/lifecycle event won the
        CAS", which is exactly what it was not.
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

        The memo also keys on the `fold` callable ITSELF.  `looplab.engine.speculation.fold` is a
        documented patch seam (tests swap it for a fabricated RunState), and a memo that outlived the
        swap would serve the previous function's answer to the new one — the "test still runs but no
        longer measures anything" failure CLAUDE.md warns about.

        A folded `RunState` is treated as immutable by every consumer: no engine or search module
        assigns to one of its attributes or mutates one of its containers, and this session already
        hands ONE folded state to a background research task that outlives the turn.  Sharing the
        object between two readers of the same tail is therefore exactly the guarantee two equal
        copies gave.  Written only from the MAIN task (the worker-thread folds in
        `_producer_card_reservation` / `_prepare_raw_card_stage` deliberately do not go through here).
        """

        events = self.store.read_all()
        tail = events[-1].seq if events else -1
        memo = self._spec_fold_memo
        if memo is not None and memo[0] is fold and memo[1] == tail and memo[2] == len(events):
            return events, memo[3]
        state = fold(events)
        self._spec_fold_memo = (fold, tail, len(events), state)
        return events, state

    def _session_state(self) -> RunState:
        """`_fold_current` for the callers that need only the folded half."""

        return self._fold_current()[1]

    def _session_gates(self, state: RunState, session: CardSession) -> CardSessionGates:
        """The one computation of a turn's three fold-derived stop conditions, from ONE snapshot."""

        return CardSessionGates(
            terminal_gate=self._terminal_intent(state),
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
    _eval_task_group: Any = None
    _eval_notify: Any = None
    _eval_boundary_owed: bool = False
    _eval_drain_requested: bool = False
    #   `_outer_boundary_served_tail`  the log seq at which a session last handed back for a
    #                          RECURRING producer yield, so the same unchanged condition cannot hand
    #                          back again — see `_card_phase_decide_exit`'s last clause.
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
        while self._evals_inflight():
            await anyio.sleep(0.05)

    def _producer_role_pair(self) -> Optional[tuple[Any, Any]]:
        """Lease one non-primary pair from the Layer-2 role pool.

        ``_build_role_pairs(1)`` is intentionally not used: it returns the primary roles whose
        per-build output slots are shared with repairs and ordinary builds.  The surrounding Card
        session never overlaps a normal build batch, so the cached pool pair is exclusively leased
        for the session and can be safely reused by its single producer.
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
    def _outstanding_requests(state: RunState) -> list[dict]:
        done = max(0, min(int(state.card_builds_done), len(state.card_build_requests)))
        return [dict(request) for request in state.card_build_requests[done:]
                if isinstance(request, Mapping)]

    @classmethod
    def _head_request(cls, state: RunState) -> Optional[dict]:
        outstanding = cls._outstanding_requests(state)
        return outstanding[0] if outstanding else None

    @staticmethod
    def _developer_sentinel(node) -> bool:
        return bool(
            node is not None
            and isinstance(getattr(node, "code", None), str)
            and is_developer_error(node.code)
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
        return bool(state.paused or state.finished or state.stop_requested)

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
        head = self._head_request(state)
        if self._request_key(head) != key or generation != state.search_epoch:
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
        # since the beginning (`orchestrator.py::_create_node` opens `create_node`); speculation is
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
        with self._op_span("card_build", card_id=card_id,
                           card_build_generation=build_generation) as span:
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
            self._reset_developer_footprint(developer)
            if kind == "draft":
                code = self._implement(
                    self._directed_idea(idea.model_copy(deep=True), state),
                    developer=developer, state=state)
            elif kind == "merge":
                parents = [state.nodes[node_id] for node_id in reservation.parent_ids]
                directed = self._directed_idea(idea.model_copy(deep=True), state)
                code = self._implement(
                    directed,
                    parents[0] if self._merge_mode == "ensemble" and parents else None,
                    developer=developer, state=state)
            elif kind == "debug":
                parent = state.nodes[action["parent_id"]]
                repair = getattr(developer, "repair", None)
                if callable(repair) and parent.error and (
                    parent.code or parent.files or self._repo_spec
                ):
                    error = self._repair_error_context(
                        parent.error_reason, parent.error, state=state, node=parent,
                    )
                    code = self._repair(parent, error, state, developer=developer)
                else:
                    code = self._implement(
                        self._directed_idea(idea.model_copy(deep=True), state),
                        parent,
                        developer=developer,
                        state=state,
                    )
            else:
                parent = state.nodes[action["parent_id"]]
                code = self._implement(
                    self._directed_idea(idea.model_copy(deep=True), state),
                    parent,
                    developer=developer,
                    state=state,
                )
            idea, finalized = self._finalize_developer_footprint(idea, developer, code)
            files = dict(getattr(developer, "last_files", {}) or {})
            deleted = tuple(getattr(developer, "last_deleted", []) or [])
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
        except Exception as exc:  # one producer failure must become an explicit give-up result
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
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
                superseded = (
                    latest.search_epoch != result.generation
                    or node_id in latest.aborted_nodes
                    or latest_card is None
                    or latest_card.dropped_reason is not None
                    or latest_card.merged_into is not None
                    or any(
                        parent_id not in latest.nodes
                        or latest.nodes[parent_id].attempt != parent_generation
                        or latest.nodes[parent_id].tombstoned
                        or parent_id in latest.aborted_nodes
                        for parent_id, parent_generation in (
                            (int(parent_id), generation)
                            for parent_id, generation in reserved.parent_generations.items()
                        )
                    )
                )
                transient = (
                    latest.paused
                    or latest.finished
                    or latest.stop_requested
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
                    # This lifecycle is the ONE kind whose node-budget slot can later be refunded, so
                    # it is the one kind that must be able to PROVE it never ran across a crash.
                    # Stamping the promise here (rather than run-wide) keeps every non-speculative
                    # run's `node_created` bytes untouched while making the refund's evidence
                    # per-node: the admission below appends `node_eval_started` before any sandbox
                    # work, and `is_unevaluated_speculative_discard` refuses to refund a node that
                    # carries no promise at all. See `events/types.py::EV_NODE_EVAL_STARTED`.
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
                    self.store.append_many(developer_crash_records(
                        node_id, terminal_node.attempt, result.code,
                        "auto-paused: a Developer session crashed (LLM unreachable or a hard "
                        "error, unresolved within the node) — resume once it's fixed",
                    ), expected_last_seq=tail)
                    self._create_paused = True
                    return None

                # A crash terminal that could not land leaves the node pending: the ordinary
                # crash-repair path still owns it, so exhaustion is simply "not this turn".
                retry_tail_cas(self.store, _plan_terminal, on_exhaust=lambda: None)
        try:
            self._emit_agent_report(node_id, developer=developer)
            self._emit_hypothesis_ranked(node_id, 0, researcher=researcher)
            self._emit_foresight_selected(
                node_id, 0, researcher=researcher, developer=developer,
            )
        finally:
            # `_emit_agent_report` does not consume `last_report`; make pair reuse explicit.
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)

    def _append_card_build_done(
        self,
        request: Mapping[str, Any],
        *,
        node_id: Optional[int] = None,
        skipped: Optional[str] = None,
    ) -> bool:
        """Close only the exact folded head, retrying a moving tail without skipping requests."""

        key = self._request_key(request)
        if key is None or (node_id is None) == (skipped is None):
            return False
        card_id, generation = key
        if skipped is not None and skipped not in {"producer_failed", "stale"}:
            return False
        payload: dict[str, Any] = {"card_id": card_id, "generation": generation}
        if skipped is not None:
            payload["skipped"] = skipped
        else:
            payload.update({"node_id": node_id, "speculative": True})
        def _plan(events, tail) -> bool:
            state = fold(events)
            if self._request_key(self._head_request(state)) != key:
                # Another main-task path may already have closed it.
                return state.card_builds_done >= len(state.card_build_requests)
            with self._id_lock:
                self.store.append(EV_CARD_BUILD_DONE, payload, expected_last_seq=tail)
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
        try:
            self.store.append(EV_CARD_BUILD_ATTEMPTED, {
                "card_id": key[0], "generation": key[1],
                # The queue position this head occupies — see `_on_card_build_attempted`.
                "index": int(state.card_builds_done)})
        except Exception:  # noqa: BLE001 — see the docstring: never block a build on its receipt
            pass

    @staticmethod
    def _head_has_unreconciled_attempt(state: RunState,
                                       key: tuple[str, int]) -> bool:
        """Does the CURRENT open head already carry a producer attempt from a dead process?

        Position-exact on purpose: the same (card_id, generation) can legitimately be re-elected after
        an earlier request for it was closed, and that older — fully reconciled — attempt must not
        quarantine the new head. Callers must first rule out an attempt this process itself started
        (`_spec_build_inflight` / a present `_spec_builds` result).
        """
        index = int(state.card_builds_done)
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

    def _speculative_selection_node_limit(self, state: RunState) -> int:
        """Compensate the pure selector for request slots already removed from the live denominator.

        Engine's translated ``policy.max_nodes`` excludes every unmaterialized durable request so the
        Strategist and ordinary selectors cannot advertise an owned slot. The pure speculative selector
        independently subtracts excluded requests (and a claim temporarily reopens its exact head), so
        add those receipts back at this call boundary to avoid charging them twice.
        """

        return max(0, int(self.policy.max_nodes)) + self._unmaterialized_card_reservations(state)

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
            state.paused
            or state.finished
            or state.stop_requested
            or self._head_request(state) is not None
            or self._speculation_depth_used(
                state, consumed_inflight=consumed_inflight) >= self.speculation_depth
        ):
            return False
        self._refresh_speculation_budget(state)
        if self._node_reservation_slots_remaining(state, events=events) < 1:
            return False
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
        """Reserve and commit an exact head result; never consult the ready-only serial claim."""

        key = self._request_key(request)
        if key is None or result.key != key or not result.success or result.idea is None:
            return "producer_failed", None
        card_id, generation = key
        events = self.store.read_all()
        state = fold(events)
        if self._request_key(self._head_request(state)) != key:
            return "closed", None
        if (
            generation != state.search_epoch
            or self._terminal_intent(state)
            or (
                max_eval_seconds is not None
                and state.total_eval_seconds >= max_eval_seconds
            )
        ):
            return "stale", None
        self._refresh_speculation_budget(state)
        # The exact request head already owns one durable future slot. Convert that ownership into
        # node_building without double-charging it, but never cross a ceiling that was already full
        # when the request arrived (legacy/corrupt prefixes remain pending for budget_extend).
        if self._node_reservation_slots_remaining(
            state, events=events, consume_request=True,
        ) < 1:
            return "budget", None
        selection_limit = self._speculative_selection_node_limit(state)
        if card_budget_used(state) >= selection_limit:
            return "stale", None

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
        if selected_action is None:
            return "stale", None
        card = state.cards.get(card_id)
        if card is None:
            return "stale", None
        from looplab.search.card_selection import card_action
        current_action = card_action(card)
        if current_action is None or current_action != result.action:
            return "stale", None
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
            return "stale", None
        if (
            reservation.idea.card_id != result.idea.card_id
            or reservation.idea.operator != result.idea.operator
            or reservation.idea.params != result.idea.params
            or reservation.idea.space != result.idea.space
            or reservation.idea.eval_profile != result.idea.eval_profile
            or reservation.idea.eval_timeout != result.idea.eval_timeout
        ):
            return "stale", None

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
        except Exception as exc:
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
            return "stale", None
        committed = fold(self.store.read_all()).nodes.get(reservation.node_id)
        if (
            committed is None
            or committed.idea.card_id != card_id
            or committed.speculative is not True
            or committed.card_build_generation != generation
        ):
            return "stale", None
        return "created", committed.id

    def _serve_card_builds(
        self,
        max_eval_seconds: Optional[float] = None,
        *,
        allow_commit: bool = True,
    ) -> bool:
        """Crash-recovery-first main-task service of one durable request."""

        self._ensure_speculation_state()
        state = self._session_state()
        request = self._head_request(state)
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
        if (
            key[1] != state.search_epoch
            or self._terminal_intent(state)
            or budget_exhausted
            or not allow_commit
        ):
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return self._append_card_build_done(request, skipped="stale")
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
                return self._append_card_build_done(request, skipped="stale")
            return False
        if not result.success:
            self._discard_spec_result(self._spec_builds.pop(key, None))
            closed = self._append_card_build_done(request, skipped="producer_failed")
            if closed:
                self._spec_force_outer = True
            return closed
        outcome, node_id = self._claim_requested_card_build(
            request, result, max_eval_seconds,
        )
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
            closed = self._append_card_build_done(request, skipped="stale")
        if closed:
            self._discard_spec_result(self._spec_builds.pop(key, None))
        return closed

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
        self._serve_card_builds(max_eval_seconds, allow_commit=False)
        return True

    async def _produce_card_build(
        self,
        request: Mapping[str, Any],
        roles: tuple[Any, Any],
        notify,
    ) -> None:
        key = self._request_key(request)
        if key is None:
            return
        try:
            try:
                # abandon_on_cancel=False makes pause/abort wait for the entire blocking
                # Developer/provider call even after the main task has durably closed this request as
                # stale. The session's exit gate still counts _spec_build_inflight, so an unavailable
                # provider can make an operator stop take the full transport timeout. Use a genuinely
                # cancellable producer or quarantine/abandon this isolated role pair after cancellation.
                result = await anyio.to_thread.run_sync(
                    # `_start_head_producer` already appended this attempt's `card_build_attempted`
                    # receipt, so a kill anywhere below leaves the head quarantined on resume instead
                    # of silently re-issuing possibly-charged work (see `_serve_card_builds`).
                    functools.partial(self._build_requested_card, dict(request), roles),
                    abandon_on_cancel=False,
                )
            except Exception as exc:  # the main task must still advance the durable gate
                result = SpecBuildResult(
                    key[0], key[1], {}, False, roles=roles,
                    error=producer_error_text(exc),
                )
            self._discard_spec_result(self._spec_builds.get(key))
            self._spec_builds[key] = result
        finally:
            self._spec_build_inflight.discard(key)
            # Notifications are only hints. Never let a full/closing stream block task-group teardown.
            notify_producer(notify, ("producer", key))

    @in_llm_lane("build")
    def _prepare_raw_card_stage(
        self,
        action: Mapping[str, Any],
        proposal_events: list,
        proposal_state: RunState,
        proposal_node_ceiling: int,
        cue_fence: bytes,
        roles: tuple[Any, Any],
    ) -> SpecRawStageResult:
        """Worker-only proposal half: no selection-affecting event may escape this call."""

        raw_action = dict(action)
        generation = proposal_state.search_epoch
        proposal_authority_seq = self._proposal_authority_seq(proposal_events)
        researcher, developer = roles
        source = "engine" if raw_action.get("kind") == "merge" else "researcher"
        self._discard_node_build_telemetry(researcher=researcher, developer=developer)
        audit_events: list[tuple[str, dict, Optional[str], Optional[str]]] = []
        try:
            # The Layer-5 speculative producer's proposal. It is a background worker doing a full
            # paid Researcher call, so it is invisible for exactly as long as the foreground one is —
            # and the beacon must NOT ride the `_capture_proposal_events` sink around it, which
            # buffers until the main task publishes. See `SharedEngineMixin._progress` for why a
            # DIAGNOSTIC row may be appended straight from this worker.
            with self._progress(PROGRESS_STAGE_BUILD, "propose",
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
                proposal_authority_seq=proposal_authority_seq,
                proposal_node_ceiling=proposal_node_ceiling,
                at_node=proposal_node_ceiling,
                source=source,
                cue_fence=cue_fence,
                success=idea is not None,
                idea=idea,
                steering_context=steering,
                cross_run_receipt=receipt,
                audit_events=tuple(audit_events),
                error="proposal rejected" if idea is None else "",
            )
        except Exception as exc:
            return SpecRawStageResult(
                generation=generation,
                action=raw_action,
                proposal_state=proposal_state,
                proposal_authority_seq=proposal_authority_seq,
                proposal_node_ceiling=proposal_node_ceiling,
                at_node=proposal_node_ceiling,
                source=source,
                cue_fence=cue_fence,
                success=False,
                audit_events=tuple(audit_events),
                error=producer_error_text(exc),
            )
        finally:
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)

    async def _produce_raw_card_stage(
        self,
        action: Mapping[str, Any],
        proposal_events: list,
        proposal_state: RunState,
        proposal_node_ceiling: int,
        cue_fence: bytes,
        roles: tuple[Any, Any],
        notify,
    ) -> None:
        try:
            try:
                result = await anyio.to_thread.run_sync(
                    functools.partial(
                        self._prepare_raw_card_stage,
                        dict(action),
                        proposal_events,
                        proposal_state,
                        proposal_node_ceiling,
                        cue_fence,
                        roles,
                    ),
                    abandon_on_cancel=False,
                )
            except Exception as exc:
                # Mirror the request-driven producer guard: one raw proposal fault yields a consumed,
                # non-staged result instead of tearing down the task group and cancelling live evals.
                try:
                    self._discard_node_build_telemetry(
                        researcher=roles[0], developer=roles[1],
                    )
                except Exception:
                    pass
                result = SpecRawStageResult(
                    generation=proposal_state.search_epoch,
                    action=dict(action),
                    proposal_state=proposal_state,
                    proposal_authority_seq=self._proposal_authority_seq(proposal_events),
                    proposal_node_ceiling=proposal_node_ceiling,
                    at_node=proposal_node_ceiling,
                    source="engine" if action.get("kind") == "merge" else "researcher",
                    cue_fence=cue_fence,
                    success=False,
                    error=producer_error_text(exc),
                )
            self._spec_raw_stage_result = result
        finally:
            self._spec_raw_stage_inflight = False
            notify_producer(notify, ("raw_proposal", proposal_state.search_epoch))

    def _serve_raw_card_stage(self) -> tuple[bool, bool]:
        """Main-task-only commit of one prepared proposal and its buffered audit intents."""

        result = self._spec_raw_stage_result
        if result is None:
            return False, False
        self._spec_raw_stage_result = None
        if not result.success or result.idea is None:
            return True, False
        card_id = self._stage_prepared_card(
            result.action,
            result.idea,
            proposal_state=result.proposal_state,
            proposal_authority_seq=result.proposal_authority_seq,
            proposal_node_ceiling=result.proposal_node_ceiling,
            at_node=result.at_node,
            source=result.source,
            steering_context=result.steering_context,
            cross_run_receipt=result.cross_run_receipt,
            proposal_cue_fence=result.cue_fence,
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
                # the only record that this lane paid for it at all.
                for event_type, data, trace_id, span_id in result.audit_events:
                    self.store.append(event_type, data, trace_id=trace_id, span_id=span_id)
            return True, False
        # the Card commit above and these proposal-audit events are separate appends. A crash
        # or append failure after EV_CARD_ADDED leaves an executable durable Card whose novelty/governance
        # audit prefix was silently lost; `_spec_raw_stage_result` was already cleared, so resume cannot
        # repair it. Commit the Card and its bounded audit intents in one tail-fenced append_many, or add a
        # durable proposal receipt plus recovery gate that keeps the Card non-selectable until it is closed.
        for event_type, data, trace_id, span_id in result.audit_events:
            self.store.append(
                event_type,
                data,
                trace_id=trace_id,
                span_id=span_id,
            )
        return True, True

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
            records = developer_crash_records(
                node.id, node.attempt, node.code,
                "auto-paused: recovered a Developer crash before GPU dispatch")
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
        tail = events[-1].seq if events else -1
        try:
            async with self._write_lock:
                self.store.append_many(records, expected_last_seq=tail)
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

    def _start_head_producer(self, current: RunState, session: CardSession) -> bool:
        """Start the exact durable head in the same turn that elected it.

        Waiting for the next loop turn leaves a request visible but not yet executing.
        A fast admitted eval can then cross the search-epoch boundary first and make a
        depth-one prefetch spuriously stale. Registering the producer before the next
        checkpoint preserves the documented live-backlog overlap without changing the
        durable request/commit authority.
        """

        head = self._head_request(current)
        key = self._request_key(head)
        if (
            head is None
            or key is None
            or key in self._spec_build_inflight
            or key in self._spec_builds
        ):
            return False
        # recovery may have terminalized this head's interrupted
        # node_building after it consumed the final physical Node id. The request then
        # has no result but capacity remains zero, so no worker can close it and this
        # session polls forever. Close recovered unbuildable heads before this gate.
        if self._node_reservation_slots_remaining(
            current, consume_request=True,
        ) < 1:
            return False
        roles = self._producer_role_pair()
        if roles is None:
            if self._append_card_build_done(
                head, skipped="producer_failed",
            ):
                session.yield_outer = True
                return True
            return False
        self._spec_build_inflight.add(key)
        # Receipt BEFORE the producer can reach a provider, and after the inflight
        # marker so a main-task service turn in between cannot mistake this process's
        # own fresh attempt for a dead process's unreconciled one.
        self._record_card_build_attempt(current, head)
        try:
            session.task_group.start_soon(
                self._produce_card_build,
                dict(head),
                roles,
                session.notify,
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
        """

        try:
            await self._evaluate(node_id, anyio.CapacityLimiter(1), max_eval_seconds)
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
            if reservation is not None:
                self._clear_eval_resource_reservation(node_id, generation)
                self._release_gpus(reservation.get("gpu_ids"))
            self._eval_inflight.discard((node_id, generation))
            notify_producer(self._eval_notify, ("eval", (node_id, generation)))

    def _card_phase_serve_raw_stage(self, session: CardSession) -> None:
        """Commit one prepared raw proposal, then — where the session may still produce — elect and
        start its producer in the same turn."""

        raw_consumed, raw_staged = self._serve_raw_card_stage()
        if not raw_consumed:
            return
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
                self._start_head_producer(self._session_state(), session)
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
        """Service one durable request head, or start the producer that will close it."""

        current = self._session_state()
        head = self._head_request(current)
        key = self._request_key(head)
        if head is None or key is None:
            return
        # Recovery still links an already-created exact Node before consulting this
        # flag. Once the admitted batch closes, every other head is acknowledged stale
        # without another scorer consult/claim crossing the outer cadence boundary.
        if self._serve_card_builds(
            session.max_eval_seconds,
            allow_commit=session.open_for_production(
                self._session_gates(current, session)),
        ):
            session.progressed = True
            if self._spec_force_outer:
                session.yield_outer = True
                self._spec_force_outer = False
            return
        # `_serve_card_builds` can return False having appended (a committed build whose
        # `card_build_done` close then lost its CAS is the live case), so both the head AND the
        # gates below are re-derived from a snapshot taken after it, never from the one above.
        current = self._session_state()
        head = self._head_request(current)
        key = self._request_key(head)
        if (
            head is not None
            and key is not None
            and session.open_for_production(self._session_gates(current, session))
            and key not in self._spec_build_inflight
            and key not in self._spec_builds
        ):
            if self._start_head_producer(current, session):
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
        consumer_active = bool(
            session.eval_inflight
            or any(self._session_admissible(node, current, session)
                   for node in current.pending_nodes())
        )
        if not (
            consumer_active
            and session.open_for_production(self._session_gates(current, session))
            and self._head_request(current) is None
            and not self._spec_build_inflight
            and not self._spec_raw_stage_inflight
            and self._spec_raw_stage_result is None
            and self._speculation_depth_used(
                current,
                consumed_inflight=session.eval_inflight,
            ) < self.speculation_depth
        ):
            return False
        # `_request_card_build` consults the Card scorer. Drain any newly-stale
        # speculative prefix immediately before that consult, not just per session turn.
        if await self._drop_stale_speculation(
            eval_inflight=session.eval_inflight,
        ):
            await anyio.sleep(0)
            return True
        requested = self._request_card_build(
            consumed_inflight=session.eval_inflight,
        )
        if not requested:
            # No durable Card owns the counterfactual next action. Propose and stage
            # that raw lane in the main task while GPU children continue in worker
            # threads; then request the exact receipt from a fresh fold. Card staging
            # owns its own tail/generation/parent CAS and may safely decline a stale
            # proposal if an eval changes the search state during the paid call.
            # Selection and proposal share one immutable log snapshot.  A second
            # read here would let an old raw action inherit a newer best/parent/cue
            # fence and make the main-task commit validate the wrong authority.
            # Deliberately NOT `_fold_current`: this pair is the proposal's OWN authority snapshot,
            # handed whole to a worker that outlives the turn, and its explicit read/fold pairing is
            # what `test_raw_action_selection_and_worker_share_one_proposal_snapshot` reads.
            proposal_events = self.store.read_all()
            proposal_state = fold(proposal_events)
            if (
                self._head_request(proposal_state) is None
                and self._speculation_depth_used(
                    proposal_state,
                    consumed_inflight=session.eval_inflight,
                ) < self.speculation_depth
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
                roles = self._producer_role_pair()
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
                    try:
                        session.task_group.start_soon(
                            self._produce_raw_card_stage,
                            dict(raw_actions[0]),
                            proposal_events,
                            proposal_state,
                            proposal_node_ceiling,
                            self._proposal_cue_fence(proposal_state),
                            roles,
                            session.notify,
                        )
                    except BaseException:
                        self._spec_raw_stage_inflight = False
                        raise
                    session.progressed = True
                else:
                    # Unsupported raw interception (or no isolated pair) must
                    # degrade at the outer serial boundary, never poll/re-propose.
                    session.yield_outer = True
        if requested:
            # The election APPENDED, so this re-folds (see `_fold_current`).
            self._start_head_producer(self._session_state(), session)
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
        )
        # PRODUCER work this session owns and no other turn can adopt: a durable request head it
        # elected, a `node_building` marker, an isolated build/raw-stage worker holding an
        # in-memory result slot.  Evals are deliberately NOT in here any more — see below.
        producer_inflight = bool(any((outstanding, building, memory_pending)))
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
        if (
            session.yield_outer
            and not session.boundary_owed
            and not gates.stopping
            and session.eval_inflight
            and not producer_inflight
        ):
            tail = events[-1].seq if events else -1
            if tail == self._outer_boundary_served_tail:
                return False              # nothing new to hand back; poll instead of ping-ponging
            self._outer_boundary_served_tail = tail
        return not producer_inflight

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
