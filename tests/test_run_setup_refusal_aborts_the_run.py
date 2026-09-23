"""A failed run-level `run_setup` ABORTS the run, and it runs ONCE (review 2026-09-22, ENG2-08).

`docs/guide/tasks.md` says of `eval.run_setup`: "A failure aborts the run." It did not. The setup runs
inside the eval worker (`eval_dispatch.py::_ensure_run_setup`, first thing in `_run_eval`), its
failure is an `EnvironmentRefusal`, and `evaluate.py::_evaluate`'s containment — written for ONE
node's environment fault (an ENOSPC, a vanished inode) — filed it as that node's
`node_failed{reason: "engine_error"}` and PAUSED the run. Worse, the in-process guard was set only on
success, so every evaluation already waiting on `_run_setup_lock` re-ran the failing install
serially, and each one earned its own `engine_error` terminal: reproduced on a real `Engine.run` at
width 2 — two `run_setup` executions, two `engine_error` nodes, a paused run.

What holds now, every property DRIVEN through the real `_evaluate` / `Engine.run` / CLI guard:

* the refusal is LATCHED: the setup runs once per process, and a waiter re-raises the refusal
  instead of re-running the install;
* it is a deliberate stop (`evaluate.py::_EVAL_DELIBERATE_STOPS`), so no node is terminalized and
  nothing is paused — the nodes stay pending for the resume the refusal's own sentence asks for;
* an eval CHILD defers it to its owner exactly like the spend ceiling (`core/errors.py::
  deferrable_run_stop`), so the run ends on ONE refusal, never on a group of N identical ones;
* the CLI records the abort the docs describe: `run_finished {reason: "error"}` carrying the
  refusal's own sentence.
"""
from __future__ import annotations

import threading
import time

import anyio
import pytest

from looplab.core.errors import (BudgetExceeded, EnvironmentRefusal, RunSetupRefusal,
                                 deferrable_budget_stop, deferrable_run_stop, exception_leaves)
from looplab.events.replay import fold
from looplab.runtime import sandbox
from tests.factories import make_engine

SETUP = ["pip", "install", "nope-not-a-distribution"]


def _failing_setup(monkeypatch, *, delay: float = 0.0) -> list:
    """Every `run_setup` launch recorded and failed. `_do_run_setup` imports `_run_argv` from the
    sandbox module at call time, so that is the seam (`tests/test_run_stop_word.py` uses it too)."""
    calls: list = []
    lock = threading.Lock()

    def _run_argv(cmd, cwd, timeout, log_path=None, **_kw):
        with lock:
            calls.append(list(cmd))
        if delay:
            time.sleep(delay)          # long enough for a sibling eval to queue on the setup lock
        return (1, "", "ERROR: No matching distribution found for nope-not-a-distribution\n",
                False)

    monkeypatch.setattr(sandbox, "_run_argv", _run_argv)
    return calls


def _engine(run_dir, **overrides):
    engine = make_engine(run_dir, max_nodes=4, **overrides)
    engine.trust_mode = "trusted_local"
    engine._eval_spec = {"command": ["python", "-c", "print(1)"],
                         "metric": {"kind": "stdout_json", "key": "metric"},
                         "run_setup": list(SETUP), "run_setup_timeout": 5.0}
    return engine


def _seed_two_nodes(engine) -> None:
    for nid in (0, 1):
        engine.store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "base"},
            "code": "print(1)"})


def _types(engine) -> list[str]:
    return [e.type for e in engine.store.read_all()]


def _assert_nothing_terminalized_or_paused(engine) -> None:
    state = fold(engine.store.read_all())
    failed = {n.id: n.error_reason for n in state.nodes.values() if n.status.value == "failed"}
    assert failed == {}, (
        f"a refused run-level setup was filed as node failures {failed}: it is a fact about the "
        "RUN's environment, and the nodes it never let start are owed an evaluation on resume")
    assert "pause" not in _types(engine), "the run was PAUSED; the documented disposition is abort"
    assert not state.paused
    assert state.pending_nodes(), "the nodes the setup never let start must stay pending"


# --------------------------------------------------------------------------- the latch

def test_waiting_evaluations_do_not_rerun_a_failed_run_setup(tmp_path, monkeypatch):
    """THE REVIEWER'S SHAPE (review/ENG2/repro_run_setup.py): two real evaluations at once. The
    second queues on `_run_setup_lock` while the first installs; when the install fails it must
    stop on the SAME refusal instead of launching pip again."""
    calls = _failing_setup(monkeypatch, delay=0.3)
    engine = _engine(tmp_path / "run")
    _seed_two_nodes(engine)

    async def _both():
        limiter = anyio.CapacityLimiter(2)
        async with anyio.create_task_group() as tg:
            tg.start_soon(engine._evaluate, 0, limiter, None)
            tg.start_soon(engine._evaluate, 1, limiter, None)

    with pytest.raises(BaseExceptionGroup) as caught:
        anyio.run(_both)

    leaves = list(exception_leaves(caught.value))
    assert leaves and all(isinstance(leaf, RunSetupRefusal) for leaf in leaves), leaves
    assert len(calls) == 1, f"the failing setup ran {len(calls)} times: {calls}"
    assert _types(engine).count("run_setup_started") == 1
    _assert_nothing_terminalized_or_paused(engine)


def test_an_evaluation_after_the_failure_refuses_without_running_the_setup_again(
        tmp_path, monkeypatch):
    """The other half of the latch: an evaluation that REACHES the setup after it failed (the fast
    path, no lock contention) re-raises the refusal — pip is not launched a second time."""
    calls = _failing_setup(monkeypatch)
    engine = _engine(tmp_path / "run")
    _seed_two_nodes(engine)

    with pytest.raises(BaseException) as first:             # noqa: PT011 — leaves asserted below
        anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    with pytest.raises(BaseException) as second:            # noqa: PT011 — leaves asserted below
        anyio.run(engine._evaluate, 1, anyio.CapacityLimiter(1), None)

    for caught in (first, second):
        leaves = list(exception_leaves(caught.value))
        assert leaves and all(isinstance(leaf, RunSetupRefusal) for leaf in leaves), leaves
    assert len(calls) == 1, f"the failing setup ran {len(calls)} times: {calls}"
    _assert_nothing_terminalized_or_paused(engine)


# --------------------------------------------------------------------------- the run ends

@pytest.mark.parametrize("width", [1, 2])
def test_a_failed_run_setup_ends_the_run_with_the_refusal(tmp_path, monkeypatch, width):
    """A real `Engine.run` through the real dispatcher, serial and parallel. It must END, raising the
    refusal ITSELF — the type the CLI boundary prints as one line at exit 2 — never a group of N
    copies and never a paused run that returns normally."""
    calls = _failing_setup(monkeypatch, delay=0.2)
    engine = _engine(tmp_path / f"run-{width}", eval_parallel=width)
    engine._eval_parallel = width

    async def _bounded():
        with anyio.fail_after(60):
            return await engine.run()

    with pytest.raises(RunSetupRefusal) as caught:
        anyio.run(_bounded)

    assert isinstance(caught.value, EnvironmentRefusal) and isinstance(caught.value, RuntimeError)
    assert "run_setup failed" in str(caught.value)
    if width == 2:
        assert _types(engine).count("node_eval_started") >= 2, (
            "the precondition (a second evaluation queued behind the setup) never happened")
    assert len(calls) == 1, f"the failing setup ran {len(calls)} times: {calls}"
    _assert_nothing_terminalized_or_paused(engine)
    assert engine._eval_budget_stop is None, "a raised stop must not outlive the run that raised it"


def test_a_failed_run_setup_ends_a_card_mode_run_with_the_refusal(tmp_path, monkeypatch):
    """The same property on the path speculation ships on: Card mode at width 2, where every
    evaluation is a child of the RUN-scoped eval group (`speculation.py::_card_eval_one`). A child
    that raised the refusal into that group would cancel its siblings and the host body and end the
    run on a group of copies; parked, the owner raises exactly one."""
    from tests.test_card_refill_unequal_durations import _occupancy_engine
    from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
        _admit_unit_speculation_receipt,
    )

    calls = _failing_setup(monkeypatch, delay=0.2)
    engine = _occupancy_engine(tmp_path / "card", max_nodes=4)
    engine.trust_mode = "trusted_local"
    engine._eval_spec = {"command": ["python", "-c", "print(1)"],
                         "metric": {"kind": "stdout_json", "key": "metric"},
                         "run_setup": list(SETUP), "run_setup_timeout": 5.0}

    async def _bounded():
        with anyio.fail_after(60):
            return await engine.run()

    with pytest.raises(RunSetupRefusal):
        anyio.run(_bounded)

    assert _types(engine).count("node_eval_started") >= 2, (
        "the precondition (two adopted evaluations at once) never happened")
    assert len(calls) == 1, f"the failing setup ran {len(calls)} times: {calls}"
    _assert_nothing_terminalized_or_paused(engine)
    assert engine._eval_budget_stop is None and engine._eval_inflight == set()


def test_the_cli_records_the_abort_the_guide_describes(tmp_path, monkeypatch):
    """`docs/guide/tasks.md`: "A failure aborts the run." The CLI's guarded drive is where a run's
    terminal is written, so the promise is checked there: `run_finished {reason: "error"}` carrying
    the refusal's sentence, and the refusal re-raised for the one-line boundary."""
    from looplab.cli.run_cmds import _run_engine_guarded

    _failing_setup(monkeypatch)
    engine = _engine(tmp_path / "run")

    with pytest.raises(RunSetupRefusal):
        _run_engine_guarded(engine)

    state = fold(engine.store.read_all())
    assert state.finished and state.stop_reason == "error", (state.finished, state.stop_reason)
    assert "run_setup failed" in (state.stop_detail or ""), state.stop_detail
    assert not state.paused


# --------------------------------------------------------------------------- the deferral rule

def test_only_a_pure_run_ending_stop_is_deferred():
    """`core/errors.py::deferrable_run_stop` — the truth table. The spend ceiling keeps exactly the
    answer `deferrable_budget_stop` gives it; a refused run setup is deferred on the same terms (every
    leaf is one); anything mixed, any other environment refusal, and a cancellation are not."""
    import asyncio

    ceiling = BudgetExceeded("LLM spend ceiling reached")
    refusal = RunSetupRefusal("run_setup failed (exit=1, timed_out=False)")
    assert deferrable_run_stop(ceiling) is ceiling
    assert deferrable_run_stop(refusal) is refusal
    assert deferrable_run_stop(BaseExceptionGroup("g", [refusal])) is refusal
    assert deferrable_run_stop(
        BaseExceptionGroup("g", [BaseExceptionGroup("h", [refusal]), refusal])) is refusal
    assert deferrable_run_stop(BaseExceptionGroup("g", [refusal, ValueError("disk")])) is None
    assert deferrable_run_stop(BaseExceptionGroup("g", [refusal, ceiling])) is None
    assert deferrable_run_stop(EnvironmentRefusal("no docker")) is None, (
        "only the RUN's own setup is a run-ending stop; another environment refusal keeps its path")
    assert deferrable_run_stop(ValueError("x")) is None
    cancelled = asyncio.CancelledError()
    cancelled.__context__ = refusal
    assert deferrable_run_stop(cancelled) is None
    # the ceiling's own rule is untouched by the widening
    assert deferrable_budget_stop(refusal) is None
