"""One node's engine fault closes THAT node — it does not cancel every sibling eval.

`_evaluate` runs as a child of the RUN-SCOPED eval task group, and its three callers are
`try/finally` with no `except`. So any exception the body did not itself handle escaped into the
group: every in-flight sibling was cancelled mid-training with NO terminal of its own, the run
exited on a traceback, and resume found all of them still `pending` and re-spent their GPU hours. On
a two-card box that is one bad node destroying its neighbour's multi-hour training.

The `gpu_unpinnable` handler already established the whole rule for one exception type. The argument
does not depend on WHICH exception it was — it depends on the blast radius, which is identical for
all of them — so the shape is generalised here.

Driven through a real `Engine` over a real event log, not by inspecting the source: a signature check
would pass on a handler that caught the exception and wrote nothing, which is most of what was wrong.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.llm import BudgetExceeded
from looplab.core.models import BENIGN_TERMINAL_REASONS, ENGINE_TERMINAL_REASONS, NodeStatus
from looplab.events.replay import fold
from factories import make_engine


def _node(engine, node_id: int = 0, code: str = "print(1)") -> int:
    """One pending node, appended as the engine appends one."""
    engine.store.append("node_created", {
        "node_id": node_id, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "base"},
        "code": code})
    return node_id


def _state(engine):
    return fold(engine.store.read_all())


def _evaluate(engine, node_id, *, boom):
    """Run one `_evaluate` with `_run_eval` replaced by a raiser, as the eval task group would."""
    def _raise(*_a, **_kw):
        raise boom

    engine._run_eval = _raise                       # the thread-side callee, deep inside the body

    async def _drive():
        await engine._evaluate(node_id, anyio.CapacityLimiter(1), None)

    anyio.run(_drive)


def _leaves(exc: BaseException) -> list:
    """The exceptions an (arbitrarily nested) group actually carries — `_evaluate` may be unwinding
    through a task group, which wraps whatever escaped."""
    inner = getattr(exc, "exceptions", None)
    if isinstance(inner, (list, tuple)) and isinstance(exc, BaseExceptionGroup):
        return [leaf for e in inner for leaf in _leaves(e)]
    return [exc]


def test_an_engine_exception_closes_the_node_instead_of_escaping(tmp_path):
    """THE DEFECT. MUTATION: remove the containment `try` -> this raises OSError out of `_evaluate`,
    which in production is the run-scoped task group and every sibling with it."""
    engine = make_engine(tmp_path)
    node_id = _node(engine)
    _evaluate(engine, node_id, boom=OSError(28, "No space left on device"))

    node = _state(engine).nodes[node_id]
    assert node.status is NodeStatus.failed, "the node must not be left pending"
    assert node.error_reason == "engine_error"


def test_the_exception_is_NAMED_on_the_terminal(tmp_path):
    """A terminal that says only "it failed" sends the operator to a traceback that no longer
    exists — the process that produced it has exited."""
    engine = make_engine(tmp_path)
    node_id = _node(engine)
    _evaluate(engine, node_id, boom=RuntimeError("the run directory went read-only"))

    failed = [e for e in engine.store.read_all() if e.type == "node_failed"]
    assert failed and "RuntimeError" in failed[-1].data["error"]
    assert "read-only" in failed[-1].data["error"]


def test_the_run_is_PAUSED(tmp_path):
    """A node closed this way is evidence about the BOX, not about the idea. Continuing to dispatch
    is how one disk-full becomes N failed nodes and a budget spent on nothing.

    MUTATION: drop the pause -> the next node is dispatched into the same broken filesystem.
    """
    engine = make_engine(tmp_path)
    node_id = _node(engine)
    _evaluate(engine, node_id, boom=OSError(28, "No space left on device"))
    assert _state(engine).paused is True


def test_the_terminal_and_the_pause_are_in_ONE_locked_section(tmp_path):
    """So no reader can ever observe a closed node with the run still dispatching, or a paused run
    with a node still pending. They are adjacent rows of the same append."""
    engine = make_engine(tmp_path)
    node_id = _node(engine)
    _evaluate(engine, node_id, boom=ValueError("boom"))
    types = [e.type for e in engine.store.read_all()]
    assert types[-2:] == ["node_failed", "pause"]


def test_engine_error_is_NOT_repairable(tmp_path):
    """`crash` means the CANDIDATE's process died, and is in `FAILURE_REASONS`, therefore in the
    default `inline_repair_reasons`, therefore repaired. Handing the Developer a disk-full and asking
    it to fix the training script cannot work and spends a paid triage call to discover that.

    MUTATION: reuse `crash` here -> the repair loop picks this up, every time.
    """
    from looplab.core.models import FAILURE_REASONS
    from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
    from looplab.engine.failure_diagnosis import DIAGNOSED_FAILURE_REASONS

    assert "engine_error" in ENGINE_TERMINAL_REASONS
    assert "engine_error" not in FAILURE_REASONS, "would become repairable"
    assert "engine_error" not in DIAGNOSED_FAILURE_REASONS, "would be sent to a paid diagnostician"
    # Absent from the salvage veto list too, and that is deliberate rather than an omission: it is
    # not in FAILURE_REASONS at all, so no salvage rung ever reaches it.
    assert "engine_error" not in NEVER_SALVAGED_REASONS


def test_engine_error_is_not_benign(tmp_path):
    """`BENIGN_TERMINAL_REASONS` is what the owner-attention feed and the failure-spike filter hide.
    An engine fault is the one thing an owner most needs to see."""
    assert "engine_error" not in BENIGN_TERMINAL_REASONS


def test_a_CANCELLATION_is_re_raised_and_never_terminalized(tmp_path):
    """Cancellation is how a reset, an operator abort and a run stop reach this worker. Answering one
    with a `node_failed` invents a failure out of a deliberate intervention, and swallowing it breaks
    structured concurrency.

    MUTATION: catch `BaseException` without the cancellation clause -> an operator stop mid-eval
    writes a bogus `engine_error` terminal and pauses the run the operator was already stopping.
    """
    engine = make_engine(tmp_path)
    node_id = _node(engine)
    before = len(engine.store.read_all())

    async def _drive():
        cancelled = anyio.get_cancelled_exc_class()

        def _cancel(*_a, **_kw):             # raised ON THE LOOP, early in the body
            raise cancelled()

        engine._assert_speculative_selection_confirmed = _cancel
        with anyio.CancelScope() as scope:
            scope.cancel()
            await engine._evaluate(node_id, anyio.CapacityLimiter(1), None)

    anyio.run(_drive)
    after = [e for e in engine.store.read_all()[before:]]
    assert not [e for e in after if e.type == "node_failed"], "a cancellation is not a node failure"
    assert _state(engine).paused is False


def test_a_BUDGET_STOP_is_re_raised_and_never_terminalized(tmp_path):
    """A SPEND LIMIT is a fact about the run, not an engine fault in one node.

    `BudgetExceeded` is an `Exception`, so the blanket clause reaches it — and every
    `except BudgetExceeded: raise` on the paths under `_evaluate` (`_triage_crash`, `_repair`,
    `crash_repair`) exists precisely to hand it up to `Engine.run`, whose
    `except BudgetExceeded: raise  # global hard stop` ends the run. Contained instead, the
    operator's own budget reaches them as `node_failed{reason: "engine_error"}` plus an
    `engine_error` pause — a reason deliberately outside every failure vocabulary, so the node gets
    no diagnosis, no repair and no salvage, and the run reports a box fault for a spend limit.

    MUTATION: drop `BudgetExceeded` from the re-raise tuple -> this test sees a contained terminal
    instead of the exception, which is exactly how it shipped.
    """
    engine = make_engine(tmp_path)
    node_id = _node(engine)
    before = len(engine.store.read_all())

    with pytest.raises(BaseException) as caught:                 # a group, if a task group is open
        _evaluate(engine, node_id, boom=BudgetExceeded("llm budget exhausted"))
    assert _leaves(caught.value) and all(isinstance(e, BudgetExceeded) for e in _leaves(caught.value)), (
        f"the budget stop must reach the caller, not a terminal: {caught.value!r}")

    after = engine.store.read_all()[before:]
    assert not [e for e in after if e.type == "node_failed"], (
        "a budget stop is not a node failure — it must reach Engine.run's global hard stop")
    assert _state(engine).paused is False, (
        "and it must not pause the run as an `engine_error`: the run is ENDING, not stalling")


def test_a_node_that_already_wrote_its_own_terminal_does_not_get_a_second(tmp_path):
    """A body that terminalized and then raised on the way out (a tracer teardown, a span export)
    must not have its reason contradicted by a second row. The fold is idempotent on duplicate
    terminals, so a second one is not corruption — it is a durable row asserting the wrong cause."""
    engine = make_engine(tmp_path)
    node_id = _node(engine)

    async def _drive():
        async with engine._write_lock:
            engine.store.append("node_failed", {
                "node_id": node_id, "generation": 0, "reason": "no_metric", "error": "real cause"})
        await engine._contain_eval_crash(node_id, 0, OSError("late teardown"))

    anyio.run(_drive)
    failed = [e for e in engine.store.read_all() if e.type == "node_failed"]
    assert len(failed) == 1, "the already-closed lifecycle must not be re-terminalized"
    assert _state(engine).nodes[node_id].error_reason == "no_metric"


def test_a_terminal_for_a_SUPERSEDED_generation_is_not_written(tmp_path):
    """The containment names the generation the body BOUND, not whatever is current when it fails:
    after a concurrent reset those are different lifecycles, and a terminal on the wrong one is
    worse than none."""
    engine = make_engine(tmp_path)
    node_id = _node(engine)

    async def _drive():
        await engine._contain_eval_crash(node_id, 99, OSError("stale"))

    anyio.run(_drive)
    assert not [e for e in engine.store.read_all() if e.type == "node_failed"]
    # ...but the run still pauses: the fault is real whatever lifecycle it belonged to.
    assert _state(engine).paused is True


def test_a_failing_append_inside_the_handler_is_swallowed(tmp_path):
    """This runs on the path where the EVENT LOG may be exactly what is broken. Raising here
    re-enters the failure mode the handler exists to contain, one frame further out."""
    engine = make_engine(tmp_path)
    node_id = _node(engine)

    def _no_append(*_a, **_kw):
        raise OSError(28, "No space left on device")

    engine.store.append = _no_append

    async def _drive():
        await engine._contain_eval_crash(node_id, 0, OSError("original"))

    anyio.run(_drive)          # must not raise
