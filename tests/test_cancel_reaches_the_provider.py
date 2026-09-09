"""doc 27 `cancel-not-propagated-into-provider-request`: a cancel token reaches the REQUEST.

Two of the three legs were open. The MCP transport polls its `cancel_check` (shipped 2026-08-17) and
`drive_tool_loop` reads one at every turn boundary — but nothing reached the provider call itself, so
a Stop pressed during a long generation still waited out the whole answer, then every remaining retry
attempt, then every backoff between them, before anyone looked at the token again. (The third leg,
the external CLI agent killed only on its TIMEOUT, is driven in `tests/test_cli_agent.py`.)

Everything here is driven against a FAKE PROVIDER — a stubbed `_sdk_chat`, a hand-rolled chunk
iterator, a client object with one method — so the property under test is "the in-flight request
observes the cancel", never "the source contains a call".
"""
from __future__ import annotations

import time

import pytest

from looplab.core.errors import LLMCancelled, LLMError
from looplab.core.llm import OpenAICompatibleClient, cancel_check_scope, request_cancelled


def _client(**kw) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(base_url="http://x/v1", api_key="k", model="m", **kw)


def _sdk_503():
    """A real SDK 5xx (the family `_policy_connection`/`_policy_throttled` retries with backoff)."""
    import httpx
    import openai
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(503, json={"error": {"message": "overloaded"}}, request=request)
    return openai.InternalServerError("boom", response=response, body={"message": "overloaded"})


# ------------------------------------------------------------------ the retry ladder and its sleeps

def test_a_cancel_while_the_request_is_in_flight_stops_the_next_attempt(monkeypatch):
    """THE MEASUREMENT THIS EXISTS FOR: with `max_retries=3` a cancelled call used to send four
    requests and sleep 2+4+8 s between them. The token fires while attempt ONE is in flight (the
    fake provider sets it, exactly as a user pressing Stop during a live generation does), so
    attempt two must never be sent and the backoff must not be waited out."""
    sent = []
    cancelled = []

    def _fake_sdk_chat(payload, use_stream):
        sent.append(use_stream)
        cancelled.append(True)          # the Stop lands while this request is in flight
        raise _sdk_503()

    client = _client(max_retries=3)
    monkeypatch.setattr(client, "_sdk_chat", _fake_sdk_chat)

    began = time.monotonic()
    with cancel_check_scope(lambda: bool(cancelled)):
        with pytest.raises(LLMCancelled):
            client._post({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7})
    elapsed = time.monotonic() - began

    assert len(sent) == 1, f"the cancelled call sent {len(sent)} requests"
    assert elapsed < 1.5, f"the backoff was waited out anyway ({elapsed:.2f}s)"


def test_a_context_that_is_already_cancelled_sends_nothing_at_all(monkeypatch):
    """The strong half of the property: once cancelled, no NEW provider request leaves the process
    while the scope is up — including one made by a fallback client the role layer reaches for after
    `except LLMError`, since it re-checks the same ambient token at its own first attempt."""
    sent = []
    client = _client(max_retries=3)
    monkeypatch.setattr(client, "_sdk_chat", lambda payload, use_stream: sent.append(1))

    with cancel_check_scope(lambda: True):
        with pytest.raises(LLMCancelled):
            client._post({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.9})
    assert sent == []


def test_without_a_token_the_ladder_is_byte_identical(monkeypatch):
    """A caller that installs no predicate pays nothing for the feature: same attempts, same sleeps,
    same `LLMError`. (The sleep seam is the one every backoff test patches.)"""
    sent, slept = [], []
    monkeypatch.setattr("looplab.core.llm.time.sleep", slept.append)
    client = _client(max_retries=2)

    def _always_503(payload, use_stream):
        sent.append(use_stream)
        raise _sdk_503()

    monkeypatch.setattr(client, "_sdk_chat", _always_503)
    with pytest.raises(LLMError) as raised:
        client._post({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.5})
    assert not isinstance(raised.value, LLMCancelled)
    assert len(sent) == 3 and slept == [2.0, 4.0]          # every attempt, every backoff


def test_a_broken_cancel_predicate_never_stops_a_paid_call(monkeypatch):
    """Guarded probe, same rule as `tool_loop._cancelled`: a predicate that raises answers False. A
    broken observer must not be able to kill a call that is about to succeed."""
    client = _client(max_retries=0)
    monkeypatch.setattr(client, "_sdk_chat", lambda payload, use_stream: {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}], "usage": {}})

    def _explodes():
        raise RuntimeError("the observer is broken")

    with cancel_check_scope(_explodes):
        assert request_cancelled() is False
        body = client._post({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.3})
    assert body["choices"][0]["message"]["content"] == "hello"


def test_a_cancelled_backoff_wakes_instead_of_sleeping_it_out():
    """`sleep_or_cancel` is what makes a Stop prompt rather than eventual: a `Retry-After` is honoured
    up to 120 s and our own backoff up to 30 s, all of it in one `time.sleep` before this existed."""
    from looplab.core.llm import sleep_or_cancel

    began = time.monotonic()
    with cancel_check_scope(lambda: True):
        with pytest.raises(LLMCancelled):
            sleep_or_cancel(30.0, "http://x/v1")
    assert time.monotonic() - began < 1.0


# ------------------------------------------------------------------------------ mid-stream cancel

class _Chunk:
    """The shape `_accumulate_stream` reads off an SDK stream event."""

    def __init__(self, text: str):
        delta = type("D", (), {"content": text, "reasoning": None, "tool_calls": None})()
        choice = type("C", (), {"delta": delta, "finish_reason": None})()
        self.choices = [choice]
        self.usage = None


def test_a_cancel_mid_stream_stops_reading_the_generation():
    """A long answer is where a Stop actually saves money, and the read loop is the only place it can
    be saved: closing the connection is what stops the provider generating. Raising (rather than
    returning the partial deltas) on purpose — a half-accumulated answer would look to `_post` like a
    keepalive stall and be RETRIED, which is the opposite of a cancel."""
    served = []

    def _chunks():
        for i in range(10):
            served.append(i)
            yield _Chunk(f"tok{i} ")

    stop_after = 3
    with cancel_check_scope(lambda: len(served) >= stop_after):
        with pytest.raises(LLMCancelled):
            OpenAICompatibleClient._accumulate_stream(_chunks())
    assert len(served) == stop_after, f"the reader consumed {len(served)} chunks past the cancel"


def test_an_uncancelled_stream_is_read_to_the_end():
    body = OpenAICompatibleClient._accumulate_stream(iter([_Chunk("a"), _Chunk("b")]))
    assert body["choices"][0]["message"]["content"] == "ab"


# ------------------------------------------------------------- the loop publishes the token it holds

def test_the_tool_loop_publishes_its_cancel_token_to_the_client():
    """`drive_tool_loop` held the only token and read it BETWEEN turns. It now scopes the same
    guarded probe around the paid call, so the client (and every retry and sleep below it) can see
    a Stop that lands while the request is in flight."""
    from looplab.agents.agent import drive_tool_loop

    seen, stopped = [], []

    class _FakeClient:
        def chat(self, messages, tools, tool_choice="auto"):
            stopped.append(1)           # the Stop lands WHILE this request is in flight
            # What a real client asks at the top of every attempt of every request it makes.
            seen.append(request_cancelled())
            return {"content": "", "tool_calls": [
                {"id": "z", "function": {"name": "emit", "arguments": "{}"}}]}

    emit = {"type": "function", "function": {"name": "emit", "parameters": {}}}
    drive_tool_loop(_FakeClient(), None, [{"role": "user", "content": "go"}], emit,
                    cancel_check=lambda: bool(stopped), finalize=lambda a: "emitted",
                    fallback=lambda m: m)
    assert seen == [True], "the client could not see the loop's cancel token"


def test_the_loop_leaves_no_token_behind_after_the_call():
    """Scoped to the call: nothing the loop does afterwards (a tool, another role's client) inherits
    a token that was only ever about this request."""
    from looplab.agents.agent import drive_tool_loop

    after = []

    class _FakeClient:
        def chat(self, messages, tools, tool_choice="auto"):
            return {"content": "", "tool_calls": [
                {"id": "z", "function": {"name": "emit", "arguments": "{}"}}]}

    emit = {"type": "function", "function": {"name": "emit", "parameters": {}}}
    drive_tool_loop(_FakeClient(), None, [{"role": "user", "content": "go"}], emit,
                    cancel_check=lambda: False,
                    finalize=lambda a: after.append(request_cancelled()),
                    fallback=lambda m: m)
    assert request_cancelled() is False
