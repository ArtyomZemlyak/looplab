"""Run-local concurrency admission for outbound LLM requests.

The broker is deliberately transport-adjacent but client-agnostic.  Engine producers select a
lane through a :class:`contextvars.ContextVar`; ``OpenAICompatibleClient`` and ``LiteLLMClient``
borrow immediately around each real provider request.  Scoping an entire producer (for example a
node build) therefore labels its requests without holding capacity while it reads files, runs tools,
or enters a nested novelty pass.

The closed lane vocabulary is intentionally small:

``build``
    Researcher/Developer work, including the foresight panel invoked by a proposal.
``deep_research``
    Deep-research synthesis and its evidence verification.
``novelty_dedup``
    Novelty judging/re-proposal and hypothesis consolidation.
``enrichment``
    Strategist, concept tagging, lessons, trust/stewards, reports and the two live-log watchdogs.
``engine``
    Foreground engine-side work that is not a build, plus the documented fail-safe for a new caller
    that has not yet been classified.  It is still governed by the total budget, so adding an LLM
    feature cannot silently bypass a finite ceiling, but it carries no background cap — the per-eval
    repair loop and the per-eval inter-stage check run here, and capping them would serialize an
    otherwise parallel eval batch (see ``default_llm_lane_limits``).

THREE OF THESE LANES ARE CAPPED AT ONE CONCURRENT REQUEST, and the cap is a claim about the
PRODUCER, not about the kind of prose it asks for.  A lane earns a background cap only when every
producer that declares it is a CADENCE or WATCHDOG producer: one that runs BESIDE the main task
(its own task group / a monitor loop), whose latency no caller is waiting on, and whose count grows
with the number of concurrent evaluations rather than with the operator's budget.  A producer the
EVAL PATH blocks on is the opposite of that on every clause, so it belongs in ``engine`` however
enrichment-flavoured its prompt is — ``BACKGROUND_LANE_PRODUCERS`` below is the machine-checked
spelling of that rule.

``total=None`` disables the global ceiling.  This is the compatibility mode used when canonical
``llm_parallel`` is unset (including a legacy-only ``parallel_build`` configuration) and for startup
AUTO: the foreground lanes (``build``, ``engine``) remain unbounded, exactly as before the broker
existed.  A positive canonical value enables the shared total.  The BACKGROUND lane caps are
independent of that ceiling and apply either way — see ``default_llm_lane_limits`` for why the two
must not be welded together.  Round-robin admission prevents a permanent build backlog from starving
a waiting background lane.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, Optional, TypeVar, cast


LLM_LANES = ("build", "deep_research", "novelty_dedup", "enrichment", "engine")
LLM_FALLBACK_LANE = "engine"


def normalize_llm_lane(value: object) -> str:
    """Return a bounded lane name; unknown producer labels use the governed fallback."""
    lane = str(value or "").strip().lower()
    return lane if lane in LLM_LANES else LLM_FALLBACK_LANE


def _positive_limit(value: object, *, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer or None")
    return value


def normalize_llm_lane_limits(value: Optional[Mapping[str, Optional[int]]]) -> dict[str, Optional[int]]:
    """Validate the additive lane-allocation shape without expanding the config/API contract.

    Missing lanes are unbounded *within* the total. Unknown names are rejected rather than silently
    creating an ungoverned queue or growing attacker-controlled broker state.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("LLM lane limits must be a mapping")
    out: dict[str, Optional[int]] = {}
    for raw_lane, raw_limit in value.items():
        if not isinstance(raw_lane, str) or raw_lane not in LLM_LANES:
            raise ValueError(f"unknown LLM lane: {raw_lane!r}")
        out[raw_lane] = _positive_limit(raw_limit, label=f"LLM lane {raw_lane!r}")
    return out


# The background producers: cadence/watchdog work that runs BESIDE the main task rather than being
# it.  Each is capped at one concurrent request whether or not a finite total exists — the cap is what
# stops one such producer from multiplying itself across every concurrent evaluation.
_BACKGROUND_LANE_LIMITS = {"deep_research": 1, "novelty_dedup": 1, "enrichment": 1}

# The REGISTRY for the capped lanes (CLAUDE.md "duck-typed seams are REGISTRY-GUARDED"): every
# `@in_llm_lane("<capped lane>")` producer in `looplab/engine/`, spelled `<module>.py::<function>`.
# `tests/test_llm_broker.py` source-scans the engine package and asserts BOTH directions — an
# unregistered producer in a capped lane is a red test, and a registered name with no decorator left
# is registry rot.
#
# The cap is a claim about WHERE the producer runs, and a lane label is a bare string that no type
# checker reads, so the one failure this registry exists to stop is a producer joining a capped lane
# because its PROMPT looks like enrichment.  That is exactly how the per-eval inter-stage check
# (`engine/eval_stages.py::_stage_check_fn::_check`) came to sit here: its docstring already said it
# "Runs inside the eval worker thread, so complete_text blocks there", so N concurrent evals'
# stage checks serialized at one and each queued behind whichever watchdog held the permit
# (measured: 4 evals, peak 1, 4x the wall time).  It lives in the uncapped `engine` lane now,
# beside the per-eval repair loop that inherits that lane as the fallback.
#
# ADDING A NAME HERE IS THE DECISION, not the paperwork: it asserts the producer runs beside the main
# task, that nothing on the eval or build path is blocked on its latency, and that its concurrency
# would otherwise scale with the eval width.  If any of those is false, use `engine`.
BACKGROUND_LANE_PRODUCERS: dict[str, tuple[str, ...]] = {
    # Deep-research synthesis and its evidence verification: a research-cadence task.
    "deep_research": (
        "research_cadence.py::_compute_deep_research",
        "research_cadence.py::_record_deep_research",
    ),
    # Novelty judging / re-proposal and hypothesis-board consolidation: cadence gates on the
    # proposal path, invoked once per turn by the main task rather than per concurrent eval.
    "novelty_dedup": (
        "novelty.py::_apply_novelty_gate",
        "research_cadence.py::_maybe_merge_hypotheses",
    ),
    "enrichment": (
        # The two per-eval live-log WATCHDOGS — the producers the cap was written for. Each runs in
        # its own background task beside a running evaluation, and there is one pair per concurrent
        # eval, so without a cap their count is the eval width times two.
        "asha_monitor.py::_monitor_asha",
        "train_monitor.py::_monitor_training",
        # Cross-run memory / lessons / claims / curation: end-of-run and mid-run cadence work on the
        # MAIN task. Capped because none of it is on the critical path of a node — a slow lessons
        # pass must not spend the provider budget a build or a repair is waiting for.
        "orchestrator.py::_write_reflection_note",
        "orchestrator.py::_reflect_lessons",
        "orchestrator.py::_comparative_lessons",
        "orchestrator.py::_maybe_distill_lessons",
        "orchestrator.py::_maybe_refresh_lessons",
        "orchestrator.py::_maybe_reconcile_lessons",
        "orchestrator.py::_causal_meta_note",
        "orchestrator.py::_store_research_claims",
        "orchestrator.py::_store_concept_curation",
        "orchestrator.py::_store_claim_curation",
        "orchestrator.py::_store_task_facets",
        # Report writing and the Strategist/coverage/tie-break cadence: same shape — bounded-cadence
        # main-task work whose latency no node is waiting on.
        "research_cadence.py::_write_report_with_seq",
        "strategy.py::_maybe_snapshot_concept_coverage",
        "strategy.py::_maybe_verify_ties",
        "strategy.py::_maybe_consult_strategist",
    ),
}


def default_llm_lane_limits(total: Optional[int]) -> dict[str, Optional[int]]:
    """Default fair allocation, with or without a finite shared budget.

    Build may consume the full total while it is the only demand.  Background categories are capped
    at one concurrent request each; round-robin gives each queued category the next available turn.
    This is work-conserving (no idle reservation) while still preventing one noisy background producer
    from multiplying itself across the whole budget.

    ``total=None`` (AUTO, or a legacy-only ``parallel_build``) keeps the historical UNBOUNDED total,
    but still applies the background caps.  Welding the two together made the cap inert exactly where
    it was needed most: both live-log watchdogs are ``@in_llm_lane("enrichment")``, and under the
    shipped AUTO default the broker was returned ``{}`` — not merely unbounded but DISABLED
    (``enabled`` is False, so ``llm_request_permit`` never borrows), which made the lane annotation
    decorative.  On a 2-GPU box that is 2 concurrent evals x 2 LLM-calling watchdogs = 4 unbounded
    background calls competing with build and repair; on 8 GPUs, 16.  The per-node backstops (200
    monitor calls, 20 judge calls) are per NODE, not per run, so nothing else bounded it.

    ``build`` and ``engine`` stay unbounded without a total, and that asymmetry is deliberate: under a
    finite total their ``1`` is a FAIRNESS share of a budget the operator asked for, while with no
    budget it would be an absolute serialization of the main task's own foreground work — including
    the per-eval repair loop, which inherits the ``engine`` fallback lane and would then serialize
    across an otherwise parallel eval batch.  A cap where there is no budget to be fair about is a
    throughput regression, not a bound on background spend.

    That argument is a property of the PRODUCER, so it applies to every eval-path producer and not
    just to the ones that reach ``engine`` by falling back to it.  Unwelding the caps from the total
    turned the same asymmetry into a live regression for the one eval-path producer that had
    declared a capped lane by hand (the per-eval inter-stage check); ``BACKGROUND_LANE_PRODUCERS``
    is the registry that keeps the next one from repeating it.
    """
    if total is None:
        return {"build": None, **_BACKGROUND_LANE_LIMITS, "engine": None}
    total = cast(int, _positive_limit(total, label="LLM total"))
    return {"build": total, **_BACKGROUND_LANE_LIMITS, "engine": 1}


@dataclass(frozen=True)
class _Ticket:
    seq: int
    lane: str


class LLMConcurrencyBroker:
    """One dynamically-resizable, atomic total+lane admission controller.

    A single ``threading.Condition`` owns queues, capacity and counters.  There are no nested
    semaphores and no acquire-order inversion: one critical section decides both total and lane
    eligibility.  Reconfiguration mutates this object in place; lowering limits never revokes an
    existing borrower, it merely waits for usage to fall below the new ceiling before admitting more.
    """

    def __init__(self, total: Optional[int] = None,
                 lane_limits: Optional[Mapping[str, Optional[int]]] = None):
        self._condition = threading.Condition()
        self._total = _positive_limit(total, label="LLM total")
        self._lane_limits = normalize_llm_lane_limits(lane_limits)
        self._queues: dict[str, deque[_Ticket]] = {lane: deque() for lane in LLM_LANES}
        self._borrowed = 0
        self._borrowed_by_lane = {lane: 0 for lane in LLM_LANES}
        self._peak = 0
        self._peak_by_lane = {lane: 0 for lane in LLM_LANES}
        self._next_seq = 0
        self._last_lane_index = len(LLM_LANES) - 1

    @property
    def enabled(self) -> bool:
        with self._condition:
            return self._total is not None or any(v is not None for v in self._lane_limits.values())

    def reconfigure(self, *, total: Optional[int],
                    lane_limits: Optional[Mapping[str, Optional[int]]] = None) -> None:
        """Atomically replace limits and wake waiters; outstanding loans remain valid."""
        normalized_total = _positive_limit(total, label="LLM total")
        normalized_lanes = (None if lane_limits is None
                            else normalize_llm_lane_limits(lane_limits))
        with self._condition:
            self._total = normalized_total
            if normalized_lanes is not None:
                self._lane_limits = dict(normalized_lanes)
            self._condition.notify_all()

    def _has_capacity_locked(self, lane: str) -> bool:
        if self._total is not None and self._borrowed >= self._total:
            return False
        lane_limit = self._lane_limits.get(lane)
        return lane_limit is None or self._borrowed_by_lane[lane] < lane_limit

    def _next_eligible_lane_locked(self) -> Optional[str]:
        if self._total is not None and self._borrowed >= self._total:
            return None
        for offset in range(1, len(LLM_LANES) + 1):
            idx = (self._last_lane_index + offset) % len(LLM_LANES)
            lane = LLM_LANES[idx]
            if self._queues[lane] and self._has_capacity_locked(lane):
                return lane
        return None

    @contextmanager
    def borrow(self, lane: str) -> Iterator[None]:
        """Borrow one atomic total+lane permit, FIFO in-lane and round-robin cross-lane."""
        lane = normalize_llm_lane(lane)
        with self._condition:
            ticket = _Ticket(self._next_seq, lane)
            self._next_seq += 1
            self._queues[lane].append(ticket)
            admitted = False
            try:
                while True:
                    selected = self._next_eligible_lane_locked()
                    if selected == lane and self._queues[lane][0] is ticket:
                        self._queues[lane].popleft()
                        self._borrowed += 1
                        self._borrowed_by_lane[lane] += 1
                        admitted = True
                        self._peak = max(self._peak, self._borrowed)
                        self._peak_by_lane[lane] = max(
                            self._peak_by_lane[lane], self._borrowed_by_lane[lane])
                        self._last_lane_index = LLM_LANES.index(lane)
                        # more total capacity may remain. Wake another lane head now
                        # rather than waiting for release and accidentally serializing total>1.
                        self._condition.notify_all()
                        break
                    self._condition.wait()
            except BaseException:
                # cancellation/KeyboardInterrupt while queued must not leave a dead
                # lane-head ticket behind. Such a ghost is permanently selected by round-robin but
                # has no thread left to consume it, poisoning this lane (and often the whole total).
                if admitted:
                    self._borrowed -= 1
                    self._borrowed_by_lane[lane] -= 1
                else:
                    try:
                        self._queues[lane].remove(ticket)
                    except ValueError:
                        pass
                self._condition.notify_all()
                raise
        try:
            yield
        finally:
            with self._condition:
                self._borrowed -= 1
                self._borrowed_by_lane[lane] -= 1
                self._condition.notify_all()

    def snapshot(self) -> dict:
        """Thread-safe diagnostics used by tests/tracing; no mutable internals escape."""
        with self._condition:
            return {
                "enabled": self._total is not None or any(
                    v is not None for v in self._lane_limits.values()),
                "total": self._total,
                "lane_limits": dict(self._lane_limits),
                "borrowed": self._borrowed,
                "borrowed_by_lane": dict(self._borrowed_by_lane),
                "waiting_by_lane": {lane: len(q) for lane, q in self._queues.items()},
                "peak": self._peak,
                "peak_by_lane": dict(self._peak_by_lane),
            }


_CURRENT_BROKER: contextvars.ContextVar[Optional[LLMConcurrencyBroker]] = contextvars.ContextVar(
    "looplab_llm_broker", default=None)
_CURRENT_LANE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "looplab_llm_lane", default=LLM_FALLBACK_LANE)


@contextmanager
def llm_broker_scope(broker: Optional[LLMConcurrencyBroker]) -> Iterator[None]:
    token = _CURRENT_BROKER.set(broker)
    try:
        yield
    finally:
        _CURRENT_BROKER.reset(token)


@contextmanager
def llm_lane_scope(lane: str) -> Iterator[None]:
    token = _CURRENT_LANE.set(normalize_llm_lane(lane))
    try:
        yield
    finally:
        _CURRENT_LANE.reset(token)


@contextmanager
def llm_request_permit() -> Iterator[None]:
    """Borrow for the current outbound request, or no-op outside a broker-scoped engine."""
    broker = _CURRENT_BROKER.get()
    if broker is None:
        yield
        return
    with broker.borrow(_CURRENT_LANE.get()):
        yield


def current_llm_lane() -> str:
    """Expose the normalized current lane for diagnostics and context-propagation tests."""
    return normalize_llm_lane(_CURRENT_LANE.get())


F = TypeVar("F", bound=Callable)


def in_llm_lane(lane: str) -> Callable[[F], F]:
    """Label a sync or async producer without borrowing capacity around the producer itself."""
    normalized = normalize_llm_lane(lane)

    def decorate(func: F) -> F:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapped(*args, **kwargs):
                with llm_lane_scope(normalized):
                    return await func(*args, **kwargs)
            return cast(F, async_wrapped)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            with llm_lane_scope(normalized):
                return func(*args, **kwargs)
        return cast(F, wrapped)

    return decorate
