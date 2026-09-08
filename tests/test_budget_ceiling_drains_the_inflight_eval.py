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
    # The run-loop exit receipt `Engine.run`'s outer `finally` writes, bound REAL rather than
    # stubbed: it is owed only once `_enter_run` has returned (`_run_loop_exit_owed`), and this host
    # never enters a run, so the shipped early-return is the branch taken. A no-op stub would have
    # asserted the same thing without proving the latch is what decides it.
    _record_run_loop_exit = Engine._record_run_loop_exit
    # The phase-event SINK `run` installs for `core/phase_events.py` (doc 52 row 16). Bound
    # REAL for the same reason as the receipt above: `run`'s frame installs and removes it,
    # and a host missing it turns that structural step into an AttributeError instead of
    # exercising it. It writes nothing here — this host has no store and never enters a run.
    _append_phase_event = Engine._append_phase_event

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
    # THE ENGINE'S OWN TAIL GATE, borrowed the same way and for the same reason: this is a stub OF
    # the Engine, not of the log. `_dispatch_evals` began calling `_fold_if_tail_moved` when the
    # resource wait's re-fold was gated on the tail seq (doc 25 ES-12), and the three tests that
    # drive the WAITING host through the real method died with `AttributeError` inside the task
    # group — a stub that borrows the method under test has to follow it when that method grows a
    # new call. `tests/test_gpu_resources.py` took the same step at the same commit; this file was
    # the one it did not reach. The base stub's `store.read_all` answers `[]`, so the gate folds an
    # empty log and the wait's own ticks are what the tests are about, unchanged.
    _fold_if_tail_moved = Engine._fold_if_tail_moved

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


# ----------------------------------------- the ceiling raised INSIDE an evaluation (2026-09-08)
#
# THE SEAM THE TWO SECTIONS ABOVE DO NOT COVER, and the one the marker on `_DeferredBudgetStop`
# stood for until this landed. Both of them raise the ceiling from the overlapped RESEARCH, whose
# exception carries the `BudgetExceeded` leaf all the way to `Engine.run`. A ceiling raised from
# inside an EVALUATION does not: the repair path re-raises it, and stage checks, triage and the
# repair critic are all paid calls, so under Card mode the raise comes out of an `eval_tg` CHILD —
# the group cancels `_run_with_llm_broker`, the outer handler catches a Cancelled whose
# `budget_stop_leaf` is None, and the drain no-opped while every SIBLING lost its terminal.
#
# The fix is to stop keying the drain on the escaping exception alone. The run's own LEDGERS hold
# the ceiling out of band, and `accountant_over_ceiling` is that question asked of them.

class _OverCeilingHost(_RunHost):
    """`_RunHost` whose reserve-commit ledger says the run is already past its ceiling.

    The escaping failure is deliberately NOT a `BudgetExceeded`: this is the cancelled-sibling
    shape, where the only carrier of the fact is the ledger.
    """

    def __init__(self, failure: BaseException, *, committed: float = 1.5, limit: float = 1.0):
        super().__init__(failure)
        self._llm_budget = types.SimpleNamespace(
            cost_limit=limit, committed_cost=committed, token_limit=None, committed_tokens=0)


# The two tests below are a PAIR and the pairing is the argument: byte-identical hosts, the SAME
# escaping exception (whose `budget_stop_leaf` is None either way, exactly like the cancellation a
# ceiling raised in an `eval_tg` child delivers to `Engine.run`), and only the LEDGER differs.

def test_a_sibling_terminal_survives_a_ceiling_the_escaping_exception_cannot_name():
    """THE PROPERTY. The run is over ceiling and an evaluation is burning; what reaches `Engine.run`
    carries no `BudgetExceeded` leaf at all. The sibling must still land its terminal — the compute
    is spent either way."""
    host = _OverCeilingHost(_wrapped(ValueError("cancelled two frames down")))
    with pytest.raises(ValueError):
        anyio.run(Engine.run, host)
    assert host.eval.score_on_disk
    assert host.eval.terminals == ["node_evaluated:0"], (
        "the drain keyed only on the escaping exception's leaf, so a ceiling raised inside an "
        "evaluation still discarded its siblings' terminals")


def test_the_same_run_under_its_ceiling_is_not_drained():
    """THE NEGATIVE CONTROL, and it is the whole reason this is a ledger question and not
    `if evals_inflight: drain`. Same host, same exception, a ledger that is INSIDE its ceiling —
    today's teardown, unchanged: no barrier, no terminal."""
    host = _OverCeilingHost(_wrapped(ValueError("cancelled two frames down")), committed=0.25)
    with pytest.raises(ValueError):
        anyio.run(Engine.run, host)
    assert host.eval.score_on_disk
    assert host.eval.terminals == []


def test_the_ledger_predicate_reads_both_halves_of_the_ceiling():
    """The truth table, stated. `llm_cost_limit`/`llm_token_limit` 0 means OFF (`core/llm_budget.py`),
    so a zero ceiling can never make this true — which is what keeps the drain off every run that
    declared no budget at all."""
    from looplab.core.llm import CostAccountant
    from looplab.engine.orchestrator import accountant_over_ceiling

    def _host(**budget):
        ledger = {"cost_limit": None, "committed_cost": 0.0,
                  "token_limit": None, "committed_tokens": 0}
        ledger.update(budget)
        return types.SimpleNamespace(_llm_budget=types.SimpleNamespace(**ledger))

    assert accountant_over_ceiling(_host(cost_limit=1.0, committed_cost=1.0)) is True   # at ==
    assert accountant_over_ceiling(_host(cost_limit=1.0, committed_cost=0.999)) is False
    assert accountant_over_ceiling(_host(token_limit=100, committed_tokens=100)) is True
    assert accountant_over_ceiling(_host(token_limit=100, committed_tokens=99)) is False
    assert accountant_over_ceiling(_host(cost_limit=0.0, committed_cost=5.0)) is False  # 0 == off
    assert accountant_over_ceiling(types.SimpleNamespace()) is False

    # …and the OTHER ledger: the `CostAccountant` family the role graph carries, which is the one
    # `core/llm.py::CostAccountant.add` actually raises on.
    spent = CostAccountant(limit=1.0)
    role = types.SimpleNamespace(accountant=spent)
    assert accountant_over_ceiling(types.SimpleNamespace(researcher=role)) is False
    spent.spent = 1.0
    assert accountant_over_ceiling(types.SimpleNamespace(researcher=role)) is True


def test_the_predicate_never_replaces_the_runs_own_stop():
    """It runs one frame from the `raise` that ends the run, so an unreadable ledger answers False
    rather than raising an introspection error over the operator's budget receipt."""
    from looplab.engine.orchestrator import accountant_over_ceiling

    class _Hostile:
        @property
        def _llm_budget(self):
            raise RuntimeError("role graph is mid-teardown")

    assert accountant_over_ceiling(_Hostile()) is False


def test_the_facade_forwards_the_task_name_and_refuses_the_start_handshake():
    """The two lesser hardenings the marker named. `name=` reaches anyio (it is what every stall
    report and task dump prints), and `start()` — whose started-value handshake cannot coexist with
    a deferred exception — is REFUSED rather than forwarded uncaptured through `__getattr__`."""
    seen: dict = {}

    class _Tg:
        def start_soon(self, func, *args, name=None):
            seen["name"] = name

        async def start(self, *_a, **_k):        # pragma: no cover - must never be reached
            raise AssertionError("the facade forwarded start() to the real group")

    facade = _DeferredBudgetStop(_Tg(), [])

    async def _noop():                            # pragma: no cover - never awaited by the stub
        return None

    facade.start_soon(_noop, name="research")
    assert seen["name"] == "research"
    with pytest.raises(TypeError, match="start_soon"):
        anyio.run(facade.start, _noop)


# ------------------------------------- the OTHER half: the node that raised the ceiling itself
#
# `_evaluate`'s attempt loop runs RUN_ATTEMPT (the sandbox; the score is on disk when it returns)
# and only THEN the phases that can spend: the inter-stage check, the training-log judge, the
# triage/diagnosis call, the repair critic and the repair. WRITE_TERMINAL comes after all of them.
# On a run at its ceiling the next of those raises, so the node that was measured is the one whose
# record is lost — the same loss the drain above fixes for its SIBLINGS.

class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def set(self, *_a, **_k):
        pass

    def set_many(self, **_k):
        pass


class _CeilingEvalHost:
    """A host for the REAL `_evaluate` driver and the REAL `_land_terminal_before_ceiling`.

    The nine phases are stubs, because what is under test is the DRIVER's unwind path and not any
    phase's body: RUN_ATTEMPT binds the record exactly as the real one does (a result and an `ok`),
    the next phase raises the ceiling, and the question is only whether the terminal is written
    before that raise leaves the worker. `_eval_write_terminal` records into the same fake log the
    guard reads, so the invariant-#2 check below is exercised against a real row and not a flag.
    """

    _evaluate = Engine._evaluate
    _land_terminal_before_ceiling = Engine._land_terminal_before_ceiling

    def __init__(self, *, raised: BaseException, rows: list | None = None, measure: bool = True):
        self._raised = raised
        self._measure = measure
        self.rows = list(rows or [])
        self.terminals: list[int] = []
        self.contained: list[BaseException] = []
        self.tracer = types.SimpleNamespace(span=lambda *a, **k: _NullSpan())
        self.store = types.SimpleNamespace(read_all=lambda: list(self.rows))

    async def _eval_admit(self, a):
        a.generation = 0
        a.node = object()
        a.sp = _NullSpan()
        return "next"

    def _eval_prepare_workdir(self, a):
        pass

    def _eval_seed_ledgers(self, a):
        pass

    async def _eval_run_attempt(self, a):
        if self._measure:
            a.res = types.SimpleNamespace(metric=1.25)   # score.log is on disk; the money is spent
            a.ok = True
        return "next"

    async def _eval_settle_outcome(self, a):
        raise self._raised                              # a PAID post-score call crosses the ceiling

    async def _eval_salvage(self, a):                   # pragma: no cover - unreachable above
        return "next"

    async def _eval_decide_repair(self, a):             # pragma: no cover
        return "next"

    async def _eval_apply_repair(self, a):              # pragma: no cover
        return "next"

    async def _eval_write_terminal(self, a):
        self.terminals.append(a.node_id)
        self.rows.append(types.SimpleNamespace(
            type="node_evaluated", data={"node_id": a.node_id, "generation": a.generation}))

    async def _contain_eval_crash(self, node_id, generation, exc):
        self.contained.append(exc)


def _drive_eval(host):
    async def _run():
        await host._evaluate(0, anyio.CapacityLimiter(1))
    return _run


def test_a_measured_node_lands_its_terminal_before_its_own_ceiling_propagates():
    """THE PROPERTY. The sandbox finished, the number exists, and a paid bookkeeping call then
    crossed the ceiling. The run still stops with the same exception — and the node it already paid
    for is in the log instead of reading `pending` on the next resume."""
    host = _CeilingEvalHost(raised=BudgetExceeded(CEILING))
    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(_drive_eval(host))
    assert str(caught.value) == CEILING          # the stop is unchanged in class and sentence
    assert host.terminals == [0], "the measured node's terminal was lost to its own ceiling"
    assert host.contained == []                  # a ceiling is never filed as a node crash


def test_the_ceiling_wrapped_by_the_nested_watcher_group_is_still_recognised():
    """The measured shape: the eval runs beside its watcher in a nested task group, so a ceiling
    from in there arrives as an `ExceptionGroup` — an `Exception`, which the driver's blanket clause
    catches. `budget_stop_leaf` is asked of the LEAVES for exactly this reason."""
    host = _CeilingEvalHost(raised=_wrapped(BudgetExceeded(CEILING)))
    with pytest.raises(BaseExceptionGroup):
        anyio.run(_drive_eval(host))
    assert host.terminals == [0]
    assert host.contained == []


def test_an_ordinary_crash_writes_no_terminal_through_this_path():
    """THE NEGATIVE CONTROL. Only a spend ceiling takes this route; every other failure keeps the
    containment handler, which is what decides the node's reason and its row."""
    boom = OSError("read-only file system")
    host = _CeilingEvalHost(raised=boom)
    anyio.run(_drive_eval(host))                 # contained, exactly as before
    assert host.terminals == []
    assert host.contained == [boom]


def test_a_ceiling_before_anything_was_measured_writes_nothing():
    """No `res` means RUN_ATTEMPT never returned: there is no paid result to lose, and inventing a
    terminal for a node that never ran would be a lie about what happened."""
    host = _CeilingEvalHost(raised=BudgetExceeded(CEILING), measure=False)
    with pytest.raises(BudgetExceeded):
        anyio.run(_drive_eval(host))
    assert host.terminals == []


def test_a_lifecycle_that_is_already_terminal_is_not_written_twice():
    """INVARIANT #2: exactly one terminal per node. The fold takes the first and would ignore a
    second, but a log carrying two is a lie about what happened, not a harmless duplicate."""
    prior = [types.SimpleNamespace(type="node_failed", data={"node_id": 0, "generation": 0})]
    host = _CeilingEvalHost(raised=BudgetExceeded(CEILING), rows=prior)
    with pytest.raises(BudgetExceeded):
        anyio.run(_drive_eval(host))
    assert host.terminals == []
