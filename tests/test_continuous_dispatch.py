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
        self._eval_parallel = max_parallel
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
    assert stub.peak <= stub._eval_parallel          # … and was never exceeded


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


class _GpuDispatchStub(_DispatchStub):
    """Adds the resource seam so the scan actually has to fit a footprint.

    Node 0 is WIDE (wants the whole pool); every other node wants one GPU. That is exactly the shape
    work-conserving first-fit starves: each small job consumes a partial release, so all four GPUs are
    never free at the same instant and the wide head keeps losing its turn.
    """

    def __init__(self, *, max_parallel, durations, gpus, wide_node=0,
                 held_elsewhere=0, released_after=0):
        super().__init__(max_parallel=max_parallel, durations=durations)
        self._total = gpus
        # `held_elsewhere` models the realistic case: this batch is dispatched while evals from the
        # PREVIOUS one still own GPUs. Without it the pool is empty at t=0 and even the widest head
        # fits on the first scan, which is precisely the case that never starves.
        self._free = gpus - held_elsewhere
        self._held = held_elsewhere
        self._released_after = released_after
        self._attempts = 0
        self._wide = wide_node
        self._epoch = 0

    def _want(self, node_id: int) -> int:
        return self._total if node_id == self._wide else 1

    def _gpu_pool_epoch(self):
        return self._epoch

    def _wait_for_gpu_change(self, epoch):
        return None

    def _card_resource_pin_for_node(self, state, node):
        return None

    def _try_reserve_node_resources(self, node, resource_pin=None):
        self._attempts += 1
        if self._held and self._attempts > self._released_after:
            self._free += self._held          # the previous batch finally gave its GPUs back
            self._held = 0
            self._epoch += 1
        want = self._want(node.id)
        if want > self._free:
            return None
        self._free -= want
        self._epoch += 1
        return {"gpu_ids": list(range(want)), "node_id": node.id}

    def _node_resource_reservation_is_current(self, state, node, reservation):
        return True

    def _register_eval_resource_reservation(self, node_id, generation, reservation):
        return None

    def _clear_eval_resource_reservation(self, node_id, generation):
        return None

    def _release_gpus(self, gpu_ids):
        self._free = min(self._total, self._free + len(gpu_ids or ()))
        self._epoch += 1


def test_a_wide_head_is_not_starved_by_a_stream_of_small_jobs(monkeypatch):
    """Bounded aging: after a few bypasses the head gets exclusive claim on releases.

    Work-conserving first-fit alone lets every partial release be consumed by the one-GPU jobs behind
    the head, so the wide node only starts once the queue is otherwise EMPTY — it is served last no
    matter how long it has waited.
    """
    from looplab.core.models import NodeStatus
    from looplab.engine.orchestrator import Engine

    smalls = list(range(1, 21))
    # STAGGERED on purpose. With identical durations the four running siblings finish together and
    # the pool is momentarily empty, which hands the head its GPUs by accident. Staggering means a
    # small is always running, so all four GPUs are never free at the same instant — the exact shape
    # first-fit starves.
    durations = {0: 0.02, **{n: 0.02 + 0.004 * n for n in smalls}}
    stub = _GpuDispatchStub(max_parallel=4, durations=durations, gpus=4,
                            held_elsewhere=2, released_after=2)
    nodes = {
        n: types.SimpleNamespace(id=n, attempt=0, status=NodeStatus.pending, tombstoned=False)
        for n in [0, *smalls]
    }
    monkeypatch.setattr(
        "looplab.engine.orchestrator.fold",
        lambda events: types.SimpleNamespace(
            total_eval_seconds=0.0, aborted_nodes=set(), nodes=nodes,
            paused=False, finished=False, stop_requested=None))
    anyio.run(Engine._dispatch_evals, stub, [{"node_id": n} for n in [0, *smalls]], None, None)

    assert sorted(stub.ran) == [0, *smalls], "every eval must still run"
    assert stub.ran.index(0) < len(smalls), (
        "the wide head was served last — every partial release went to the small jobs behind it")
