"""B1/C1/C2 · No-progress stuck detection, self-plan, and auto-summary for the shared agent loop.
Offline (fake chat client), no model needed."""
from __future__ import annotations

import json

from looplab.agents.agent import drive_tool_loop, loop_opts_from_settings
from looplab.core.context_budget import compact_history
from looplab.agents.stuck import StuckDetector


# --------------------------------------------------------------------------- detector unit
def test_stuck_repeated_pair_trips_at_threshold():
    d = StuckDetector(repeat_threshold=4)
    assert d.push("read", {"f": "a"}, "same") is None      # 1
    assert d.push("read", {"f": "a"}, "same") is None      # 2
    assert d.push("read", {"f": "a"}, "same") is None      # 3
    assert d.push("read", {"f": "a"}, "same") is not None   # 4 -> stuck


def test_stuck_distinct_args_not_flagged_even_with_same_observation():
    # A tool that returns the SAME observation for DIFFERENT args must NOT be flagged: the action
    # part of the pair differs, so this is legitimate progress (reading different files).
    d = StuckDetector(repeat_threshold=3)
    assert d.push("read", {"f": "a"}, "obs") is None
    assert d.push("read", {"f": "b"}, "obs") is None
    assert d.push("read", {"f": "c"}, "obs") is None
    assert d.push("read", {"f": "d"}, "obs") is None


def test_stuck_single_long_call_not_flagged():
    # One long-running command appears once -> never flagged (avoids OpenHands' early bug).
    d = StuckDetector(repeat_threshold=4)
    assert d.push("run", {"cmd": "train.py"}, "still running...") is None


def test_stuck_alternating_two_calls():
    d = StuckDetector(alternate_threshold=4)
    reason = None
    for i in range(8):                       # A B A B A B A B  (4 cycles)
        reason = d.push("read", {"f": "a" if i % 2 == 0 else "b"}, "x")
    assert reason is not None and "alternating" in reason


def test_stuck_alternating_not_flagged_when_observations_evolve():
    # A legitimate two-step poll (same two calls, same args) whose OBSERVATIONS change is progress.
    d = StuckDetector(alternate_threshold=4)
    reason = None
    for i in range(8):
        reason = d.push("status" if i % 2 == 0 else "wait", {"job": "x"}, f"state-{i}")
    assert reason is None


def test_stuck_alternating_flagged_when_pairs_repeat():
    d = StuckDetector(alternate_threshold=4)
    reason = None
    for i in range(8):                       # A=>x, B=>y repeating: both calls AND results repeat
        reason = d.push("a" if i % 2 == 0 else "b", {}, "x" if i % 2 == 0 else "y")
    assert reason is not None and "alternating" in reason


def test_stuck_disabled_is_noop():
    d = StuckDetector(enabled=False, repeat_threshold=2)
    assert d.push("read", {"f": "a"}, "x") is None
    assert d.push("read", {"f": "a"}, "x") is None
    assert d.push("read", {"f": "a"}, "x") is None


# --------------------------------------------------------------------------- loop integration
_EMIT = {"type": "function", "function": {
    "name": "emit", "description": "final", "parameters": {"type": "object", "properties": {}}}}


class _Tools:
    def specs(self):
        return [{"type": "function", "function": {
            "name": "peek", "description": "", "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return "constant-observation"


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


class _RepeatClient:
    """Always returns the SAME tool call — a model genuinely stuck in a loop. No `complete_tool`,
    so `_force_emit` returns None and the loop must terminate via the stuck guard -> fallback."""
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools, tool_choice="auto"):
        self.calls += 1
        return _tool_call("peek", {"q": "x"})


def test_loop_terminates_on_no_progress():
    client = _RepeatClient()
    out = drive_tool_loop(client, _Tools(), [{"role": "user", "content": "go"}], _EMIT,
                          stuck_repeat=4,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out == ("fallback", None)         # stopped gracefully instead of looping forever
    assert client.calls <= 6                 # bounded: ~repeat_threshold turns, not unbounded


class _ScriptClient:
    """Scripts assistant messages; records the messages AND tool specs seen each turn."""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []
        self.tool_names = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append([dict(m) for m in messages])
        self.tool_names.append({t["function"]["name"] for t in tools})
        return self.scripted.pop(0)


def test_loop_still_emits_normally_with_stuck_on():
    client = _ScriptClient([
        _tool_call("peek", {"q": "a"}),
        _tool_call("emit", {"ok": True}),
    ])
    out = drive_tool_loop(client, _Tools(), [{"role": "user", "content": "go"}], _EMIT,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out == ("emit", {"ok": True})


# --------------------------------------------------------------------------- self-plan (C1)
def test_self_plan_tool_exposed_stored_and_reinjected():
    client = _ScriptClient([
        _tool_call("update_plan", {"plan": "do X", "todos": [{"item": "step a", "status": "pending"}]}),
        _tool_call("peek", {"q": "a"}),       # turn 1 (distinct -> no stuck)
        _tool_call("peek", {"q": "b"}),       # turn 2 -> reinjection fires (every=2)
        _tool_call("emit", {"ok": True}),
    ])
    out = drive_tool_loop(client, _Tools(), [{"role": "user", "content": "go"}], _EMIT,
                          self_plan=True, plan_reinject_every=2,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out == ("emit", {"ok": True})
    # the update_plan tool is offered to the model
    assert "update_plan" in client.tool_names[0]
    # at turn_idx=2 the current plan is re-injected as a reminder. It's a USER-role message (not
    # `system`): the plan is verbatim model output, so re-injecting it with system authority would let
    # content the model was steered into by injected tool output re-issue itself as a privileged rule.
    reinjected = any("do X" in str(m.get("content")) and m.get("role") == "user"
                     for m in client.turns[2])
    assert reinjected


# --------------------------------------------------------------------------- auto-summary (C2)
def test_compact_history_summarizes_stale_middle():
    msgs = [{"role": "system", "content": "task"},
            {"role": "assistant", "content": "A" * 400},
            {"role": "tool", "content": "B" * 400},
            {"role": "assistant", "content": "C" * 400},
            {"role": "user", "content": "recent"}]
    out = compact_history(msgs, max_chars=100, summarize=lambda _t: "SHORT SUMMARY", keep_last=2)
    assert out[0]["content"] == "task"                       # system kept
    assert any("SHORT SUMMARY" in str(m.get("content")) for m in out)
    assert out[-1]["content"] == "recent"                   # last turn kept verbatim
    assert not any("A" * 400 == m.get("content") for m in out)   # stale middle gone


def test_compact_history_falls_back_to_truncation_on_summarizer_error():
    def _boom(_t):
        raise RuntimeError("summarizer down")
    msgs = [{"role": "system", "content": "task"},
            {"role": "assistant", "content": "A" * 400},
            {"role": "tool", "content": "B" * 400},
            {"role": "user", "content": "recent"}]
    out = compact_history(msgs, max_chars=100, summarize=_boom, keep_last=1)
    assert not any("Summary of earlier steps" in str(m.get("content")) for m in out)
    assert out[0]["content"] == "task"


def test_compact_history_does_not_orphan_tool_message():
    # assistant(tool_calls) + tool reply pairs. Summarizing the middle must not leave the kept tail
    # starting on a role:tool whose owning assistant was summarized away (endpoints reject that).
    def _asst(cid, pad):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": cid, "function": {"name": "read", "arguments": "{}"}}],
                "_pad": pad}
    msgs = [{"role": "system", "content": "S" * 200},
            {"role": "user", "content": "go"},
            _asst("a1", "A" * 300), {"role": "tool", "tool_call_id": "a1", "content": "R1" * 150},
            _asst("a2", "B" * 300), {"role": "tool", "tool_call_id": "a2", "content": "R2" * 150},
            _asst("a3", "C" * 300), {"role": "tool", "tool_call_id": "a3", "content": "R3" * 150}]
    out = compact_history(msgs, max_chars=300, summarize=lambda _t: "SUMMARY", keep_last=3)
    for i, m in enumerate(out):                 # every kept tool reply must follow its assistant(tc)
        if m.get("role") == "tool":
            prev = out[i - 1] if i > 0 else {}
            assert prev.get("tool_calls"), f"orphaned tool at index {i} (prev role={prev.get('role')})"


def test_compact_history_noop_under_budget():
    msgs = [{"role": "system", "content": "task"}, {"role": "user", "content": "hi"}]
    assert compact_history(msgs, max_chars=10_000, summarize=lambda _t: "x") is msgs


# --------------------------------------------------------------------------- settings wiring
def test_loop_opts_from_settings_defaults():
    class _S:
        pass
    opts = loop_opts_from_settings(_S())     # bare object -> getattr fallbacks (match config defaults)
    assert opts["stuck_detection"] is True
    assert opts["stuck_repeat"] == 4
    assert opts["self_plan"] is True
    assert opts["auto_summary"] is True


def test_config_enables_plan_and_summary_by_default():
    from looplab.core.config import Settings
    s = Settings()
    assert s.agent_self_plan is True
    assert s.agent_auto_summary is True
    assert s.agent_stuck_detection is True


def test_agentic_researcher_no_context_budget_kwarg_collision(monkeypatch):
    # loop_opts_from_settings injects context_budget_chars (Settings default 1_000_000, so ALWAYS
    # present). ToolUsingResearcher.propose must not ALSO pass it explicitly, or run_phase() gets the
    # keyword twice -> TypeError, caught by the broad except -> silent fallback: the agentic Researcher
    # would be DEAD in the DEFAULT config. Guard both wiring paths (also ToolUsingStrategist.decide).
    from looplab.core.config import Settings
    from looplab.core.models import Idea, RunState
    from looplab.agents import agent as agent_mod
    from looplab.agents.agent import ToolUsingResearcher, loop_opts_from_settings

    seen = {}

    def _fake_run_phase(*a, **kw):
        seen["cb"] = kw.get("context_budget_chars", "MISSING")
        return Idea(operator="draft", rationale="REACHED")

    monkeypatch.setattr(agent_mod, "run_phase", _fake_run_phase)
    r = ToolUsingResearcher(client=object(), tools=None,
                            context_budget_chars=Settings().context_budget_chars,
                            loop_opts=loop_opts_from_settings(Settings()))
    r._fallback = lambda m: Idea(operator="draft", rationale="FALLBACK")
    out = r.propose(RunState(goal="g", direction="min"), None)
    assert out.rationale == "REACHED"       # reached run_phase, not the TypeError -> fallback
    assert seen["cb"] == 1_000_000          # budget passed through exactly once


# --------------------------------------------------------------- P3: every tool call always executes
# The G2 read-dedup cache (stub an exact repeated read) was REMOVED by explicit operator decision
# ("always read what is asked" — docs/PROMPT_REVIEW.md P3): the stub pointed at content the model
# could no longer see, and the cached copy could go stale. The StuckDetector is the loop-safety net.
class _RepeatThenEmitClient:
    """Issues the SAME read call `repeats` times (identical re-reads), then emits."""
    def __init__(self, repeats):
        self.repeats = repeats
        self.calls = 0

    def chat(self, messages, tools, tool_choice="auto"):
        self.calls += 1
        if self.calls <= self.repeats:
            return _tool_call("read_file", {"path": "a.py"})
        return {"content": "", "tool_calls": [{"id": "e", "function": {"name": "emit", "arguments": "{}"}}]}


class _CountingReadTools:
    def __init__(self, content="file contents"):
        self.executed = 0
        self.content = content

    def specs(self):
        return [{"type": "function", "function": {
            "name": "read_file", "description": "", "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        self.executed += 1
        return self.content


def test_repeated_identical_reads_always_execute_and_return_full_content():
    # 2 identical read_file calls, then emit (below the stuck threshold of 4, so termination is via
    # emit). BOTH must execute for real and BOTH tool messages must carry the full content — no
    # "(already ran …)" stub, no cache.
    client = _RepeatThenEmitClient(repeats=2)
    tools = _CountingReadTools()
    messages = [{"role": "user", "content": "go"}]
    out = drive_tool_loop(client, tools, messages, _EMIT,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out[0] == "emit"
    assert tools.executed == 2                # every read ran — repeats are never suppressed
    reads = [m for m in messages if m.get("role") == "tool"]
    assert [m["content"] for m in reads] == ["file contents", "file contents"]
    assert not any("already ran" in m["content"] for m in reads)


def test_genuinely_stuck_repeats_still_trip_the_stuck_guard():
    # With the dedup gone, the StuckDetector is the only anti-thrash net: a model that re-issues the
    # SAME read (same args, same result) forever must still terminate via the stuck guard.
    class _Forever:
        calls = 0
        def chat(self, messages, tools, tool_choice="auto"):
            self.calls += 1
            return _tool_call("read_file", {"path": "a.py"})
    client = _Forever()
    out = drive_tool_loop(client, _CountingReadTools(), [{"role": "user", "content": "go"}], _EMIT,
                          stuck_repeat=4,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out == ("fallback", None)
    assert client.calls <= 6                  # bounded by the detector, not by turn budget


# ------------------------------------------------------------------ P3: explicit truncation marker
def test_oversized_tool_result_gets_truncation_marker():
    # A result over the 4000-char cap must arrive truncated WITH an explicit marker (so the model
    # knows the reply is partial and can re-request a narrower range), and the provenance hook must
    # receive exactly the same capped string as the tool message.
    client = _RepeatThenEmitClient(repeats=1)
    tools = _CountingReadTools(content="X" * 9000)
    messages = [{"role": "user", "content": "go"}]
    seen: list = []
    out = drive_tool_loop(client, tools, messages, _EMIT,
                          on_tool_result=lambda n, a, r: seen.append(r),
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out[0] == "emit"
    got = next(m["content"] for m in messages if m.get("role") == "tool")
    assert len(got) <= 4000
    assert got.startswith("X" * 100)                       # the head survives
    assert "truncated by the tool-result cap" in got and got.endswith("re-request a narrower range]")
    omitted = int(got.split("— ")[1].split(" chars")[0])
    assert omitted == 9000 - got.index("\n…[")             # exact count of characters cut
    assert seen == [got]                                   # hook got the marked string verbatim


def test_result_at_or_under_cap_is_untouched():
    client = _RepeatThenEmitClient(repeats=1)
    tools = _CountingReadTools(content="Y" * 4000)         # exactly at the cap
    messages = [{"role": "user", "content": "go"}]
    drive_tool_loop(client, tools, messages, _EMIT,
                    finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    got = next(m["content"] for m in messages if m.get("role") == "tool")
    assert got == "Y" * 4000                               # no marker when nothing was cut


class _ScriptToolClient:
    """Plays a scripted list of tool calls, then emits."""
    def __init__(self, script):
        self.script = [_tool_call(n, a) for n, a in script] + [
            {"content": "", "tool_calls": [{"id": "e", "function": {"name": "emit", "arguments": "{}"}}]}]

    def chat(self, messages, tools, tool_choice="auto"):
        return self.script.pop(0)


class _CountingNamedTools:
    """Counts executions per tool name; every listed name is offered as a spec."""
    def __init__(self, *names):
        self.names, self.counts = names, {}

    def specs(self):
        return [{"type": "function", "function": {
            "name": n, "description": "", "parameters": {"type": "object", "properties": {}}}}
            for n in self.names]

    def execute(self, name, args):
        self.counts[name] = self.counts.get(name, 0) + 1
        return f"{name}-result-{self.counts[name]}"


def test_cursor_polls_and_mixed_tools_all_execute():
    # read_output is a CURSOR poll ("new output since your last read") — identical args every call by
    # design. With every tool call executing unconditionally (P3), polls, writes and misnamed
    # mutators all just run; nothing is stubbed or cache-invalidated.
    client = _ScriptToolClient([("read_output", {"task_id": "t1"})] * 3)
    tools = _CountingNamedTools("read_output")
    out = drive_tool_loop(client, tools, [{"role": "user", "content": "go"}], _EMIT,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out[0] == "emit"
    assert tools.counts["read_output"] == 3


def test_context_budget_zero_means_off(monkeypatch):
    # The documented "0 = off": compaction must not run AT ALL (the old `or DEFAULT` fallback turned
    # an explicit 0 into the 120k default — MORE aggressive than configured, mega-review L6).
    import looplab.core.context_budget as cb

    def boom(*_a, **_k):
        raise AssertionError("compaction ran with context_budget_chars=0")

    monkeypatch.setattr(cb, "compact_history", boom)
    monkeypatch.setattr(cb, "truncate_history", boom)
    client = _ScriptToolClient([("read_file", {"path": "a.py"})])
    tools = _CountingNamedTools("read_file")
    out = drive_tool_loop(client, tools, [{"role": "user", "content": "go"}], _EMIT,
                          auto_summary=True, context_budget_chars=0,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None))
    assert out[0] == "emit"


def test_loop_opts_plumb_context_budget():
    # The configured budget must ride loop_opts_from_settings to EVERY loop (the Developer's implement
    # session was still compacting at the 120k built-in — mega-review L5); a bare settings object
    # without the field keeps the loop's own default (key absent -> None -> built-in).
    class _S:
        context_budget_chars = 123_456
    assert loop_opts_from_settings(_S())["context_budget_chars"] == 123_456

    class _Bare:
        pass
    assert "context_budget_chars" not in loop_opts_from_settings(_Bare())
