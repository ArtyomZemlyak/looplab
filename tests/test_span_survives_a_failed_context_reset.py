"""A span's row must not depend on its context bookkeeping succeeding.

MEASURED on `runs/e5small-dr-unified-v12/spans.jsonl`. Joining every `parent_id` against the
`span_id`s actually present:

    spans 3618, distinct ids 3618, distinct parents 31
    parents with NO row of their own: 4, covering 268 children
        891a4e7216bf6d  256 children   <- the parent of the LAST SIX spans the run ever wrote

A row is written on CLOSE, so an operation that loses its close writes nothing while its children
write normally — which is exactly the shape of those four orphans.

THE CAUSE, driven rather than reasoned: `Tracer.span`'s `finally` ended with the export, after five
`ContextVar.reset` calls. `reset` RAISES `ValueError: <Token ...> was created in a different
Context` when a span's enter and exit land in different contexts, and a raising reset skipped the
export entirely. Entering under `contextvars.copy_context().run(...)` and exiting outside it
reproduces it in three lines.

The fix is an ORDERING: export first, then unwind. The export stays wrapped, so a failing exporter
still cannot mask the in-flight exception; what changes is that context bookkeeping no longer
decides whether a diagnostic gets recorded.

NOT A COMPLETE EXPLANATION of v12's 33-hour silence, and this file does not claim to be one: the
tracer SURVIVES the failed reset — a later span writes normally, which the last test here pins.
"""
from __future__ import annotations

import contextvars
import pathlib
import tempfile

import pytest

from looplab.core.tracing import JsonlSpanExporter, Tracer


def _tracer():
    path = pathlib.Path(tempfile.mkdtemp()) / "spans.jsonl"
    return Tracer(JsonlSpanExporter(path), run_id="probe"), path


def _rows(path) -> int:
    return sum(1 for _ in open(path, errors="ignore")) if path.exists() else 0


def _cross_context_exit(tracer, name="crossed"):
    """Enter the span inside a COPIED context and leave it in the outer one."""
    cm = tracer.span(name, new_trace=True)
    contextvars.copy_context().run(cm.__enter__)
    with pytest.raises(ValueError):
        cm.__exit__(None, None, None)


def test_a_span_whose_reset_raises_is_still_recorded():
    tracer, path = _tracer()
    with tracer.span("ordinary", new_trace=True):
        pass
    before = _rows(path)
    _cross_context_exit(tracer)
    assert _rows(path) == before + 1, "the span lost its row when the context reset raised"


def test_the_non_vacuity_partner_an_ordinary_span_records_too():
    # Without this, the assertion above could pass on a tracer that writes rows for everything
    # regardless of what happened — it pins that a normal close is the baseline being matched.
    tracer, path = _tracer()
    assert _rows(path) == 0
    with tracer.span("ordinary", new_trace=True):
        pass
    assert _rows(path) == 1


def test_the_failed_reset_still_raises_and_is_not_swallowed():
    # The ValueError is real information — enter and exit crossed a context boundary — and the fix
    # must not hide it. Only the ORDER changed, never the propagation.
    tracer, _ = _tracer()
    _cross_context_exit(tracer)          # the pytest.raises inside IS the assertion


def test_an_exception_inside_the_span_still_records_and_re_raises():
    tracer, path = _tracer()
    with pytest.raises(RuntimeError):
        with tracer.span("boom", new_trace=True):
            raise RuntimeError("the body failed")
    assert _rows(path) == 1


def test_an_exporter_that_raises_never_masks_the_body_s_exception():
    # The export moved earlier in `finally`; it must stay wrapped, or a disk-full exporter would
    # replace the caller's real failure with its own.
    path = pathlib.Path(tempfile.mkdtemp()) / "spans.jsonl"

    class Angry(JsonlSpanExporter):
        def export(self, span):
            raise OSError("no space left on device")

    tracer = Tracer(Angry(path), run_id="probe")
    with pytest.raises(RuntimeError):
        with tracer.span("boom", new_trace=True):
            raise RuntimeError("the body failed")


def test_the_tracer_keeps_working_after_a_failed_reset():
    # Pins what this fix does NOT claim: a lost close does not kill the tracer, so it is not by
    # itself the explanation for a run that stopped writing spans entirely.
    tracer, path = _tracer()
    _cross_context_exit(tracer)
    before = _rows(path)
    with tracer.span("after", new_trace=True):
        pass
    assert _rows(path) == before + 1


def test_a_FAILED_RESET_NEVER_REPLACES_THE_CALLERS_EXCEPTION():
    """The other half of the same `finally`, and the one the export was already wrapped against.

    The export is wrapped with the comment that a failing exporter "must never mask the in-flight
    exception". The seven `ContextVar.reset` calls immediately below it were bare — and a raising
    `reset` in a `finally` does exactly that: it REPLACES the exception already unwinding. So an
    eval failing with `BudgetExceeded`, or a proposal with an `OSError`, surfaced as
    `ValueError: Token ... was created in a different Context`; every `except BudgetExceeded` in the
    tree missed it, and the tracer's own bookkeeping decided the caller's terminal. It also
    abandoned the six restores after the one that raised.

    The shape is MORE reachable now, not less: the paid proposal moved onto `anyio.to_thread`, and
    a thread hop copies the context.

    MUTATION: make the restores bare again -> the caller's `RuntimeError` is gone and a `ValueError`
    about a token arrives in its place.
    """
    tracer, path = _tracer()
    before = _rows(path)
    cm = tracer.span("crossed", new_trace=True)
    contextvars.copy_context().run(cm.__enter__)

    with pytest.raises(RuntimeError, match="the body failed"):
        try:
            raise RuntimeError("the body failed")
        except RuntimeError:
            if cm.__exit__(*__import__("sys").exc_info()) is not True:
                raise

    assert _rows(path) == before + 1, "and the row still lands"


def test_EVERY_restore_is_attempted_even_when_one_raises():
    """A `finally` that stops at the first failure leaves the tracer's own state describing a span
    that has closed — a node id, a generation and a phase that outlive the block they belonged to,
    on every span written afterwards."""
    from looplab.core import tracing

    tracer, _path = _tracer()
    cm = tracer.span("crossed", new_trace=True, node_id=7, phase="train")
    contextvars.copy_context().run(cm.__enter__)
    with pytest.raises(ValueError):
        cm.__exit__(None, None, None)

    assert tracing._node_ctx.get() is None, "the node id must not outlive the span"
    assert tracing._phase_ctx.get() is None, "nor the phase"
    assert tracing._capture_ctx.get() is None, "nor the capture policy"
