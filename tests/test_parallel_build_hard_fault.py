"""A build that RAISES inside the parallel fan-out gates the run; the spend ceiling ENDS it.

Review 2026-09-22, ENG1-01, driven end to end rather than asserted about the source.

`Engine._create_node_guarded` is the parallel build path's containment: an exception out of one
pooled build becomes that reservation's `build_crash` terminal instead of tearing down its
siblings, and it then asks the MAIN task for the run-global circuit-breaker pause. Two defects sat
in that one handler:

* THE PAUSE NAMED A NODE THAT DOES NOT EXIST. The raise happens before `node_created`, so the id
  is a bare reservation — and `replay.py::_on_pause` reads a pause that names a node as the SCOPED
  developer-crash breaker and drops it unless that node is folded, failed, with
  `error_reason == "developer_crash"`. Measured on the reviewer's reproduction (a 401 surfacing as
  an exception, `llm_parallel=2`, `max_nodes=6`): three `pause` rows appended, `fold().paused`
  False, six reservations spent on `build_crash` and the run "finished" with no eligible
  candidate — the breaker the code documented never engaged once.
* THE SPEND CEILING WAS ONE NODE'S CRASH. `BudgetExceeded` is an `Exception`, so the same blind
  handler recorded it as `build_crash` and the run went on proposing — paid calls — against an
  accountant already over its limit. The serial path ends the run on it; the fan-out now does the
  same, one join later, so the sibling builds already paid for still land.
"""
from __future__ import annotations

import threading

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.cli.run_cmds import _run_engine_guarded
from looplab.core.llm import BudgetExceeded
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_BUILDING, EV_NODE_CREATED, EV_NODE_FAILED, EV_PAUSE
from tests.factories import TOY_TASK, make_engine


class _RaisingDeveloper:
    """A Developer whose provider RAISES — a 401 surfacing as an exception, not the in-band
    `(developer error: …)` sentinel the scoped breaker is built for."""

    is_code_generating = False

    def implement(self, idea):
        raise RuntimeError("HTTP 401 from provider")

    def implement_from(self, idea, parent):
        raise RuntimeError("HTTP 401 from provider")


class _CeilingOnFirstBuild:
    """Every pooled Developer shares ONE counter: the first build to reach the provider hits the
    spend ceiling mid-build; every other build is the toy's own, so a sibling can land."""

    def __init__(self, inner, shared):
        self._inner = inner
        self._shared = shared

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _maybe_stop(self):
        with self._shared["lock"]:
            self._shared["calls"] += 1
            first = self._shared["calls"] == 1
        if first:
            raise BudgetExceeded("LLM budget exceeded: spent 1.0100 >= budget 1.0000")

    # `implement` only: the toy Developer has no `implement_from`, and `__getattr__` must keep
    # answering that honestly or the engine would route an improve through a method that is not
    # there.
    def implement(self, idea, *args, **kwargs):
        self._maybe_stop()
        return self._inner.implement(idea, *args, **kwargs)


def _fan_out_engine(run_dir, developer, *, steady: bool = False, n_seeds: int = 2,
                    max_nodes: int = 6):
    """A 2-wide fan-out over the toy task whose every pooled pair carries `developer()`."""
    task = ToyTask.load(TOY_TASK)

    def factory():
        researcher, _developer = task.build_roles()
        return researcher, developer()

    researcher, _ = task.build_roles()
    return make_engine(run_dir, task=task, researcher=researcher, developer=developer(),
                       n_seeds=n_seeds, max_nodes=max_nodes, llm_parallel=2,
                       role_factory=factory, max_seconds=60, steady_state_build=steady)


@pytest.mark.parametrize("steady", [False, True], ids=["barrier", "steady_lane"])
def test_a_raising_build_in_the_fan_out_pauses_the_run_by_its_own_fold(tmp_path, steady):
    """THE PROPERTY IS THE FOLD'S, not the in-memory flag: the run must read `paused` back from its
    own log, stop after the one chunk that crashed, and stay resumable (not finished)."""
    eng = _fan_out_engine(tmp_path / "run", _RaisingDeveloper, steady=steady)
    assert eng._llm_parallel == 2
    state = anyio.run(eng.run)
    events = eng.store.read_all()
    assert fold(events).paused is True, "the circuit-breaker pause did not engage in the fold"
    assert state.paused is True and not state.finished, (
        "a dead provider must FREEZE the run (resumable), not finish it with no candidate")
    pauses = [e.data for e in events if e.type == EV_PAUSE]
    assert len(pauses) == 1, f"one gate per dead provider, got {pauses}"
    assert "node_id" not in pauses[0], (
        "a pause naming a bare reservation is dropped by `replay._on_pause` — it must be node-less")
    assert "resume once it" in pauses[0]["reason"]
    building = [e for e in events if e.type == EV_NODE_BUILDING]
    failed = [e.data for e in events if e.type == EV_NODE_FAILED]
    assert len(building) <= 2, (
        f"{len(building)} reservations spent: the run kept building past the first crashing chunk")
    assert failed and {f["reason"] for f in failed} == {"build_crash"}
    assert not [e for e in events if e.type == EV_NODE_CREATED]


@pytest.mark.parametrize("steady", [False, True], ids=["barrier", "steady_lane"])
def test_the_spend_ceiling_inside_a_fan_out_build_ends_the_run(tmp_path, steady):
    """`BudgetExceeded` out of a pooled build is the RUN's stop: it reaches `Engine.run` (so the CLI
    records `budget_exhausted`), it is never one node's `build_crash`, and it trips no pause."""
    shared = {"calls": 0, "lock": threading.Lock()}
    task = ToyTask.load(TOY_TASK)

    def developer():
        return _CeilingOnFirstBuild(task.build_roles()[1], shared)

    eng = _fan_out_engine(tmp_path / "run", developer, steady=steady, n_seeds=4, max_nodes=8)
    with pytest.raises(BudgetExceeded):
        anyio.run(eng.run)
    events = eng.store.read_all()
    failed = [e.data for e in events if e.type == EV_NODE_FAILED]
    assert not [f for f in failed if f["reason"] == "build_crash"], (
        f"the spend ceiling was recorded as one node's crash: {failed}")
    assert not [e for e in events if e.type == EV_PAUSE], "a budget stop is not a provider fault"
    # A held stop starts NO new work: at most the two lanes already running when it was raised
    # reached the provider (the steady lane's admission tests the sink before its next proposal).
    assert shared["calls"] <= 2, f"{shared['calls']} builds started past the spend ceiling"
    # The build that hit the ceiling keeps its reservation OPEN — exactly as the serial path
    # leaves it — and the next entry closes it through `_recover_interrupted_builds`.
    assert fold(events).buildings, "the interrupted build's reservation should still be open"


class _AlwaysAtTheCeiling:
    """Every build reaches the provider after the ceiling: each one raises the stop."""

    is_code_generating = False

    def implement(self, idea):
        raise BudgetExceeded("LLM budget exceeded: spent 1.0100 >= budget 1.0000")


@pytest.mark.parametrize("steady", [False, True], ids=["barrier", "steady_lane"])
def test_every_lane_at_the_ceiling_still_ends_the_run_with_ONE_bare_stop(tmp_path, steady):
    """Two builds of one fan-out raising the stop together is the ORDINARY shape — the accountant
    is over for every caller at once. Held and re-raised, the run ends with the accountant's own
    exception; let through the task group, it escapes as a two-member exception group, which
    `Engine.run` deliberately does not collapse (only a LONE member is the run's own error)."""
    eng = _fan_out_engine(tmp_path / "run", _AlwaysAtTheCeiling, steady=steady)
    with pytest.raises(BudgetExceeded) as caught:
        anyio.run(eng.run)
    assert not isinstance(caught.value, BaseExceptionGroup)
    events = eng.store.read_all()
    assert not [e for e in events if e.type in (EV_NODE_FAILED, EV_PAUSE, EV_NODE_CREATED)]


class _FaultBesideCeiling:
    """Two builds meet at a thread barrier — so BOTH are running before either raises — and then
    the first to have entered raises a hard provider fault while the second hits the ceiling."""

    is_code_generating = False

    def __init__(self, shared):
        self._shared = shared

    def implement(self, idea):
        with self._shared["lock"]:
            index = self._shared["entered"]
            self._shared["entered"] += 1
        self._shared["barrier"].wait()
        if index == 0:
            raise RuntimeError("HTTP 401 from provider")
        raise BudgetExceeded("LLM budget exceeded: spent 1.0100 >= budget 1.0000")


@pytest.mark.parametrize("steady", [False, True], ids=["barrier", "steady_lane"])
def test_a_sibling_fault_is_recorded_before_the_held_stop_ends_the_run(tmp_path, steady):
    """The HELD stop is re-raised after the join, and only after the pause a sibling's hard fault
    requested has been appended: that fault really happened in this fan-out, and a stop let
    through the task group instead skips the drain and silently loses it."""
    shared = {"entered": 0, "lock": threading.Lock(), "barrier": threading.Barrier(2, timeout=30)}
    eng = _fan_out_engine(tmp_path / "run", lambda: _FaultBesideCeiling(shared), steady=steady)
    with pytest.raises(BudgetExceeded):
        anyio.run(eng.run)
    events = eng.store.read_all()
    pauses = [e.data for e in events if e.type == EV_PAUSE]
    assert len(pauses) == 1 and "node_id" not in pauses[0], pauses
    failed = [e.data for e in events if e.type == EV_NODE_FAILED]
    assert [f["reason"] for f in failed] == ["build_crash"], failed


def test_a_proposal_in_flight_when_a_lane_hits_the_ceiling_reserves_nothing(tmp_path):
    """The steady lane proposes WHILE lanes build. A proposal that returns after a running lane
    raised the stop is already paid for, but a node reserved for it is new work the held stop
    forbids (its build's first paid call would only raise again)."""
    shared = {"calls": 0, "lock": threading.Lock()}
    raised = threading.Event()
    task = ToyTask.load(TOY_TASK)

    class _SignalledCeiling(_CeilingOnFirstBuild):
        def _maybe_stop(self):
            try:
                super()._maybe_stop()
            except BudgetExceeded:
                raised.set()
                raise

    eng = _fan_out_engine(tmp_path / "run",
                          lambda: _SignalledCeiling(task.build_roles()[1], shared),
                          steady=True, n_seeds=4, max_nodes=8)
    real = eng._await_batch_proposal
    proposals = {"n": 0}

    async def _slow_second_proposal(state, width):
        proposals["n"] += 1
        if proposals["n"] == 2:
            with anyio.fail_after(30):
                while not raised.is_set():
                    await anyio.sleep(0.01)
            await anyio.sleep(0.2)          # let the lane's task hand its stop to the sink
        return await real(state, width)

    eng._await_batch_proposal = _slow_second_proposal
    with pytest.raises(BudgetExceeded):
        anyio.run(eng.run)
    assert proposals["n"] == 2, "the second proposal is the one this test holds in flight"
    building = [e for e in eng.store.read_all() if e.type == EV_NODE_BUILDING]
    assert len(building) == 1, f"a node was reserved after the stop was held: {len(building)}"


def test_the_barrier_lands_the_sibling_it_already_paid_for_before_the_stop(tmp_path):
    """The deferral is the point of stashing rather than letting the raise cancel the group: the
    chunk's OTHER build was already paid for, and its node must be in the log the stop ends."""
    shared = {"calls": 0, "lock": threading.Lock()}
    task = ToyTask.load(TOY_TASK)

    def developer():
        return _CeilingOnFirstBuild(task.build_roles()[1], shared)

    eng = _fan_out_engine(tmp_path / "run", developer, steady=False, n_seeds=2, max_nodes=4)
    with pytest.raises(BudgetExceeded):
        anyio.run(eng.run)
    events = eng.store.read_all()
    created = [e for e in events if e.type == EV_NODE_CREATED]
    assert len(created) == 1, "the sibling build in the same chunk must land its node"
    assert shared["calls"] == 2, "no build may START after the chunk that hit the ceiling"


def test_the_cli_records_a_fan_out_ceiling_as_budget_exhausted(tmp_path):
    shared = {"calls": 0, "lock": threading.Lock()}
    task = ToyTask.load(TOY_TASK)

    def developer():
        return _CeilingOnFirstBuild(task.build_roles()[1], shared)

    eng = _fan_out_engine(tmp_path / "run", developer, n_seeds=2, max_nodes=4)
    with pytest.raises(BudgetExceeded):
        _run_engine_guarded(eng)
    finished = [e.data for e in eng.store.read_all() if e.type == "run_finished"]
    assert len(finished) == 1 and finished[0]["reason"] == "budget_exhausted", finished
