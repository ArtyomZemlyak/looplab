"""A cut-short assistant turn must SAY it was cut short.

The operator's report: "the assistant hangs around 40 tool uses and then some tool use comes back as
the answer". The loop was behaving as designed — on budget exhaustion it salvages one forced emit
from everything it gathered, which rescues an answer instead of discarding the investigation. What it
never did was tell anyone, so a truncated answer arrived looking like a finished one.

`agent_time_budget_s` is UNSET by default, and `serve/assistant.py::run_turn` then applied a
five-minute FLOOR — so on a shared, slow endpoint this is the limit most long turns actually hit,
and the documented "0 = no cap" was unreachable for the chat. That floor is now the DEFAULT of the
chat's own `assistant_time_budget_s` (see `assistant.assistant_time_budget`), so "no cap" is
expressible without also unbounding every engine role.

And the half that was still missing: only two of the loop's FIVE non-emit exits reported anything.
The other three — stuck, prose-stall, convergence ceiling — fell straight to `fallback`, which for
the assistant returns the last thing the model said out loud. A long turn that started circling came
back as a bare interstitial narration with no notice at all, which is the operator's report verbatim.
"""
from __future__ import annotations

import time

from looplab.agents.loop_options import EXPLICIT_ONLY_LOOP_ARGS
from looplab.agents.tool_loop import _note_budget, drive_tool_loop


def test_the_observer_is_registered_as_an_explicit_only_callback():
    """The registry PARTITIONS the loop's keyword-only parameters. A callback reachable both ways
    raises a duplicate-keyword TypeError that the loop's own containment swallows — the exact defect
    `loop_options.py`'s docstring records, which left the agentic Researcher silently dead."""
    from looplab.agents.loop_options import LOOP_OPTION_FIELDS

    assert "on_budget" in EXPLICIT_ONLY_LOOP_ARGS
    assert "on_budget" not in LOOP_OPTION_FIELDS


def test_a_broken_observer_can_never_break_the_loop_it_observes():
    """This fires on the way to a salvage emit. A raising callback here would turn a rescued answer
    into a crash — the opposite of what the salvage exists for."""
    def boom(_payload):
        raise RuntimeError("observer exploded")

    _note_budget(boom, "time", turns=3, seconds=1.0)      # must not raise
    _note_budget(None, "time", turns=3, seconds=1.0)      # …and absent is fine


def test_the_observer_reports_which_budget_ran_out_and_how_far_it_got():
    seen = {}
    _note_budget(seen.update, "time", turns=41, seconds=302.4567)
    assert seen == {"kind": "time", "turns": 41, "seconds": 302.457}

    seen = {}
    _note_budget(seen.update, "turns", turns=30, seconds=12.0)
    assert seen["kind"] == "turns" and seen["turns"] == 30


class _Client:
    """A model that keeps calling a tool and never emits — the shape of a turn that runs out."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.calls += 1
        time.sleep(0.02)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": "look", "arguments": "{}"}}]}

    def complete_text(self, messages):
        return "text"


class _Tools:
    def specs(self):
        return [{"type": "function", "function": {"name": "look", "description": "look",
                                                  "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return "looked"


def test_a_turn_that_runs_out_of_wall_clock_notifies_before_it_salvages():
    seen = {}
    drive_tool_loop(
        _Client(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        time_budget_s=0.05, finalize=lambda args: "done",
        fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert seen.get("kind") == "time", "the caller must learn the wall clock is what stopped it"
    assert seen["turns"] >= 1 and seen["seconds"] >= 0.05


def test_an_ordinary_turn_reports_nothing():
    """Absent, not `{"kind": None}`: the envelope key is what tells a UI to show the notice at all."""
    class _Emitter(_Client):
        def chat(self, messages, tool_specs, tool_choice="auto"):
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "e1", "type": "function",
                 "function": {"name": "emit", "arguments": '{"reply": "hi"}'}}]}

    seen = {}
    result = drive_tool_loop(
        _Emitter(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        time_budget_s=30.0, finalize=lambda args: args.get("reply"),
        fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert result == "hi"
    assert seen == {}


def test_run_turn_puts_the_notice_in_both_the_envelope_and_the_answer():
    """Two surfaces read this differently — the chat renders the reply text, tooling reads the dict —
    so a notice in only one of them is invisible to half the callers.

    The notice TEXT moved out of `run_turn` into `assistant.cutoff_notice` when the watch service
    became a second caller that reports the same fact about a wake-up turn; the pins moved with it
    rather than being deleted, because what they hold is that both surfaces still get one."""
    import inspect

    from looplab.serve import assistant

    source = inspect.getsource(assistant.run_turn)
    assert "on_budget=budget_box.update" in source
    assert '"budget_exhausted": dict(budget_box)' in source
    assert "cutoff_notice(budget_box)" in source

    notice = inspect.getsource(assistant.cutoff_notice)
    assert "was cut" in notice and "not a finished investigation" in notice
    assert "assistant_time_budget_s" in notice, "the notice must name the knob that raises the limit"


class _StreamingFake:
    """A model that never emits, then streams a final answer — the ORDINARY assistant path."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.calls += 1
        time.sleep(0.03)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{self.calls}", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]}

    def complete_text_stream(self, messages):
        yield "Streamed answer."

    def complete_text(self, messages):
        return "Streamed answer."


def test_the_notice_survives_the_streamed_final_answer(tmp_path):
    """DRIVES THE TURN instead of pinning the source, because the source pin passed while the
    feature did not work: the streaming block REPLACES `reply` wholesale, so a notice appended
    before it was silently dropped on the ordinary path — sink present, not cancelled, trace intact.
    The envelope key survived; the sentence the operator reads did not."""
    from looplab.core.config import Settings
    from looplab.serve.assistant import run_turn

    result = run_turn(_StreamingFake(), tmp_path, [], "go", "plan",
                      settings=Settings(agent_time_budget_s=0.05),
                      reply_sink=lambda _chunk: None)

    assert result["ok"] is True
    assert "budget_exhausted" in result, "the envelope must still carry the machine-readable fact"
    assert "Streamed answer." in result["reply"], "the streamed answer must still be the answer"
    assert "was cut" in result["reply"], (
        "the notice must survive the streamed answer — this is the assertion the source pin could "
        "not make")
    assert "assistant_time_budget_s" in result["reply"]


def test_an_ordinary_streamed_turn_carries_no_notice(tmp_path):
    from looplab.core.config import Settings
    from looplab.serve.assistant import run_turn

    class _Emitter(_StreamingFake):
        def chat(self, messages, tool_specs, tool_choice="auto"):
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "e1", "type": "function",
                 "function": {"name": "final_answer", "arguments": '{"reply": "done"}'}}]}

    result = run_turn(_Emitter(), tmp_path, [], "go", "plan",
                      settings=Settings(agent_time_budget_s=60.0),
                      reply_sink=lambda _chunk: None)
    assert "budget_exhausted" not in result
    assert "was cut" not in result["reply"]


# --------------------------------------------------------------- the other three exits (F4)
class _Circles:
    """A model that repeats ONE call forever — the STUCK exit, and the operator's report verbatim.

    It also writes interstitial prose, which is what `fallback` hands back: before this was reported,
    a 300-second turn returned "Let me look at the next file." as the answer, with nothing anywhere
    saying the investigation had been cut off.
    """

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tool_specs, tool_choice="auto"):
        self.calls += 1
        return {"role": "assistant", "content": "Let me look at the next file.",
                "tool_calls": [{"id": f"c{self.calls}", "type": "function",
                                "function": {"name": "look", "arguments": "{}"}}]}

    def complete_text(self, messages):
        return "text"


def test_a_turn_stopped_for_going_in_circles_says_so_and_says_what_it_repeated():
    seen = {}
    drive_tool_loop(
        _Circles(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        finalize=lambda args: "done", fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert seen.get("kind") == "stuck"
    assert "look" in seen.get("detail", ""), "the operator must learn WHAT it repeated"


def test_a_turn_the_model_would_not_emit_from_says_so():
    class _OnlyProse:
        def chat(self, messages, tool_specs, tool_choice="auto"):
            return {"role": "assistant", "content": "I think the answer is probably fine.",
                    "tool_calls": []}

        def complete_text(self, messages):
            return "text"

    seen = {}
    drive_tool_loop(
        _OnlyProse(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        finalize=lambda args: "done", fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert seen.get("kind") == "stalled"


def test_a_turn_forced_to_answer_at_the_convergence_ceiling_says_so():
    class _Wanders:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tool_specs, tool_choice="auto"):
            self.calls += 1
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{self.calls}", "type": "function",
                 "function": {"name": "look", "arguments": f'{{"n": {self.calls}}}'}}]}

        def complete_text(self, messages):
            return "text"

    seen = {}
    drive_tool_loop(
        _Wanders(), _Tools(), [{"role": "user", "content": "go"}],
        {"type": "function", "function": {"name": "emit", "description": "emit",
         "parameters": {"type": "object", "properties": {"reply": {"type": "string"}}}}},
        emit_after=0, emit_force=3, stuck_detection=False,
        finalize=lambda args: "done", fallback=lambda msgs: "fell back", on_budget=seen.update)
    assert seen.get("kind") == "emit_force"
    assert seen["turns"] == 3


def test_every_reported_kind_is_in_the_closed_vocabulary_and_has_a_sentence():
    """A kind the loop can report but the chat has no words for is how this defect comes back: the
    fact reaches the envelope and the operator still reads a truncated answer with no explanation."""
    from looplab.agents.tool_loop import LOOP_CUTOFF_KINDS
    from looplab.serve.assistant import _CUTOFF_SENTENCES

    assert set(_CUTOFF_SENTENCES) == set(LOOP_CUTOFF_KINDS)
    for kind in LOOP_CUTOFF_KINDS:
        from looplab.serve.assistant import cutoff_notice
        text = cutoff_notice({"kind": kind, "turns": 7, "seconds": 1.5})
        assert "was cut short" in text and "7 tool turns" in text


def test_an_unknown_kind_still_produces_a_notice_rather_than_silence():
    from looplab.serve.assistant import cutoff_notice
    assert "was cut short" in cutoff_notice({"kind": "something_new", "turns": 1, "seconds": 1})
    assert cutoff_notice({}) == "", "an ordinary turn carries no notice at all"


def test_a_stuck_exit_does_not_send_the_operator_to_a_knob_that_cannot_help():
    from looplab.serve.assistant import cutoff_notice
    assert "assistant_time_budget_s" in cutoff_notice({"kind": "time", "turns": 1, "seconds": 1})
    assert "assistant_time_budget_s" not in cutoff_notice({"kind": "stuck", "turns": 1, "seconds": 1})


# --------------------------------------------------------------- the budget rule's truth table
def test_the_assistant_budget_rule():
    """The floor became a DEFAULT. `0` now genuinely means no cap for the chat — the spelling that
    did not exist before, and whose absence made the settings table's "0 = no cap" false here."""
    from looplab.core.config import Settings
    from looplab.serve.assistant import ASSISTANT_TIME_BUDGET_DEFAULT_S, assistant_time_budget

    assert assistant_time_budget(Settings()) == ASSISTANT_TIME_BUDGET_DEFAULT_S == 300.0
    assert assistant_time_budget(Settings(agent_time_budget_s=900.0)) == 900.0
    assert assistant_time_budget(Settings(assistant_time_budget_s=0.0)) == 0.0
    assert assistant_time_budget(Settings(assistant_time_budget_s=1800.0)) == 1800.0
    # The chat's own value WINS over the engine-wide one, in both directions.
    assert assistant_time_budget(
        Settings(assistant_time_budget_s=0.0, agent_time_budget_s=900.0)) == 0.0
    assert assistant_time_budget(
        Settings(assistant_time_budget_s=60.0, agent_time_budget_s=900.0)) == 60.0
    # No settings at all (a subagent built without one) still gets the guard, never "no cap".
    assert assistant_time_budget(None) == 300.0


def test_the_turn_actually_runs_unbounded_when_the_operator_says_so(tmp_path):
    """DRIVES it: a source pin cannot tell "0 means no cap" from "0 means 300"."""
    from looplab.core.config import Settings
    from looplab.serve import assistant as assistant_mod

    seen = {}
    real = assistant_mod.run_turn

    import looplab.agents.agent as agent_mod
    original = agent_mod.drive_tool_loop

    def _spy(client, tools, convo, emit_spec, **kw):
        seen["time_budget_s"] = kw.get("time_budget_s")
        return "ok"

    agent_mod.drive_tool_loop = _spy
    try:
        real(_Client(), tmp_path, [], "go", "plan",
             settings=Settings(assistant_time_budget_s=0.0))
        assert seen["time_budget_s"] == 0.0, "0 must reach the loop as 0 — the loop reads it as off"
        real(_Client(), tmp_path, [], "go", "plan", settings=Settings())
        assert seen["time_budget_s"] == 300.0
    finally:
        agent_mod.drive_tool_loop = original
