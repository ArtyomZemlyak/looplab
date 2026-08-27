"""A billed LLM call that opens no span must NAME ITSELF in the log.

MEASURED over two completed runs: `rubertlite-dr-unified-v9` bills 3,103 provider calls and opens
2,815 `generation` spans; v8 bills 5,788 and opens 5,247. Both gaps are 9.3 % of calls — 288 and 541
DISTINCT full-sized chats (prompts 6.7k-16.5k, completions 66-8,032), 6.8 % and 5.0 % of the run's
tokens — and they appear in no trace surface at all.

THE CAUSE IS STRUCTURAL: `Tracer.span` binds `_current_tracer`, so the tracer reaches nested code
only as a side effect of an outer span being open, and `generation` no-ops when it is unset. NOT a
thread problem — `anyio.to_thread.run_sync` propagates ContextVars; only a bare `threading.Thread`
does not.

WHICH sites those are could not be found by reading. Four candidate causes were eliminated by
measurement (stream->blocking fallback, embeddings, retries/keepalive stalls, the llm_parallel build
fan-out) and a fifth by arithmetic (the stage checker is 8-14x too small), because the calls reach
the client through role objects and shared funnels. So the engine names them itself.

Every assertion has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import io
import logging

import pytest

from looplab.core import tracing


@pytest.fixture
def caplines():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    log = logging.getLogger("looplab.core.tracing")
    log.addHandler(handler)
    prior = log.level
    log.setLevel(logging.WARNING)
    tracing._untraced_seen.clear()
    # This process traces: the reporter is deliberately silent in one that never built a Tracer
    # (the UI server, genesis, preflight), so every test below has to opt in explicitly.
    was_constructed = tracing._tracer_ever_constructed
    tracing._tracer_ever_constructed = True
    try:
        yield buf
    finally:
        log.removeHandler(handler)
        log.setLevel(prior)
        tracing._untraced_seen.clear()
        tracing._tracer_ever_constructed = was_constructed


def _untraced_call():
    with tracing.generation(op="chat", model="m"):
        pass


def _make_funnel(filename: str):
    """A function whose frame claims to live at `filename`.

    The production shape is `role -> core/llm.py::complete_text -> generation(...)`, and NOTHING in
    the tree calls `generation` from anywhere but the client. A test that calls it directly from the
    test module therefore exercises a frame shape production never has — which is why the original
    walk (break at the first non-tracing frame) passed here while naming only `core/llm.py` in every
    real run. Compiling with a chosen filename reproduces the real stack without importing the
    client or making a provider call.
    """
    ns: dict = {}
    exec(compile("def funnel(gen):\n    with gen(op='chat', model='m'):\n        pass\n",
                 filename, "exec"), ns)
    return ns["funnel"]


def test_an_untraced_generation_names_its_caller(caplines):
    """MUTATION: drop the `_note_untraced_generation()` call -> silence, and the investigation that
    cost four cycles has to start over."""
    _untraced_call()
    out = caplines.getvalue()

    assert "untraced LLM generation" in out
    assert "_untraced_call" in out, "the CALLER must be named, not the tracing module"
    assert "tracing.py" not in out.split(" in ")[0], (
        "MUTATION: report the first frame -> every line names tracing.py and no site is identified")
    assert "BILLED" in out, "the operator must learn the call cost money"


def test_it_reports_ONCE_per_site(caplines):
    """A hot loop must not fill the console. MUTATION: drop the `_untraced_seen` guard -> one line
    per call, and a 288-call run drowns its own console log."""
    for _ in range(5):
        _untraced_call()

    assert caplines.getvalue().count("untraced LLM generation") == 1


def test_two_DIFFERENT_sites_both_report(caplines):
    """MUTATION: key the guard on a constant instead of the site -> the second producer is never
    named, which is the whole deliverable."""
    def _other_caller():
        with tracing.generation(op="chat", model="m"):
            pass

    _untraced_call()
    _other_caller()
    out = caplines.getvalue()

    assert out.count("untraced LLM generation") == 2
    assert "_untraced_call" in out and "_other_caller" in out


def test_it_names_the_PRODUCER_and_not_the_shared_client_funnel(caplines):
    """The deliverable is WHICH role billed the untraced call, and the client is never the answer.

    Every `generation(...)` call site in the tree is inside `core/llm.py`, so a walk that stops at
    the first frame outside `tracing.py` reports `core/llm.py::complete_text` for all of them — the
    funnel the module docstring says grepping already finds. Worse, `_untraced_seen` is keyed on that
    string, so producers two onward are silenced entirely.

    MUTATION: `break` at the first non-tracing frame -> `site` is the client, `role_alpha` and
    `role_beta` never appear, and the second call logs nothing at all.
    """
    funnel = _make_funnel("/srv/looplab/looplab/core/llm.py")

    def role_alpha():
        funnel(tracing.generation)

    def role_beta():
        funnel(tracing.generation)

    role_alpha()
    role_beta()
    out = caplines.getvalue()

    assert out.count("untraced LLM generation") == 2, (
        "each distinct PRODUCER must report; keying on the shared funnel collapses them to one")
    assert "role_alpha" in out and "role_beta" in out
    for line in out.splitlines():
        named = line.split("no operation span open at ", 1)[1].split(" —")[0]
        assert "core/llm.py" not in named, f"named the funnel, not the producer: {named}"
    # The funnel is still worth printing — it says HOW the producer reached the client — but it
    # rides the `via` chain, which is not what the dedup key or the headline is built from.
    assert "via" in out and "core/llm.py" in out


def test_a_process_that_never_traced_stays_silent(caplines):
    """`Tracer` is constructed in exactly one place. The UI server, `looplab genesis`, preflight and
    the CLI helpers all bill provider calls with no tracer anywhere, by design.

    MUTATION: drop the `_tracer_ever_constructed` guard -> those surfaces log up to six WARNINGs
    telling the operator the call "will appear in no trace surface", and a trace bug is filed
    against a surface that never had one.
    """
    tracing._tracer_ever_constructed = False
    _untraced_call()

    assert caplines.getvalue() == ""


def test_a_TRACED_generation_reports_nothing(caplines, tmp_path):
    """The no-op branch is the only one that may speak.

    MUTATION: move the call above the `tr is None` check -> every healthy generation warns, which is
    both noise and a false accusation about calls that ARE traced.
    """
    exporter = tracing.JsonlSpanExporter(tmp_path / "spans.jsonl")
    tracer = tracing.Tracer(exporter, run_id="t")
    with tracer.span("op", kind="operation"):
        _untraced_call()

    assert "untraced LLM generation" not in caplines.getvalue()


def test_the_reporter_never_raises(caplines):
    """It runs inside a call that has already cost money; a diagnostic may not break it.

    MUTATION: remove the containment `except` -> a frame-walk failure propagates out of
    `generation` and kills the provider call it was only observing.
    """
    import inspect as _inspect

    def _boom():                                  # the frame walk itself fails
        raise RuntimeError("frame introspection unavailable")

    real = _inspect.currentframe
    _inspect.currentframe = _boom
    try:
        # MUST NOT PROPAGATE. Without a broad containment `except` this raises straight out of
        # `generation` and kills a provider call the diagnostic was only observing.
        tracing._note_untraced_generation()
        with tracing.generation(op="chat", model="m") as handle:
            assert handle is not None
    finally:
        _inspect.currentframe = real
