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
    """Records BOTH ends of a span, because only the pair can tell an open one from a closed one.

    The first version appended the name on entry and did nothing on exit, so `opened[-1]` read
    exactly the same whether the span was still open or had been closed a line earlier. That made
    the test below unable to fail for the reason its own docstring gives. Driven 2026-08-31 with
    `_write_reflection_note` rewritten to

        with self._op_span("reflection"):
            pass
        return self.lessons.write_reflection_note(final)

    -- the provider called outside the span, which is the untraced-money shape itself, and the
    precise counterexample the test names -- all three tests here stayed green.
    """

    def __init__(self):
        self.opened = []                 # every span ever ENTERED, in order
        self.live = []                   # the ones open RIGHT NOW, innermost last

    @contextlib.contextmanager
    def span(self, name, **attrs):
        self.opened.append(name)
        self.live.append(name)
        try:
            yield
        finally:
            self.live.pop()


def _engine_with(tracer, calls):
    eng = Engine.__new__(Engine)
    eng.tracer = tracer
    # What is open AT THE MOMENT the provider is called, which is the only thing that decides
    # whether the call is billed inside a trace. `tracer.opened` cannot answer it: a span that has
    # already exited is still in there.
    eng.lessons = types.SimpleNamespace(
        write_reflection_note=lambda final: calls.append(list(tracer.live)))
    return eng


def test_reflection_runs_inside_a_span():
    tracer = _RecordingTracer()
    calls = []
    _engine_with(tracer, calls)._write_reflection_note(object())
    assert "reflection" in tracer.opened, (
        "run-end reflection made its billed calls with no operation span open")


def test_the_span_is_open_while_the_provider_is_called():
    """A span opened and closed BEFORE the call would still leave the money untraced.

    `attributes.phase` on a `generation` span is the surface every per-phase spend table is built
    from, and a span that has already exited stamps nothing onto the call that follows it. So the
    assertion is over the LIVE stack at call time, not over the names ever seen.
    """
    tracer = _RecordingTracer()
    calls = []
    _engine_with(tracer, calls)._write_reflection_note(object())
    assert calls == [["reflection"]], (
        f"the reflection provider call was made with these spans open: {calls} -- a span opened "
        "and closed before the call leaves the money exactly as untraced as no span at all")


def test_no_tracer_still_reflects():
    """Engines built by `__new__` in tests have no tracer; `_op_span` must stay a null context."""
    eng = Engine.__new__(Engine)
    done = []
    eng.lessons = types.SimpleNamespace(write_reflection_note=lambda final: done.append(1))
    eng._write_reflection_note(object())
    assert done == [1]
