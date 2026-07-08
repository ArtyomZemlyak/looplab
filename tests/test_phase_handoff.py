"""Phase-handoff summaries: after each LLM phase in a node build, ONE extra call distills its
transcript into a brief injected into the NEXT phase (even across the role boundary), so later
phases trust what earlier ones explored instead of re-reading. Offline (fake clients)."""
from __future__ import annotations

import json

import pytest

from looplab.agents.agent import (
    run_phase, handoff_scope, summarize_phase, _flatten_transcript, _handoff_ctx)

_EMIT = {"type": "function", "function": {
    "name": "emit", "description": "final", "parameters": {"type": "object", "properties": {}}}}


def _emit_call():
    return {"content": "", "tool_calls": [{"id": "e", "function": {"name": "emit", "arguments": "{}"}}]}


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


class _BigReadTools:
    """One read tool returning a big blob (so the phase transcript clears summarize's min_chars)."""
    def specs(self):
        return [{"type": "function", "function": {
            "name": "read_file", "description": "", "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return "X" * 3000


class _PhaseClient:
    """Drives a phase to emit after one big read, and returns a canned summary for the handoff call.
    Records every message list handed to `chat` so a test can assert the injected brief."""
    def __init__(self, summary="REPO BRIEF: entrypoint=test.py, data=/d confirmed"):
        self.summary = summary
        self.seen = []
        self._script = None

    def chat(self, messages, tools, tool_choice="auto"):
        self.seen.append([dict(m) for m in messages])
        if self._script is None:
            self._script = iter([_tool_call("read_file", {"p": "a"}), _emit_call()])
        return next(self._script)

    def complete_text(self, messages):     # the handoff summary call (tool-free)
        return self.summary


def _run_one(client, msgs, label="phase"):
    return run_phase(client, _BigReadTools(), msgs, _EMIT, label=label,
                     finalize=lambda a: ("emit", a), fallback=lambda m: ("fb", None))


# --------------------------------------------------------------------------- no active scope
def test_run_phase_is_a_plain_loop_without_a_scope():
    # Outside a handoff_scope: no brief injected, no summary call — a drop-in for drive_tool_loop.
    c = _PhaseClient()
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}]
    out = _run_one(c, msgs)
    assert out[0] == "emit"
    assert _handoff_ctx.get() is None                      # no scope leaked
    # the first chat saw exactly system+user, nothing injected
    assert [m["role"] for m in c.seen[0]] == ["system", "user"]


# --------------------------------------------------------------------------- the handoff chain
def test_earlier_phase_summary_is_injected_into_the_next_phase():
    with handoff_scope(enabled=True):
        c1 = _PhaseClient(summary="PHASE-1 FINDINGS: files a.py,b.py; data at /d")
        _run_one(c1, [{"role": "system", "content": "S1"}, {"role": "user", "content": "phase 1"}],
                 label="Researcher·propose")
        # phase 2 (a DIFFERENT role) must receive phase 1's brief up front
        c2 = _PhaseClient()
        _run_one(c2, [{"role": "system", "content": "S2"}, {"role": "user", "content": "phase 2"}],
                 label="Developer·stages")
        first_turn = c2.seen[0]
        # injected right after the system message, as a user-role context block
        assert first_turn[0]["role"] == "system"
        assert first_turn[1]["role"] == "user" and "PHASE-1 FINDINGS" in first_turn[1]["content"]
        assert "EARLIER PHASES" in first_turn[1]["content"]
        assert first_turn[2]["content"] == "phase 2"        # the phase's own user prompt still follows


def test_scope_disabled_is_inert():
    with handoff_scope(enabled=False):
        assert _handoff_ctx.get() is None
        c = _PhaseClient()
        _run_one(c, [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}])
        assert [m["role"] for m in c.seen[0]] == ["system", "user"]   # nothing injected


def test_scope_resets_after_exit():
    with handoff_scope(enabled=True):
        assert _handoff_ctx.get() == []
    assert _handoff_ctx.get() is None


# --------------------------------------------------------------------------- summarize_phase unit
def test_summarize_phase_skips_a_trivial_transcript():
    # Too little explored -> no wasted LLM call (returns '').
    calls = []

    class _C:
        def complete_text(self, m):
            calls.append(m); return "should not be called"
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
    assert summarize_phase(_C(), msgs, phase="a", next_phase="b") == ""
    assert calls == []


def test_summarize_phase_distills_a_real_transcript():
    class _C:
        def complete_text(self, m):
            return "BRIEF: three bullets"
    msgs = [{"role": "system", "content": "ignored"},
            {"role": "assistant", "content": "reading", "tool_calls": [
                {"function": {"name": "read_file"}}]},
            {"role": "tool", "content": "Y" * 2500}]
    assert summarize_phase(_C(), msgs, phase="a", next_phase="b") == "BRIEF: three bullets"


def test_summarize_phase_never_raises_on_a_bad_client():
    assert summarize_phase(object(), [{"role": "tool", "content": "Z" * 3000}],
                           phase="a", next_phase="b") == ""


def test_summarize_phase_propagates_budget_exceeded():
    # A hard budget stop must NOT be masked by the optional summary call (mega-review MAJOR).
    from looplab.core.llm import BudgetExceeded

    class _C:
        def complete_text(self, m):
            raise BudgetExceeded("stop")
    with pytest.raises(BudgetExceeded):
        summarize_phase(_C(), [{"role": "tool", "content": "Y" * 3000}], phase="a", next_phase="b")


# --------------------------------------------------------------------------- terminal-phase guard
def test_handoff_false_skips_the_summary_call():
    calls = []

    class _C(_PhaseClient):
        def complete_text(self, m):
            calls.append(m); return "SHOULD NOT RUN"
    with handoff_scope(enabled=True):
        run_phase(_C(), _BigReadTools(),
                  [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}],
                  _EMIT, label="Developer·implement", handoff=False,
                  finalize=lambda a: ("emit", a), fallback=lambda m: ("fb", None))
        assert _handoff_ctx.get() == []       # terminal phase contributed nothing
        assert calls == []                    # and spent no summary LLM call


# --------------------------------------------------------------------------- shared read cache
class _CountReadTools:
    def __init__(self):
        self.reads = 0

    def specs(self):
        return [{"type": "function", "function": {
            "name": "read_file", "description": "", "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        self.reads += 1
        return "file contents"


class _ReadThenEmit:
    def __init__(self):
        self.n = 0

    def chat(self, messages, tools, tool_choice="auto"):
        self.n += 1
        return _tool_call("read_file", {"p": "a.py"}) if self.n == 1 else _emit_call()

    def complete_text(self, m):
        return ""     # keep the ledger empty so this test isolates the read cache


def test_read_cache_is_shared_across_phases_in_a_scope():
    tools = _CountReadTools()
    with handoff_scope(enabled=True):
        run_phase(_ReadThenEmit(), tools,
                  [{"role": "system", "content": "S1"}, {"role": "user", "content": "p1"}],
                  _EMIT, label="stages", finalize=lambda a: ("e", a), fallback=lambda m: ("f", None))
        run_phase(_ReadThenEmit(), tools,
                  [{"role": "system", "content": "S2"}, {"role": "user", "content": "p2"}],
                  _EMIT, label="plan", finalize=lambda a: ("e", a), fallback=lambda m: ("f", None))
    assert tools.reads == 1     # phase 2's identical read served from the shared node cache, not re-run


def test_handoff_summary_is_traced_as_its_own_span():
    # The summary call is wrapped in a `handoff-summary` operation span so it's a visible, labeled
    # band in the UI trace (not an anonymous generation) — auditable, per the user's ask.
    from looplab.core import tracing
    from looplab.core.tracing import Tracer, JsonlSpanExporter
    import tempfile
    import os
    import orjson

    class _SummClient(_PhaseClient):
        def complete_text(self, m):
            return "THE BRIEF"
    with tempfile.TemporaryDirectory() as d:
        tr = Tracer(JsonlSpanExporter(os.path.join(d, "s.jsonl")), run_id="r")
        tok = tracing._current_tracer.set(tr)
        try:
            with tracing.operation("stages"), handoff_scope(enabled=True):
                _run_one(_SummClient(), [{"role": "system", "content": "S"},
                                         {"role": "user", "content": "go"}], label="stages")
        finally:
            tracing._current_tracer.reset(tok)
        recs = [orjson.loads(x) for x in open(os.path.join(d, "s.jsonl"), "rb").read().splitlines()]
        names = [r["name"] for r in recs]
        assert "handoff-summary" in names                       # the summary got its own span
        hs = next(r for r in recs if r["name"] == "handoff-summary")
        assert hs["attributes"].get("handoff_from") == "stages"  # tagged with the source phase


def test_read_cache_is_per_loop_without_a_scope():
    # No scope → each phase gets its own loop-local cache → the identical read runs in BOTH phases.
    tools = _CountReadTools()
    for tag in ("p1", "p2"):
        run_phase(_ReadThenEmit(), tools,
                  [{"role": "system", "content": "S"}, {"role": "user", "content": tag}],
                  _EMIT, label=tag, finalize=lambda a: ("e", a), fallback=lambda m: ("f", None))
    assert tools.reads == 2


def test_flatten_drops_system_and_labels_tools():
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "assistant", "content": "hmm", "tool_calls": [{"function": {"name": "grep"}}]},
            {"role": "tool", "content": "result"}]
    text = _flatten_transcript(msgs)
    assert "SYS" not in text
    assert "tool calls: grep" in text and "[tool result] result" in text
