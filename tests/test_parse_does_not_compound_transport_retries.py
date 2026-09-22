"""A structured ask never re-asks through a second parser what the TRANSPORT already gave up on
(review 2026-09-22, CORE-04).

`core/parse.py::_walk_parsers` caught every `LLMError` as "this parser failed" and moved on to the
text parser. But an `LLMError` from the shipped client is raised only AFTER its own retry ladder
(`OpenAICompatibleClient._RETRY_POLICY`, 1 + `max_retries` attempts with backoff) has run out — so
for one transient failure the walk bought the whole ladder TWICE (measured: 18 provider attempts for
one structured ask with `max_retries=8`), sent a request that had just been refused for a bad key a
second time (a 401, twice), and re-asked after the caller's cancel token had fired. The ParseError it
finally raised dropped the cause (`raise ParseError(...)` with no `from`).

Now the walk re-raises what a second parser cannot fix — a cancel, and a failure
`classify_llm_failure` calls `unreachable` / `throttled` / `overloaded` / `credential` — and falls
back only for `model` / `protocol` failures (the endpoint answered and refused the TOOL request, e.g.
a 400 "tools are not supported", which the text parser exists for).
"""
from __future__ import annotations

import httpx
import openai
import pytest
from pydantic import BaseModel

from looplab.core import llm, llm_transient
from looplab.core.errors import LLMCancelled, LLMError
from looplab.core.parse import ParseError, parse_structured


class _M(BaseModel):
    x: int


_REQ = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
_ASK = [{"role": "user", "content": "hi"}]


def _counting_client(monkeypatch, failure):
    """The REAL client with its SDK call replaced by one that always fails with `failure`."""
    monkeypatch.setattr(llm_transient.time, "sleep", lambda _s: None)     # no real backoff
    client = llm.OpenAICompatibleClient(model="m", base_url="http://127.0.0.1:9/v1", api_key="k",
                                        max_retries=8)
    calls = [0]

    def _sdk_chat(payload, use_stream):
        calls[0] += 1
        raise failure()

    client._sdk_chat = _sdk_chat
    return client, calls


def _connection_reset():
    return openai.APIConnectionError(message="reset", request=_REQ)


def _unauthorized():
    return openai.AuthenticationError("bad key", response=httpx.Response(401, request=_REQ),
                                      body=None)


def _tools_unsupported():
    return openai.BadRequestError("tools are not supported by this model",
                                  response=httpx.Response(400, request=_REQ), body=None)


def test_a_transient_failure_buys_the_transport_ladder_once(monkeypatch):
    client, calls = _counting_client(monkeypatch, _connection_reset)
    with pytest.raises(LLMError) as raised:
        parse_structured(client, _ASK, _M)
    assert calls[0] == 9, f"{calls[0]} provider attempts for ONE structured ask (1 + max_retries=8)"
    assert llm_transient.classify_llm_failure(raised.value) == "unreachable"


def test_a_refused_credential_is_sent_once(monkeypatch):
    client, calls = _counting_client(monkeypatch, _unauthorized)
    with pytest.raises(LLMError) as raised:
        parse_structured(client, _ASK, _M)
    assert calls[0] == 1, "a request refused for its key was sent again through the text parser"
    assert llm_transient.classify_llm_failure(raised.value) == "credential"


def test_a_refused_tool_request_still_falls_back_to_the_text_parser(monkeypatch):
    """The case the text parser EXISTS for: the endpoint is up and answered, it just will not do
    tool calls. One tool attempt, one text attempt — and the ParseError now carries its cause."""
    client, calls = _counting_client(monkeypatch, _tools_unsupported)
    with pytest.raises(ParseError) as raised:
        parse_structured(client, _ASK, _M)
    assert calls[0] == 2
    assert isinstance(raised.value.__cause__, LLMError)
    assert llm_transient.classify_llm_failure(raised.value) == "model"


def test_a_cancelled_ask_is_not_re_asked():
    class _Cancelled:
        text_calls = 0

        def complete_tool(self, messages, schema):
            raise LLMCancelled("cancelled by the caller before it was sent")

        def complete_text(self, messages):
            self.text_calls += 1
            return '{"x": 1}'

    client = _Cancelled()
    with pytest.raises(LLMCancelled):
        parse_structured(client, _ASK, _M)
    assert client.text_calls == 0, "a cancelled ask was answered through the text parser anyway"


def test_an_ordinary_parse_failure_still_raises_parse_error_with_its_cause():
    class _Garbage:
        def complete_tool(self, messages, schema):
            return {"x": "not a number at all"}

        def complete_text(self, messages):
            return "no json here"

    with pytest.raises(ParseError) as raised:
        parse_structured(_Garbage(), _ASK, _M)
    assert raised.value.__cause__ is not None


def test_the_researcher_degrades_after_one_ladder_not_four(monkeypatch):
    """`LLMResearcher.propose` retried a failed parse once with feedback, so an unreachable endpoint
    cost it TWO walks of TWO parsers of the whole ladder — 36 provider attempts for one fallback
    proposal. The walk re-raises the transport's failure now; the Researcher degrades on it at once,
    to the same sentinel fallback the engine's proposal-path breaker recognises."""
    from looplab.agents.roles import LLMResearcher, is_researcher_fallback, researcher_fallback_cause
    from looplab.core.models import RunState

    client, calls = _counting_client(monkeypatch, _connection_reset)
    idea = LLMResearcher(client, bounds={"x": (0.0, 1.0)}).propose(RunState(), None)
    assert calls[0] == 9
    assert is_researcher_fallback(idea)
    assert "reset" in researcher_fallback_cause(idea) or "failed" in researcher_fallback_cause(idea)
