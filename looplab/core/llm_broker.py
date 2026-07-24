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
    Strategist, concept tagging, lessons, trust/stewards, reports and training monitors.
``engine``
    Documented fail-safe for a new engine-side caller that has not yet been classified.  It is still
    governed by the total budget, so adding an LLM feature cannot silently bypass a finite ceiling.

``total=None`` disables the global ceiling.  This is the compatibility mode used when canonical
``llm_parallel`` is unset (including a legacy-only ``parallel_build`` configuration) and for startup
AUTO: historical overlapped research remains unbounded.  A positive canonical value enables the
shared total.  Per-lane limits remain useful under a finite total; round-robin admission prevents a
permanent build backlog from starving a waiting background lane.
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


def default_llm_lane_limits(total: Optional[int]) -> dict[str, Optional[int]]:
    """Default fair allocation for a finite shared budget.

    Build may consume the full total while it is the only demand.  Background categories are capped
    at one concurrent request each; round-robin gives each queued category the next available turn.
    This is work-conserving (no idle reservation) while still preventing one noisy background producer
    from multiplying itself across the whole budget.
    """
    if total is None:
        return {}
    total = cast(int, _positive_limit(total, label="LLM total"))
    return {
        "build": total,
        "deep_research": 1,
        "novelty_dedup": 1,
        "enrichment": 1,
        "engine": 1,
    }


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
