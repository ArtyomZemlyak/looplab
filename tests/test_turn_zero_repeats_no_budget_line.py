"""Guard: turn zero repeats no budget line, and a MOVED figure still lands.

Driven, not pinned: the loop is run against a stub client, and the property is read off the
message list the client was actually handed.
"""
import json
from looplab.agents import tool_loop


class _Client:
    def __init__(self, script):
        self.script, self.seen, self.model = list(script), [], "stub"

    def chat(self, messages, tool_specs=None, tool_choice=None, **kw):
        self.seen.append([dict(m) for m in messages])
        return self.script.pop(0)


def _emit(payload):
    return {"content": "", "tool_calls": [{"id": "1", "type": "function",
            "function": {"name": "emit", "arguments": json.dumps(payload)}}]}


def _noop_tool():
    return {"content": "", "tool_calls": [{"id": "0", "type": "function",
            "function": {"name": "ping", "arguments": "{}"}}]}


class _Tools:
    def specs(self):
        return [{"type": "function", "function": {"name": "ping", "description": "p",
                 "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return "pong"


_SPEC = {"type": "function", "function": {"name": "emit", "description": "e",
         "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}}


def _reminders(msgs):
    return [m["content"] for m in msgs
            if m.get("role") == "user" and str(m.get("content", "")).startswith("Reminder — ")]


def test_turn_zero_does_not_repeat_the_opening_budget_line():
    figure = ["BUDGET: $0.0000 of $1.0000 spent"]
    client = _Client([_emit({"x": "done"})])
    opener = {"role": "user", "content": figure[0] + "\ngo"}
    tool_loop.drive_tool_loop(client, _Tools(), [opener], _SPEC,
                              finalize=lambda a: a, budget_note=lambda: figure[0])
    assert _reminders(client.seen[0]) == [], (
        "the caller already leads its opening turn with this exact figure")


def test_a_figure_that_moves_still_lands():
    figure = ["BUDGET: $0.0000 of $1.0000 spent"]
    client = _Client([_noop_tool(), _emit({"x": "done"})])
    opener = {"role": "user", "content": figure[0] + "\ngo"}

    class _Moving:
        def __call__(self):
            note = figure[0]
            figure[0] = "BUDGET: $0.5000 of $1.0000 spent"
            return note
    tool_loop.drive_tool_loop(client, _Tools(), [opener], _SPEC,
                              finalize=lambda a: a, budget_note=_Moving())
    assert any("$0.5000" in r for r in _reminders(client.seen[-1])), (
        "the whole point of the callable is that the model hears the figure MOVE"
    )


def test_a_budget_note_that_raises_never_ends_the_session():
    client = _Client([_emit({"x": "done"})])

    def _boom():
        raise RuntimeError("no accountant")
    out = tool_loop.drive_tool_loop(client, _Tools(), [{"role": "user", "content": "go"}], _SPEC,
                                    finalize=lambda a: a, budget_note=_boom)
    assert out is not None
