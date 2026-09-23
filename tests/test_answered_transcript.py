"""A transcript RE-SENT to a provider carries no unanswered tool call (review 2026-09-22, TAT-09).

`drive_tool_loop` leaves the transcript inconsistent on purpose when it accepts an emit: it returns
at once, so the emit's own `tool_call_id` — and any sibling call listed after it in the same assistant
turn — never receives its `role: "tool"` answer (paying for tool calls whose result nobody will read
would be worse). Its comment states the consequence as a CALLER OBLIGATION: anything that re-sends that
transcript must first drop the unanswered ids, or a strict OpenAI-compatible endpoint answers HTTP 400
("an assistant message with 'tool_calls' must be followed by tool messages responding to each
'tool_call_id'"). `serve/assistant.py` met the obligation inline; `agentic_struct._final` did not — a
malformed emit fell back to `parse_structured(client, messages, ...)` over the very transcript that
still carried the dangling emit id, so on a strict endpoint the fallback that exists to rescue a bad
emit failed too (and `agentic_struct`'s outer `except` then re-sent the same transcript a second time).

`tool_loop.answered_transcript` is now the ONE spelling of the obligation, used by both.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from looplab.agents.tool_loop import agentic_struct, agentic_text, answered_transcript
from looplab.core.errors import LLMError


def _dangling(messages) -> list[str]:
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    return [c.get("id") for m in messages for c in (m.get("tool_calls") or [])
            if c.get("id") not in answered]


class _Tools:
    def specs(self):
        return [{"type": "function", "function": {
            "name": "look", "description": "Look.", "parameters": {"type": "object",
                                                                   "properties": {}}}}]

    def execute(self, name, args):
        return "looked"


class _M(BaseModel):
    x: int


def _call(cid: str, name: str, args: dict) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class _StrictEndpoint:
    """An OpenAI-compatible endpoint that refuses a transcript with an unanswered tool call, the way
    strict servers do — and otherwise answers like a model that emitted a malformed first draft.

    `turn` is what `chat` answers: by default ONE assistant turn whose emit carries an argument that
    does not validate (`x` is not an int), which is what sends `agentic_struct` down its fallback."""

    def __init__(self, turn=None):
        self.turn = turn or [_call("call_emit", "emit", {"x": "not-an-int"})]
        self.requests: list[tuple[str, list[str]]] = []

    def _admit(self, kind, messages):
        dangling = _dangling(messages)
        self.requests.append((kind, dangling))
        if dangling:
            raise LLMError(f"HTTP 400: tool_call_ids {dangling} did not have response messages")

    def chat(self, messages, tools, tool_choice="auto"):
        self._admit("chat", messages)
        return {"content": "", "tool_calls": list(self.turn)}

    def complete_tool(self, messages, schema):
        self._admit("complete_tool", messages)
        return {"x": 3}

    def complete_text(self, messages):
        self._admit("complete_text", messages)
        return '{"x": 3}'


def _opening():
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_a_malformed_emit_falls_back_over_an_answered_transcript():
    """THE DEFECT, driven end to end through the real loop against a strict endpoint. Before the fix
    the fallback's first request carried `call_emit` unanswered, the endpoint refused it, and the
    whole call raised instead of returning the rescued answer."""
    endpoint = _StrictEndpoint()
    out = agentic_struct(endpoint, _Tools(), _opening(), _M)
    assert out == _M(x=3)
    assert all(not dangling for _kind, dangling in endpoint.requests), endpoint.requests
    # The rescue is ONE structured ask — not a refused one followed by the outer `except`'s retry.
    assert [kind for kind, _ in endpoint.requests] == ["chat", "complete_tool"]


@pytest.mark.parametrize("turn", [
    [_call("c_look", "look", {}), _call("c_emit", "emit", {"x": "nope"})],   # sibling answered
    [_call("c_emit", "emit", {"x": "nope"}), _call("c_look", "look", {})],   # sibling after emit
], ids=["sibling-before-emit", "sibling-after-emit"])
def test_every_unanswered_sibling_of_the_emit_is_closed_too(turn):
    """The loop returns AT the emit, so a call listed after it in the same turn never ran either."""
    endpoint = _StrictEndpoint(turn)
    assert agentic_struct(endpoint, _Tools(), _opening(), _M) == _M(x=3)
    assert all(not dangling for _kind, dangling in endpoint.requests), endpoint.requests


class _RaisingTools(_Tools):
    def execute(self, name, args):
        raise RuntimeError("the tool itself blew up")


@pytest.mark.parametrize("wrapper", ["struct", "text"])
def test_a_loop_that_raised_mid_turn_re_sends_an_answered_transcript(wrapper):
    """The wrappers' OTHER re-send: `except Exception: return fb(messages)`. A tool that raises
    escapes the loop AFTER the assistant turn naming it was appended and BEFORE its answer was, so
    the same obligation applies there."""
    endpoint = _StrictEndpoint([_call("c_look", "look", {})])
    if wrapper == "struct":
        assert agentic_struct(endpoint, _RaisingTools(), _opening(), _M) == _M(x=3)
    else:
        assert agentic_text(endpoint, _RaisingTools(), _opening()) == '{"x": 3}'
    assert all(not dangling for _kind, dangling in endpoint.requests), endpoint.requests


# ------------------------------------------------------------------ the helper's truth table

def test_the_helper_drops_only_what_nobody_answered():
    convo = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "assistant", "content": "thinking out loud", "tool_calls": [{"id": "c"}]},
        {"role": "assistant", "content": "  ", "tool_calls": [{"id": "d"}]},
    ]
    out = answered_transcript(convo)
    assert out[:2] == convo[:2]
    assert out[2] == {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]}
    assert out[3] is convo[3]
    # A turn whose every call is unanswered keeps its prose without `tool_calls`, and is dropped
    # entirely when it has none — an empty assistant turn is its own 400 on some servers.
    assert out[4] == {"role": "assistant", "content": "thinking out loud"}
    assert len(out) == 5
    assert _dangling(out) == []
    # The input is never edited in place: the caller still holds the loop's own record.
    assert convo[2]["tool_calls"] == [{"id": "a"}, {"id": "b"}] and len(convo) == 6


def test_an_answered_transcript_is_the_same_messages():
    convo = [{"role": "user", "content": "u"},
             {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
             {"role": "tool", "tool_call_id": "a", "content": "ra"}]
    out = answered_transcript(convo)
    assert out == convo and all(o is c for o, c in zip(out, convo))


def test_the_assistant_streams_its_answer_over_the_same_rule(tmp_path):
    """The assistant met the obligation inline before the helper existed; it now calls the helper, and
    the streamed final answer must still never carry the id its loop returned on."""
    from looplab.serve.assistant import run_turn

    class _StreamingStrict(_StrictEndpoint):
        def __init__(self):
            super().__init__()
            self.turns = [
                {"content": "", "tool_calls": [
                    _call("c_list", "list_runs", {}),
                    _call("c_final", "final_answer", {"reply": "loop reply"})]},
            ]

        def chat(self, messages, tools, tool_choice="auto"):
            self._admit("chat", messages)
            return self.turns.pop(0)

        def complete_text_stream(self, messages):
            self._admit("stream", messages)
            yield "streamed reply"

    endpoint = _StreamingStrict()
    got = []
    res = run_turn(endpoint, tmp_path, [], "hi", "plan", reply_sink=got.append)
    assert "".join(got) == "streamed reply" and res["reply"] == "streamed reply"
    assert ("stream", []) in endpoint.requests
