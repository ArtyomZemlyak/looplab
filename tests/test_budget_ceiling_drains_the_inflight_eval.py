"""A scored node must not be lost because the spend ceiling fired while it was being scored.

MEASURED on the 2026-08-24 campaign (`runs-B`, read-only, re-derived for this file). Five of the
twenty task-arms finished with one more `score.log` on disk than they had `node_evaluated` events:

    task                    node dirs   score.log   node_evaluated   lost score (champion)
    integer_factorization       4           4             3          4.0958  (8.3255)
    spectral_clustering         2           2             1          invalid results -> 0.0 (0.0)
    max_clique_cpsat            7           7             6          invalid results -> 0.0 (31.664)
    min_dominating_set          3           3             2          1.0804  (7.2265)
    multi_dim_knapsack          5           5             4          2.8004  (2.8586)

All five event tails are the SAME five rows: `node_eval_started` -> `workspace_seeded` ->
`research_attempted` -> one research `llm_usage` -> a long silence while the evaluation runs -> the
ceiling. The evaluation finished and wrote its score to disk; the loop never saw a result it had
already paid for. `multi_dim_knapsack` is the near miss — 2.8004 against a champion of 2.8586.

THE MECHANISM these tests pin. `anyio.to_thread.run_sync` is `abandon_on_cancel=False` for the eval
worker, i.e. SHIELDED, so a cancellation aimed at the evaluation does not abandon it: the subprocess
runs to completion and writes its score. The cancellation is delivered at the first checkpoint AFTER
that thread returns — i.e. between the eval finishing and its single `EV_NODE_EVALUATED` append.
Cancelling therefore saved nothing (the compute was already spent) and lost the only durable record
of it.

Both halves are pinned, and the second matters as much as the first: the run must still STOP. Every
test below that proves a score survives has a sibling proving the ceiling still ends the run with
the same exception it always raised.
"""
from __future__ import annotations

import threading
import types

import anyio
import pytest

from looplab.core.errors import budget_stop_leaf
from looplab.core.llm import BudgetExceeded
from looplab.engine.orchestrator import Engine, _DeferredBudgetStop

CEILING = ("LLM spend ceiling reached: $1.0031 of the $1.0000 set by `llm_budget_usd`. "
           "The run stops here rather than spending more.")


# --------------------------------------------------------------------------------- the shared fake

class _PaidEval:
    """One evaluation, reproducing the real shape of the window the score was lost in.

    `_score_written` is the subprocess writing `score.log` inside the SHIELDED worker thread; the
    append after it is `engine/evaluate.py`'s single `EV_NODE_EVALUATED`, guarded by the engine's
    write lock — which is a cancellation checkpoint, and is exactly where the cancellation landed.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.score_on_disk = False
        self.terminals: list[str] = []
        self._lock = anyio.Lock()

    def _blocking_eval(self) -> None:
        self.entered.set()
        self.release.wait(5.0)
        self.score_on_disk = True          # score.log is now on disk; the money is spent

    async def run(self, node_id: int = 0) -> None:
        # abandon_on_cancel=False == the engine's own eval hop: a cancel cannot abandon it.
        await anyio.to_thread.run_sync(self._blocking_eval, abandon_on_cancel=False)
        async with self._lock:             # the first checkpoint after the eval returns
            self.terminals.append(f"node_evaluated:{node_id}")


# ------------------------------------------------------------- Engine.run (the Card-mode path)

class _RunHost:
    """A host for the REAL `Engine.run` frame, driven as an unbound method.

    Only the members `run` itself touches are provided, so the structure under test — the
    run-scoped `eval_tg`, the drain hook, the group unwrap — is the shipped code and not a copy.
    """

    _eval_task_group = None
    _drain_inflight_evaluation = Engine._drain_inflight_evaluation
    _evals_inflight = Engine._evals_inflight
    _drain_adopted_evals = Engine._drain_adopted_evals

    def __init__(self, failure: BaseException):
        self._failure = failure
        self._eval_inflight: set[tuple[int, int]] = set()
        self._eval_drain_requested = False
        self.eval = _PaidEval()

    def _ensure_speculation_state(self) -> None:
        pass

    async def _eval_child(self) -> None:
        self._eval_inflight.add((0, 0))
        try:
            await self.eval.run()
        finally:
            self._eval_inflight.discard((0, 0))

    async def _run_with_llm_broker(self):
        # An adopted evaluation is admitted into the RUN-scoped group, exactly as `_card_eval_one`
        # is, and then the ceiling fires from the overlapped research in the SESSION's group.
        self._eval_task_group.start_soon(self._eval_child)
        await anyio.to_thread.run_sync(self.eval.entered.wait, abandon_on_cancel=True)
        self.eval.release.set()
        raise self._failure


def _wrapped(exc: BaseException) -> BaseException:
    """How the ceiling actually arrives: wrapped by the CardSession's own task group."""
    return BaseExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [exc])


def test_a_score_already_paid_for_survives_the_ceiling():
    """THE DEFECT. Without the drain the evaluation is cancelled at the checkpoint after its
    shielded worker returns — score on disk, terminal never written."""
    host = _RunHost(_wrapped(BudgetExceeded(CEILING)))
    with pytest.raises(BudgetExceeded):
        anyio.run(Engine.run, host)
    assert host.eval.score_on_disk, "the eval must reach the state the campaign observed"
    assert host.eval.terminals == ["node_evaluated:0"], "the paid-for result was discarded again"


def test_the_ceiling_still_ends_the_run():
    """THE OTHER HALF, and it is not a formality: a drain that swallowed the stop would turn a
    budgeted run into an unbounded one. Same class, same sentence, still terminal."""
    host = _RunHost(_wrapped(BudgetExceeded(CEILING)))
    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(Engine.run, host)
    assert str(caught.value) == CEILING
    assert host._eval_inflight == set()          # and it did not leave the group holding children


def test_an_ordinary_crash_is_not_drained():
    """The falsifier for 'just always drain'. A genuine failure keeps today's teardown: the eval is
    cancelled at its next checkpoint and writes no terminal. Draining unconditionally would make
    every crash wait out a multi-hour training before the operator sees the traceback."""
    host = _RunHost(_wrapped(ValueError("solver blew up")))
    with pytest.raises(ValueError):
        anyio.run(Engine.run, host)
    assert host.eval.score_on_disk                # the shielded thread still finished …
    assert host.eval.terminals == []              # … and the cancel still preempted the append


def test_the_drain_is_a_no_op_when_nothing_is_in_flight():
    """It must not invent a barrier. A ceiling with no live evaluation returns immediately."""
    host = _RunHost(BudgetExceeded(CEILING))
    host._eval_inflight = set()

    async def _drive():
        with anyio.move_on_after(2.0) as scope:
            await host._drain_inflight_evaluation(BudgetExceeded(CEILING))
        return scope.cancelled_caught

    assert anyio.run(_drive) is False


def test_the_hook_recognises_the_ceiling_through_the_wrapping_it_arrives_in():
    """The engine-side predicate is the SAME one the CLI records the disposition with. A private
    copy that failed to look through a task group would silently reinstate the defect."""
    leaf = BudgetExceeded(CEILING)
    assert budget_stop_leaf(_wrapped(_wrapped(leaf))) is leaf
    assert budget_stop_leaf(_wrapped(ValueError("x"))) is None


# ------------------------------------------------- _dispatch_evals (the speculation-off path)

class _DispatchHost:
    """A host for the REAL `_dispatch_evals`. Speculation off is a supported configuration, and
    there the evaluation is awaited INSIDE the same group the overlapped research runs in — so the
    research task's raise cancels it directly, one frame lower than the Card path."""

    _dispatch_evals = Engine._dispatch_evals

    def __init__(self, *, raise_ceiling: bool = True, width: int = 1, queued: int = 2):
        self._eval_parallel = width
        self._concurrent_research_repeat = False
        self._raise_ceiling = raise_ceiling
        self.store = types.SimpleNamespace(read_all=lambda: [])
        self.evals = [_PaidEval() for _ in range(queued)]
        self.started: list[int] = []

    def _spawn_research(self, tg, state) -> bool:
        # Stands in for the real overlapped research: one background task in the eval window that
        # crosses the ceiling on its provider call. `tg` is whatever `_dispatch_evals` handed us.
        # The timer is the campaign's own ordering — the ceiling fires WHILE node 0 sits in its
        # shielded worker thread, and the eval finishes (and writes its score) afterwards.
        async def _research() -> None:
            await anyio.to_thread.run_sync(self.evals[0].entered.wait, abandon_on_cancel=True)
            if self._raise_ceiling:
                threading.Timer(0.05, self.evals[0].release.set).start()
                raise BudgetExceeded(CEILING)

        tg.start_soon(_research)
        return True

    def _skip_if_aborted(self, _a, _state) -> bool:
        return False

    async def _evaluate(self, node_id, _limiter, _max_es) -> None:
        self.started.append(node_id)
        await self.evals[node_id].run(node_id)


def _dispatch(host, monkeypatch):
    """Drive the real `_dispatch_evals` with no task group of our own, so whatever it raises
    reaches `pytest.raises` unwrapped — the same object the accountant raised."""
    monkeypatch.setattr(
        "looplab.engine.orchestrator.fold",
        lambda _events: types.SimpleNamespace(total_eval_seconds=0.0, nodes={},
                                              aborted_nodes=set()))
    queue = [{"node_id": i} for i in range(len(host.evals))]

    async def _drive():
        await host._dispatch_evals(queue, object(), None)

    return _drive


def test_dispatch_evals_records_the_running_eval_before_the_ceiling_stops_the_run(monkeypatch):
    host = _DispatchHost()
    with pytest.raises(BaseException) as caught:               # noqa: PT011 - both halves asserted
        anyio.run(_dispatch(host, monkeypatch))
    assert host.evals[0].terminals == ["node_evaluated:0"]     # the paid-for score survived …
    # … and the hard stop is unchanged: the accountant's own exception, NOT a task group wrapping
    # it, so `cli/run_cmds.py` still records `run_finished {"reason": "budget_exhausted"}`.
    assert isinstance(caught.value, BudgetExceeded)
    assert str(caught.value) == CEILING


def test_dispatch_evals_starts_no_further_evaluation_after_the_ceiling(monkeypatch):
    """The ceiling belongs before STARTING an evaluation. A deferred stop that kept admitting work
    would be a strictly worse bug than the one being fixed."""
    host = _DispatchHost()
    with pytest.raises(BudgetExceeded):
        anyio.run(_dispatch(host, monkeypatch))
    assert host.started == [0]
    assert host.evals[1].terminals == []


def test_dispatch_evals_without_a_ceiling_is_unchanged(monkeypatch):
    """The all-clear path: no deferred stop, every queued eval runs, nothing raises."""
    host = _DispatchHost(raise_ceiling=False)
    for ev in host.evals:
        ev.release.set()
    anyio.run(_dispatch(host, monkeypatch))
    assert host.started == [0, 1]
    assert [ev.terminals for ev in host.evals] == [["node_evaluated:0"], ["node_evaluated:1"]]


# ------------------------------------- the serial RESOURCE WAIT (docs/57, the window the host above
# could not see: it has no `_wait_reserve_node_resources`, so the `hasattr` skips the whole wait)

class _WaitingDispatchHost(_DispatchHost):
    """A serial host whose queue head must WAIT for the GPU pool — the cross-run lease that can
    hold a node for hours — and whose overlapped research crosses the ceiling DURING that wait.

    Two dials, both in wait ticks: `free_after` says on which tick the pool grants a reservation,
    `ceiling_at` says after which tick the research task raises. Every tick yields once
    (`anyio.sleep(0)`), which is the real shape too — the sink is appended by a task on the same
    event loop, so a captured stop can only ever become visible across an `await`.
    """

    def __init__(self, *, free_after: int, ceiling_at: int):
        super().__init__(queued=1)
        self.free_after = free_after
        self.ceiling_at = ceiling_at
        self.ticks = 0
        self.released: list = []
        self.registered: list = []

    def _spawn_research(self, tg, state) -> bool:
        async def _research() -> None:
            while self.ticks < self.ceiling_at:
                await anyio.sleep(0)
            raise BudgetExceeded(CEILING)

        tg.start_soon(_research)
        return True

    async def _wait_reserve_node_resources(self, node, *, resource_pin, wait_once):
        assert wait_once, "the serial branch waits ONE bounded tick per fold"
        self.ticks += 1
        await anyio.sleep(0)                       # the bounded condition wait, as a checkpoint
        return {"gpu_ids": [0]} if self.ticks >= self.free_after else None

    def _card_resource_pin_for_node(self, state, node):
        return None

    def _node_resource_reservation_is_current(self, state, node, reservation) -> bool:
        return True

    def _register_eval_resource_reservation(self, node_id, generation, reservation) -> None:
        self.registered.append((node_id, generation))

    def _clear_eval_resource_reservation(self, node_id, generation) -> None:
        pass

    def _release_gpus(self, gpu_ids) -> None:
        self.released.append(list(gpu_ids or []))

    async def _evaluate(self, node_id, _limiter, _max_es) -> None:
        self.started.append(node_id)             # started AFTER the ceiling = the defect


def _dispatch_with_pending_node(host, monkeypatch):
    """Like `_dispatch`, with a fold whose node 0 is a live, pending, generation-0 node — the
    lifecycle gates `_eval_admission_current` re-checks inside the wait all hold, so the ONLY thing
    that can refuse the admission is the ceiling."""
    from looplab.core.models import NodeStatus

    node = types.SimpleNamespace(id=0, attempt=0, status=NodeStatus.pending, tombstoned=False)
    monkeypatch.setattr(
        "looplab.engine.orchestrator.fold",
        lambda _events: types.SimpleNamespace(
            total_eval_seconds=0.0, nodes={0: node}, aborted_nodes=set(),
            paused=False, finished=False, stop_requested=None))

    async def _drive():
        await host._dispatch_evals([{"node_id": 0}], object(), None)

    return _drive


def test_a_ceiling_captured_DURING_the_resource_wait_starts_no_evaluation(monkeypatch):
    """The marker's shape: the pool is held elsewhere for three ticks, the research task crosses the
    ceiling after the first, and the pool then frees. Before the fix the wait loop re-folded and
    re-checked every lifecycle gate on every tick and never the ceiling, so node 0 was admitted and
    STARTED on a run that was already over."""
    host = _WaitingDispatchHost(free_after=3, ceiling_at=1)
    with pytest.raises(BudgetExceeded):
        anyio.run(_dispatch_with_pending_node(host, monkeypatch))
    assert host.started == [], "an evaluation was started after the ceiling"
    assert host.registered == [] and host.released == []


def test_a_ceiling_that_lands_with_the_reservation_hands_the_devices_back(monkeypatch):
    """The other instant: the stop is captured across the very `await` that GRANTS the devices.
    The eval must still not start, and the reservation must be released rather than leaked into a
    pool no evaluation will ever return it to."""
    host = _WaitingDispatchHost(free_after=1, ceiling_at=1)
    with pytest.raises(BudgetExceeded):
        anyio.run(_dispatch_with_pending_node(host, monkeypatch))
    assert host.started == []
    assert host.released == [[0]], "the reservation granted under the ceiling was not released"
    assert host.registered == [], "a reservation was registered for an eval that never ran"


def test_the_resource_wait_still_admits_the_eval_when_no_ceiling_fires(monkeypatch):
    """The all-clear path through the SAME wait: the pool frees on tick 3, no stop is ever captured,
    and the eval runs on the reservation it waited for."""
    host = _WaitingDispatchHost(free_after=3, ceiling_at=10 ** 9)
    host._spawn_research = lambda tg, state: True      # no research task, so nothing can raise
    anyio.run(_dispatch_with_pending_node(host, monkeypatch))
    assert host.started == [0]
    assert host.registered == [(0, 0)]
    assert host.released == [[0]], "the eval's own reservation is released once in its `finally`"


def test_the_recheck_is_the_sink_and_nothing_else():
    from looplab.engine.orchestrator import budget_stop_recheck

    assert not budget_stop_recheck([])
    assert budget_stop_recheck([BudgetExceeded(CEILING)])


# ------------------------------------------------------------------- the facade's own contract

def test_the_facade_defers_only_the_ceiling_and_passes_everything_else_through():
    """`_DeferredBudgetStop` intercepts `start_soon` and NOTHING else — `cancel_scope` above all,
    which `_dispatch_evals`'s `finally` uses to stop the repeating research loop."""
    sink: list[BaseException] = []
    seen: list[str] = []

    async def _drive():
        async with anyio.create_task_group() as tg:
            facade = _DeferredBudgetStop(tg, sink)
            assert facade.cancel_scope is tg.cancel_scope

            async def _ceiling():
                raise BudgetExceeded(CEILING)

            async def _ordinary():
                seen.append("ran")

            facade.start_soon(_ceiling)
            facade.start_soon(_ordinary)

    anyio.run(_drive)
    assert [str(e) for e in sink] == [CEILING]
    assert seen == ["ran"], "a deferred ceiling must not cancel its siblings"


def test_the_facade_never_swallows_an_ordinary_failure():
    """Only the budget stop is deferred. Anything else still tears the group down at once."""
    sink: list[BaseException] = []

    async def _drive():
        async with anyio.create_task_group() as tg:
            async def _boom():
                raise ValueError("provider exploded")
            _DeferredBudgetStop(tg, sink).start_soon(_boom)

    with pytest.raises(BaseExceptionGroup):
        anyio.run(_drive)
    assert sink == []


def test_the_parallel_dispatcher_stops_admitting_at_the_refill_point(monkeypatch):
    """The continuous-dispatch branch has TWO admission points, and the second is where it spends
    nearly all of its time: `await slots.acquire()`, the refill wait. A gate only at the top of the
    producer loop would admit one more evaluation for every slot a finishing sibling frees, which is
    the worst place to be lenient — the ceiling has already been recorded by then."""
    host = _DispatchHost(width=2, queued=3)
    for ev in host.evals[1:]:
        ev.release.set()                    # only node 0 is held open, to pin the ceiling's timing
    with pytest.raises(BaseException) as caught:    # noqa: PT011 - both halves asserted below
        anyio.run(_dispatch(host, monkeypatch))
    assert isinstance(caught.value, BudgetExceeded)
    assert 2 not in host.started, "a freed slot was refilled after the ceiling fired"
    assert host.evals[0].terminals == ["node_evaluated:0"]
