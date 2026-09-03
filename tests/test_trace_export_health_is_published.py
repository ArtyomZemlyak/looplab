"""A dead span exporter must not be able to hide behind its own silence.

MEASURED on e5small-dr-unified-v12, 2026-09-01: `spans.jsonl` was last written at 18:20 while
`events.jsonl` was still appending 10.5 hours later. `py-spy dump` on the live engine (pid 803216)
lists MainThread, AnyIO workers and subprocess `_pump` threads and NO `looplab-trace-export-*`
thread at all: the worker is gone. `grep -c trace_export_loss` over the whole run: 0.

That silence is structural, not bad luck. The loss receipt is written BY the exporter
(`AsyncJsonlSpanExporter._worker_main` calls `self._writer._export_line` for its own receipt), so a
worker that has stopped cannot report that it stopped. The `_LOG.warning` merged in 1ac3b3a1 is
raised from the same doomed path. Meanwhile `AsyncJsonlSpanExporter.metrics()` — "a process-local,
race-consistent exporter health snapshot" carrying `worker_alive`, `shutdown`, `export_failures`,
`loss_receipt_failures` and every per-reason drop — was read by NOTHING in the product: the only
`.metrics()` reference outside tracing.py is `traceview.py`'s unrelated `exporter_metrics_partial`
key, computed from an omitted-span count.

So the engine, which is the one writer that outlives a dead exporter, publishes the snapshot on the
run's own log. These tests drive the publisher, the predicate that gates it, and the CALL ITSELF —
a health method nobody calls would be the exact defect this fixes, one level up.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import types

import pytest

from looplab.engine.orchestrator import Engine
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_TRACE_EXPORT_HEALTH,
                                  trace_export_unhealthy)

HEALTHY = {
    "accepted_spans": 12, "exported_spans": 12, "dropped_spans": 0,
    "export_failures": 0, "queued_spans": 0, "buffered_bytes": 0,
    "loss_receipts": 0, "loss_receipt_failures": 0,
    "worker_alive": True, "shutdown": False,
}


def _snapshot(**overrides):
    return dict(HEALTHY, **overrides)


def _engine(snapshot, *, raises=None):
    """Minimal stand-in carrying only what the publisher reads: the tracer's exporter and the log."""
    appended: list[tuple[str, dict]] = []

    def metrics():
        if raises is not None:
            raise raises
        return dict(snapshot)

    return types.SimpleNamespace(
        tracer=types.SimpleNamespace(exporter=types.SimpleNamespace(metrics=metrics)),
        store=types.SimpleNamespace(append=lambda kind, data: appended.append((kind, data))),
        _trace_export_health_seen=(),
    ), appended


# --------------------------------------------------------------------------- the predicate

@pytest.mark.parametrize("snapshot, unhealthy, why", [
    (_snapshot(), False, "a working exporter"),
    (_snapshot(shutdown=True), True, "stopped accepting for good"),
    (_snapshot(worker_alive=False, queued_spans=3), True, "rows queued behind a dead worker"),
    (_snapshot(worker_alive=False), False, "idle with an empty queue owns no thread"),
    (_snapshot(dropped_spans=1), True, "a recorded drop"),
    (_snapshot(export_failures=7), True, "v12's shape: writes failing, worker gone"),
    (_snapshot(loss_receipt_failures=1), True, "the receipt that could not report itself"),
])
def test_the_predicate_separates_a_working_exporter_from_a_failing_one(snapshot, unhealthy, why):
    assert trace_export_unhealthy(snapshot) is unhealthy, why


@pytest.mark.parametrize("reason, unhealthy", [
    ("crashed", True),          # an exception escaped the worker loop
    ("receipt_failed", True),   # the loss receipt itself could not be written
    ("abandoned", True),        # terminal ownership released: this process writes no more spans
    ("idle", False),            # parked with nothing queued; the next submit restarts it
    ("retired", False),         # handed the file off; the next submit restarts it
    ("", False),                # never stopped
])
def test_a_worker_that_died_badly_is_published_even_with_nothing_dropped(reason, unhealthy):
    """The case this whole row exists for, and the predicate was SILENT on it until 2026-09-03.

    `core/tracing.py::TRACE_WORKER_STOP_REASONS` landed after the predicate did. Before it, the
    five terminal paths were byte-identical from the outside, so a crashed worker with an empty
    queue and zero drops looked exactly like a worker resting between submits — which is v12's
    shape: no receipts, no drops, no thread, and no row. Splitting routine parking (`idle`,
    `retired`) from span loss (`crashed`, `receipt_failed`, `abandoned`) is what makes the silence
    reportable.
    """
    snapshot = _snapshot(worker_alive=False, worker_stop_reason=reason)
    assert trace_export_unhealthy(snapshot) is unhealthy


def test_the_stop_reason_is_read_from_the_snapshot_the_exporter_actually_returns():
    """Non-vacuity against a key that does not exist: `metrics()` must really carry this.

    A predicate keyed on a field the exporter never reports would pass every test above and fire
    never in production — the exact shape of a guard that cannot fail.
    """
    import pathlib
    import tempfile

    from looplab.core.tracing import AsyncJsonlSpanExporter

    exporter = AsyncJsonlSpanExporter(
        pathlib.Path(tempfile.mkdtemp()) / "spans.jsonl", run_id="probe")
    keys = set(exporter.metrics())
    assert "worker_stop_reason" in keys
    assert {"stopped_crashed", "stopped_idle"} <= keys


def test_the_denylist_names_only_reasons_the_exporter_can_produce():
    """A word in the denylist that nothing emits is a decoy, the same rule the registry states."""
    from looplab.core.tracing import TRACE_WORKER_STOP_REASONS
    from looplab.events.types import _TRACE_WORKER_STOPS_THAT_LOSE_SPANS

    assert _TRACE_WORKER_STOPS_THAT_LOSE_SPANS <= set(TRACE_WORKER_STOP_REASONS)


def test_the_predicate_refuses_a_non_snapshot_rather_than_raising():
    # The publisher calls this on whatever `metrics()` returned; a diagnostic read may never
    # take the run loop down with it.
    for junk in (None, [], "shutdown", 3):
        assert trace_export_unhealthy(junk) is False


def test_a_boolean_is_not_a_count():
    # `True == 1` in Python, so a snapshot whose counter arrived as a bool must not read as loss.
    assert trace_export_unhealthy(_snapshot(dropped_spans=True)) is False


# --------------------------------------------------------------------------- the publisher

def test_a_dead_exporter_reaches_the_run_log():
    engine, appended = _engine(_snapshot(worker_alive=False, export_failures=7,
                                         loss_receipt_failures=1))
    assert Engine._record_trace_export_health(engine) is True
    assert len(appended) == 1
    kind, data = appended[0]
    assert kind == EV_TRACE_EXPORT_HEALTH
    # The row carries the evidence, not merely the alarm: without these an operator learns
    # nothing the silence did not already tell them.
    assert data["worker_alive"] is False
    assert data["export_failures"] == 7
    assert data["loss_receipt_failures"] == 1


def test_a_healthy_exporter_writes_nothing():
    engine, appended = _engine(_snapshot())
    assert Engine._record_trace_export_health(engine) is False
    assert appended == []


def test_the_healthy_case_is_not_vacuous():
    # Same stub, same call, one field flipped: proves the empty log above is the predicate's
    # doing and not a harness that cannot append at all.
    engine, appended = _engine(_snapshot(shutdown=True))
    assert Engine._record_trace_export_health(engine) is True
    assert [kind for kind, _ in appended] == [EV_TRACE_EXPORT_HEALTH]


def test_a_permanently_dead_exporter_is_recorded_once_per_state():
    snapshot = _snapshot(worker_alive=False, export_failures=7)
    engine, appended = _engine(snapshot)
    assert Engine._record_trace_export_health(engine) is True
    assert Engine._record_trace_export_health(engine) is False, "an unchanged state re-appends"
    assert len(appended) == 1
    # A state that MOVES is news again — the loss is still growing.
    snapshot["export_failures"] = 9
    assert Engine._record_trace_export_health(engine) is True
    assert len(appended) == 2
    assert appended[-1][1]["export_failures"] == 9


def test_a_metrics_read_that_raises_never_reaches_the_loop():
    engine, appended = _engine(_snapshot(shutdown=True), raises=RuntimeError("exporter is wedged"))
    assert Engine._record_trace_export_health(engine) is False
    assert appended == []


def test_an_exporter_without_the_snapshot_is_not_an_error():
    # `JsonlSpanExporter` (the synchronous one) has no `metrics()`; a run configured with it must
    # not fail here.
    engine = types.SimpleNamespace(
        tracer=types.SimpleNamespace(exporter=types.SimpleNamespace()),
        store=types.SimpleNamespace(append=lambda *a: pytest.fail("appended without a snapshot")),
        _trace_export_health_seen=(),
    )
    assert Engine._record_trace_export_health(engine) is False


# --------------------------------------------------------------------------- the call site

def test_the_run_loop_actually_calls_the_publisher():
    tree = ast.parse(pathlib.Path(inspect.getsourcefile(Engine)).read_text())
    engine_cls = next(n for n in tree.body
                      if isinstance(n, ast.ClassDef) and n.name == "Engine")
    # `_run_with_llm_broker` owns the turn loop (`Engine.run` delegates into it) — the same
    # method whose thirteen exits `run_loop_exited` had to name in 8da60134.
    loop_owner = next(n for n in engine_cls.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == "_run_with_llm_broker")
    loops = [n for n in ast.walk(loop_owner) if isinstance(n, ast.While)]
    assert loops, "_run_with_llm_broker no longer has a loop to publish from"

    def calls_publisher(node):
        return [c for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "_record_trace_export_health"]

    in_loop = [c for loop in loops for c in calls_publisher(loop)]
    assert in_loop, ("_run_with_llm_broker does not call _record_trace_export_health — a health snapshot "
                     "nobody reads is the defect this change exists to close")

    # And it must publish BEFORE the decision prefix is read, or the row lands after this turn's
    # fold and moves the tail under the seq recheck that follows it.
    reads = [n for loop in loops for n in ast.walk(loop)
             if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "decision_events" for t in n.targets)]
    assert reads, "the decision prefix is no longer read into `decision_events`"
    assert min(c.lineno for c in in_loop) < min(r.lineno for r in reads)


def test_the_event_is_declared_diagnostic():
    # Invariant #1: the engine is the sole writer of FOLDED domain events. This row is engine-
    # written but carries no domain meaning, and `_proposal_authority_seq` excludes
    # DIAGNOSTIC_EVENTS wholesale — so a health row cannot displace a paid proposal.
    assert EV_TRACE_EXPORT_HEALTH in DIAGNOSTIC_EVENTS
