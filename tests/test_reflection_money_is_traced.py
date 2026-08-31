"""Run-end reflection is BILLED; it must not be invisible.

MEASURED over the 68-run probe corpus on 2026-08-29: 105 of 25,430 billed calls ($0.1916 of
$100.2691) carry no `span_id`, and every one of them lands with NO operation span open at all.
Correlating dsIF6's four against its own timeline pins the window exactly — `report` opens a span
and its generation sits inside it, then `finalize_step: reflection_begun` fires and the next three
generations (and the run-end lessons pass after them) have nothing open. `run.log` says so in as
many words: "untraced LLM generation: no operation span open at tool_loop.py:821 ... this call IS
BILLED and will appear in no trace surface."

`_write_reflection_note` is the one enrichment-lane entry point on the finalization path with no
`_op_span`, while its siblings `_maybe_distill_lessons`, `_maybe_refresh_lessons` and
`_maybe_reconcile_lessons` all have one. One span there covers everything reflection calls,
including `_reflect_lessons` and `_comparative_lessons`.

The dollars are small. What is not small: `attributes.phase` on `generation` spans is the surface
every per-phase spend table in docs/56 is built from, so money outside a span is money no
accounting of this campaign can see -- solHull's reflection alone was $0.0508.
"""
import contextlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from looplab.engine.orchestrator import Engine  # noqa: E402


class _RecordingTracer:
    def __init__(self):
        self.opened = []

    @contextlib.contextmanager
    def span(self, name, **attrs):
        self.opened.append(name)
        yield


def _engine_with(tracer, calls):
    eng = Engine.__new__(Engine)
    eng.tracer = tracer
    eng.lessons = types.SimpleNamespace(
        write_reflection_note=lambda final: calls.append(tracer.opened[-1] if tracer.opened
                                                         else None))
    return eng


def test_reflection_runs_inside_a_span():
    tracer = _RecordingTracer()
    calls = []
    _engine_with(tracer, calls)._write_reflection_note(object())
    assert "reflection" in tracer.opened, (
        "run-end reflection made its billed calls with no operation span open")


def test_the_span_is_open_while_the_provider_is_called():
    """A span opened and closed BEFORE the call would still leave the money untraced."""
    tracer = _RecordingTracer()
    calls = []
    _engine_with(tracer, calls)._write_reflection_note(object())
    assert calls == ["reflection"], f"reflection called outside its own span: {calls}"


def test_no_tracer_still_reflects():
    """Engines built by `__new__` in tests have no tracer; `_op_span` must stay a null context."""
    eng = Engine.__new__(Engine)
    done = []
    eng.lessons = types.SimpleNamespace(write_reflection_note=lambda final: done.append(1))
    eng._write_reflection_note(object())
    assert done == [1]
