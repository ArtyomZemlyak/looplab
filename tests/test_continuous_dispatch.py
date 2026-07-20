"""Continuous GPU dispatch — the parallel branch of `Engine._dispatch_evals` keeps its `max_parallel`
slot pool FULL: the instant a short eval frees its slot (and its GPU), the next queued eval is admitted,
instead of the old `started >= max_parallel: break` that ran only the first `max_parallel` and deferred
the rest to a future spine iteration (idling every GPU a short eval freed while a long sibling ran — the
10h-vs-1h case). These drive `_dispatch_evals` through a light stub host so no real Engine/eval is needed.

The barrier is preserved (the inner task group still joins the WHOLE batch), so nothing here touches the
`pending_nodes()`-keyed spine guarantees; the fix is purely that a freed slot refills from the same batch.
"""
from __future__ import annotations

import types

import anyio


class _DispatchStub:
    """Minimal host for `Engine._dispatch_evals` (unbound method, this as `self`). `_evaluate` is a
    stub coroutine that records live/peak concurrency and sleeps a per-node duration; the private
    single-token limiter it receives is a no-op, so the outer semaphore is the only bound under test."""

    def __init__(self, *, max_parallel, durations):
        self.max_parallel = max_parallel
        self._durations = durations
        self._concurrent_research_repeat = False
        self.store = types.SimpleNamespace(read_all=lambda: [])
        self.live = 0
        self.peak = 0
        self.ran: list[int] = []

    # `_dispatch_evals` calls these on `self`; keep them inert for the concurrency test.
    def _spawn_research(self, tg, state):
        pass

    def _skip_if_aborted(self, a, cur):
        return False

    async def _evaluate(self, node_id, limiter, max_es):
        async with limiter:                       # per-eval CapacityLimiter(1) -> a no-op slot
            self.live += 1
            self.peak = max(self.peak, self.live)
            await anyio.sleep(self._durations[node_id])
            self.ran.append(node_id)
            self.live -= 1


def _drive(stub, evals, max_es, monkeypatch, *, total_eval_seconds=0.0):
    # `_dispatch_evals` folds per admission; the stub state only needs the fields the branch reads.
    monkeypatch.setattr(
        "looplab.engine.orchestrator.fold",
        lambda events: types.SimpleNamespace(
            total_eval_seconds=total_eval_seconds, aborted_nodes=set(), nodes={}))
    from looplab.engine.orchestrator import Engine
    anyio.run(Engine._dispatch_evals, stub, evals, None, max_es)


def test_freed_slot_refills_from_the_same_batch(monkeypatch):
    # max_parallel=2, 5 evals: node 0 is LONG, the rest are short. The old break would run only {0,1}
    # this batch; continuous dispatch refills each freed slot, so ALL FIVE run and concurrency is
    # capped at exactly 2 the whole time.
    durations = {0: 0.30, 1: 0.03, 2: 0.03, 3: 0.03, 4: 0.03}
    stub = _DispatchStub(max_parallel=2, durations=durations)
    _drive(stub, [{"node_id": i} for i in range(5)], None, monkeypatch)
    assert sorted(stub.ran) == [0, 1, 2, 3, 4]        # every eval ran — the refill (not just the first 2)
    assert stub.peak == 2                             # the pool filled …
    assert stub.peak <= stub.max_parallel            # … and was never exceeded


def test_batch_that_fits_the_pool_runs_all_without_blocking(monkeypatch):
    # batch (3) <= max_parallel (4): admits all with no refill wait, peak bounded by the batch size.
    durations = {0: 0.02, 1: 0.02, 2: 0.02}
    stub = _DispatchStub(max_parallel=4, durations=durations)
    _drive(stub, [{"node_id": i} for i in range(3)], None, monkeypatch)
    assert sorted(stub.ran) == [0, 1, 2]
    assert stub.peak == 3                             # all three ran concurrently, none deferred


def test_eval_budget_guard_admits_nothing_once_spent(monkeypatch):
    # The per-admission budget guard: with total_eval_seconds already over max_es, the producer breaks
    # before admitting a single eval (and the empty task group exits cleanly — never hangs).
    stub = _DispatchStub(max_parallel=3, durations={0: 0.02, 1: 0.02})
    _drive(stub, [{"node_id": 0}, {"node_id": 1}], 1.0, monkeypatch, total_eval_seconds=100.0)
    assert stub.ran == []                             # budget spent -> nothing admitted
    assert stub.peak == 0


def test_refill_rechecks_budget_after_waiting_for_a_slot(monkeypatch):
    # Node 2 reaches the producer while both slots are occupied. Node 0 then finishes and crosses the
    # budget while the producer is asleep in slots.acquire(); the freed slot must NOT admit node 2 from
    # the stale pre-wait fold.
    stub = _DispatchStub(max_parallel=2, durations={0: 0.02, 1: 0.15, 2: 0.01})
    stub.total_eval_seconds = 0.0
    original_evaluate = stub._evaluate

    async def _evaluate_and_charge(node_id, limiter, max_es):
        await original_evaluate(node_id, limiter, max_es)
        if node_id == 0:
            stub.total_eval_seconds = 100.0

    stub._evaluate = _evaluate_and_charge
    monkeypatch.setattr(
        "looplab.engine.orchestrator.fold",
        lambda events: types.SimpleNamespace(
            total_eval_seconds=stub.total_eval_seconds, aborted_nodes=set(), nodes={}),
    )
    from looplab.engine.orchestrator import Engine

    anyio.run(
        Engine._dispatch_evals,
        stub,
        [{"node_id": 0}, {"node_id": 1}, {"node_id": 2}],
        None,
        50.0,
    )
    assert sorted(stub.ran) == [0, 1]
    assert stub.peak == 2
