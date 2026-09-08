"""The paid cadence block runs OFF the loop thread, and writes NOTHING folded from its worker.

WHAT WAS WRONG (`paid-cadences-hold-the-engine-loop`, closed 2026-09-08). `_run_cadences` — the
Strategist consult, the concept re-tag/consolidation pass, the verifier tie-break, the report
refresh, `foresight_rank`, the deep-research step and the lesson distillations — was a plain `def`
with no `await` in it, called as `state = self._run_cadences(state)` from the async spine. So the
whole block executed as ONE event-loop callback and nothing else on the loop could run while it
spent. Measured on v11 (a full 24 h run, 9 evaluations) the block is ~15.5 min of `operation` span
against `evaluate`'s 5026.6 min — 0.3 % of wall clock, which is why this sat open — but the minutes
are not the cost. What the loop owes during them is: the eval watcher tick, operator abort/reset
detection, the train-monitor kill signal and the control ACK. `cadence.at_creation_boundary` made
these gates due WHILE evaluations run, so every one of those minutes is a burning GPU that cannot be
stopped.

WHAT THIS FILE DRIVES, in the order the risk runs:
  1. THE HOLD — a tick counter against a cadence that really blocks, and its negative control: the
     same block called the old way counts ZERO ticks (the 177->177 / 38->38 measurement, as a test);
  2. INVARIANT #1 — the worker writes no FOLDED row; the main task publishes them after the await.
     Read from INSIDE the worker, against the REAL store, so a bare `to_thread` goes red here;
  3. the two registered thread-side seams (`DIAGNOSTIC_EVENTS`, `BACKGROUND_APPENDABLE`) still pass
     straight through — buffering them would be an observability regression, and their position is
     the one thing those registries exist to say is safe;
  4. the buffer is published even when the cadence RAISES (a Strategist and a deep-research step
     both spend, so `BudgetExceeded` out of this block is a real path);
  5. the sink is invisible to every other task, which is the entire safety argument for resolving
     `Engine.store` through a ContextVar;
  6. a tail-fenced append is REFUSED rather than silently unfenced.
"""
from __future__ import annotations

import functools
import threading
import time

import anyio
import pytest

from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, Node, NodeStatus
from looplab.engine.orchestrator import _CADENCE_STORE_SINK, _BufferedCadenceStore
from looplab.events.replay import fold
from tests.factories import make_engine

_BLOCK_S = 0.20          # long enough that a turning loop ticks many times, short enough to be free


def _busy_nodes() -> dict:
    """One node landed, one still training — the state every GPU run on this box is in ~always."""
    return {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                status=NodeStatus.evaluated, metric=1.0),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft"), status=NodeStatus.pending),
    }


class _SlowReportWriter:
    """A paid cadence that really spends: `generate` blocks the thread it is called on, exactly as a
    provider request does. `seen_rows` is what the REAL log held at the instant it was called."""

    def __init__(self, engine=None):
        self.calls = 0
        self.engine = engine
        self.seen_rows: list[str] = []
        self.entered = threading.Event()

    def generate(self, state, trigger=""):
        self.calls += 1
        if self.engine is not None:
            # The REAL store, not `engine.store` — inside the worker that name resolves to the
            # buffered view, and the question here is what a reader OUTSIDE this worker can see.
            self.seen_rows = [e.type for e in self.engine._event_store.read_all()]
        self.entered.set()
        time.sleep(_BLOCK_S)
        return {"headline": "h", "verdict": "v", "at_node": len(state.nodes), "trigger": trigger}


def _engine(tmp_path, name, writer):
    eng = make_engine(tmp_path / name, report_writer=writer, report_every=1,
                      cadence_while_evaluating=True)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                     "direction": "min"})
    state = fold(eng.store.read_all())
    state.nodes = _busy_nodes()
    return eng, state


async def _ticks_around(body):
    """Run `body(mark)` under a ticking sibling task; return (ticks the loop turned during it, out).

    `mark()` samples the counter, so the measurement brackets exactly the cadence and not the setup.
    """
    ticks = 0
    sampled: list[int] = []

    async def _ticker():
        nonlocal ticks
        while True:
            await anyio.sleep(0.001)
            ticks += 1

    async with anyio.create_task_group() as tg:
        tg.start_soon(_ticker)
        await anyio.sleep(0.02)                  # the ticker is really running before we measure
        out = await body(lambda: sampled.append(ticks))
        tg.cancel_scope.cancel()
    assert len(sampled) == 2, "the body must mark before and after the block"
    return sampled[1] - sampled[0], out


# ----------------------------------------------------------------------------- 1. THE HOLD

def test_the_loop_turns_while_the_paid_cadence_block_spends(tmp_path):
    """THE PROPERTY. A cadence that really blocks for `_BLOCK_S`, and the loop keeps turning
    throughout — which is what an eval watcher, an abort poll and a kill signal need."""
    writer = _SlowReportWriter()
    eng, state = _engine(tmp_path, "turns", writer)

    async def _body(mark):
        mark()
        out = await eng._offload_cadence(functools.partial(eng._run_cadences, state))
        mark()
        return out

    turned, _out = anyio.run(_ticks_around, _body)
    assert writer.calls == 1, "the cadence must really have run"
    assert turned > 5, f"the loop turned {turned} times while the cadence spent {_BLOCK_S}s"


def test_the_same_block_called_the_old_way_turns_the_loop_zero_times(tmp_path):
    """THE NEGATIVE CONTROL, and it is the measurement this item was closed on: called as
    `state = self._run_cadences(state)` from the async spine, the whole block is ONE event-loop
    callback, so a sibling task cannot run at all. 177->177, 38->38, 36->36, 34->34."""
    writer = _SlowReportWriter()
    eng, state = _engine(tmp_path, "holds", writer)

    async def _body(mark):
        mark()
        out = eng._run_cadences(state)           # exactly the pre-2026-09-08 call
        mark()
        return out

    turned, _out = anyio.run(_ticks_around, _body)
    assert writer.calls == 1
    assert turned == 0, f"the loop turned {turned} times inside a synchronous cadence block"


# -------------------------------------------------------------------------- 2. INVARIANT #1

def test_the_worker_writes_no_folded_row_and_the_main_task_publishes_it(tmp_path):
    """The row a cadence writes is FOLDED and authority-bearing (`verifier_group_scored` MOVES the
    champion tie-break), so it may not land from a worker thread. It must be invisible in the REAL
    log while the worker runs and present the moment the await returns."""
    writer = _SlowReportWriter()
    eng, state = _engine(tmp_path, "sink", writer)
    writer.engine = eng

    async def _run():
        return await eng._offload_cadence(functools.partial(eng._run_cadences, state))

    anyio.run(_run)
    assert writer.calls == 1
    assert "report_generated" not in writer.seen_rows, (
        "a folded cadence row was appended from the worker thread — invariant #1's sole-writer rule")
    assert "report_generated" in [e.type for e in eng.store.read_all()], (
        "the buffered row was never published by the main task")


def test_the_worker_reads_back_its_own_buffered_rows(tmp_path):
    """`_run_cadences` threads one `state` through eleven consumers and several re-fold after
    writing. A view that hid the buffer would hand the next consumer a state missing the row the
    previous one just decided — stale, not merely delayed."""
    eng, _state = _engine(tmp_path, "readback", _SlowReportWriter())
    view = _BufferedCadenceStore(eng.store)
    view.append("report_generated", {"at_node": 3})
    assert [e.type for e in view.read_all()][-1] == "report_generated"
    assert "report_generated" not in [e.type for e in eng.store.read_all()]
    assert view.read_all()[-1].seq > view.read_all()[-2].seq, "the view's own seqs stay ordered"


# ------------------------------------------------------------ 3. the registered thread-side seams

def test_the_two_registered_thread_side_seams_pass_straight_through(tmp_path):
    """`DIAGNOSTIC_EVENTS` is fold-ignored AND excluded wholesale from every seq-equality fence;
    `BACKGROUND_APPENDABLE` is the concurrent-research allow-list with a splice-neutrality proof.
    Both are appended from worker threads today, and buffering them would be an observability
    regression — `phase_progress` is how the UI shows a multi-minute cadence is alive and
    `llm_usage` is the durable ledger a spend ceiling is read off."""
    eng, _state = _engine(tmp_path, "seams", _SlowReportWriter())
    live: list[list[str]] = []

    def _cadence():
        eng.store.append("phase_progress", {"phase": "report", "stage": "x"})
        eng.store.append("llm_usage", {"cost": 0.01})
        eng.store.append("report_generated", {"at_node": 1})
        live.append([e.type for e in eng._event_store.read_all()])
        return "done"

    async def _run():
        return await eng._offload_cadence(_cadence)

    assert anyio.run(_run) == "done"
    assert live[0][-2:] == ["phase_progress", "llm_usage"], "a registered seam was buffered"
    assert "report_generated" not in live[0]
    assert [e.type for e in eng.store.read_all()][-1] == "report_generated"


# ----------------------------------------------------------------------- 4. the publish on a raise

def test_the_buffer_is_published_when_the_cadence_raises(tmp_path):
    """A `BudgetExceeded` out of this block is a real path — the Strategist and the deep-research
    step both spend. Discarding the rows that were durable at emit time before the offload existed
    would buy the same paid pass again on resume, because the receipts are the gate."""
    eng, _state = _engine(tmp_path, "raise", _SlowReportWriter())

    def _cadence():
        eng.store.append("report_generated", {"at_node": 1})
        raise BudgetExceeded("ceiling")

    async def _run():
        await eng._offload_cadence(_cadence)

    with pytest.raises(BudgetExceeded):
        anyio.run(_run)
    assert "report_generated" in [e.type for e in eng.store.read_all()]


def test_the_sink_is_removed_even_when_the_cadence_raises(tmp_path):
    """A leaked sink would send every later main-task append into a buffer nobody publishes."""
    eng, _state = _engine(tmp_path, "leak", _SlowReportWriter())

    async def _run():
        with pytest.raises(RuntimeError):
            await eng._offload_cadence(functools.partial(_boom))
        assert _CADENCE_STORE_SINK.get() is None
        eng.store.append("report_generated", {"at_node": 1})

    def _boom():
        raise RuntimeError("cadence exploded")

    anyio.run(_run)
    assert "report_generated" in [e.type for e in eng.store.read_all()]


# ------------------------------------------------------- 5. the sink belongs to ONE task, not all

def test_a_sibling_task_appending_during_the_block_still_reaches_the_real_log(tmp_path):
    """THE SAFETY ARGUMENT for resolving `Engine.store` through a ContextVar, driven: an evaluation
    running beside the cadence carries the context it was SPAWNED with, so it cannot see this
    buffer. If it could, a `node_evaluated` would be delayed behind a paid cadence — invariant #2's
    terminal held hostage by an enrichment pass."""
    writer = _SlowReportWriter()
    eng, state = _engine(tmp_path, "sibling", writer)

    async def _sibling():
        await anyio.to_thread.run_sync(writer.entered.wait, abandon_on_cancel=True)
        eng.store.append("node_evaluated", {"node_id": 9, "generation": 0, "metric": 1.0})

    async def _run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(_sibling)
            await eng._offload_cadence(functools.partial(eng._run_cadences, state))

    anyio.run(_run)
    kinds = [e.type for e in eng.store.read_all()]
    assert kinds.index("node_evaluated") < kinds.index("report_generated"), (
        "the sibling's terminal was buffered behind the cadence, or never landed live")


# ------------------------------------------------------------------------ 6. the refused promises

@pytest.mark.parametrize("kwargs", [{"expected_last_seq": 3}, {"require_durable": True}])
def test_a_promise_the_buffer_cannot_keep_is_refused(tmp_path, kwargs):
    """`expected_last_seq` is a promise about the tail of the REAL log at the instant of the append,
    and a buffered row cannot keep it: the publish happens later, against a tail that has moved by
    construction. No cadence uses one today; a future one gets this instead of an unfenced write."""
    eng, _state = _engine(tmp_path, f"cas{sorted(kwargs)[0]}", _SlowReportWriter())
    view = _BufferedCadenceStore(eng.store)
    with pytest.raises(TypeError, match="cadence worker"):
        view.append("report_generated", {"at_node": 1}, **kwargs)
    # …and a registered pass-through type keeps the real store's own semantics, untouched.
    view.append("phase_progress", {"phase": "report"}, **{k: v for k, v in kwargs.items()
                                                          if k == "require_durable"})
