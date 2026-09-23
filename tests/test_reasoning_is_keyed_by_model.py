"""The reasoning toggle is shaped, and degraded, per MODEL — not per client (review 2026-09-22, CORE-06).

`core/llm.py::model_override` routes ONE request to another model on the same endpoint (the operator
x model router, doc 52 row 19), but the client's reasoning state was frozen at construction for the
client's OWN model: `self.reasoning` was `reasoning_body(<client model>)`, and `_reasoning_ok` was
one bool. So a routed arm got the other model's reasoning SHAPE (a qwen arm behind a non-qwen client
sent `reasoning_effort` instead of `chat_template_kwargs.enable_thinking`), and ONE arm's 400 on its
toggle switched reasoning off for EVERY model the client serves, for the rest of the run. A third
path poisoned the same flag: a 400 naming `guided_json` matched `_is_reasoning_reject`'s generic keys
("unrecognized", "does not support parameters", …), so a constrained-decoding rejection turned the
reasoning toggle off permanently and then re-sent the offending field anyway.

Driven through the real `_sdk_chat` (only the SDK call at `_bounded_create` is replaced), so what is
asserted is the request that would have gone on the wire.
"""
from __future__ import annotations

import copy

import httpx
import openai
import pytest

import looplab.core.llm as llm
from looplab.core.config import Settings
from looplab.core.errors import ConfigRefusal
from looplab.core.llm import LLMError, make_llm_client, model_override

_REQ = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
_OK = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class _Body:
    def __init__(self, body):
        self._body = body

    def model_dump(self):
        return copy.deepcopy(self._body)


def _reject(message: str) -> openai.BadRequestError:
    body = {"error": {"message": message}}
    return openai.BadRequestError("err", response=httpx.Response(400, request=_REQ, json=body),
                                  body=body)


def _client(monkeypatch, *, rejects=None, **settings):
    """A real Settings-built client whose SDK call is replaced by a recorder.

    `rejects(kwargs)` may return an exception to raise for that request instead of answering."""
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    client = make_llm_client(Settings(llm_base_url="http://127.0.0.1:9/v1", **settings),
                             stream=False)
    sent: list[dict] = []

    def create(kwargs, join_s, *, counted=False):
        sent.append(copy.deepcopy(kwargs))
        exc = rejects(kwargs) if rejects is not None else None
        if exc is not None:
            raise exc
        return _Body(_OK)

    client._bounded_create = create
    return client, sent


def _ask(client):
    return client.complete_text([{"role": "user", "content": "go"}])


def _shape(kwargs) -> dict:
    return kwargs.get("extra_body") or {}


def test_a_routed_request_carries_the_reasoning_shape_of_ITS_model(monkeypatch):
    """MUTATION: read `self.reasoning` instead of `_reasoning_for_call(model)` in `_sdk_chat` -> the
    qwen arm is sent `reasoning_effort`, the non-qwen client's shape."""
    client, sent = _client(monkeypatch, llm_model="gpt-strong", llm_reasoning="high")
    _ask(client)
    with model_override("qwen3-32b"):
        _ask(client)

    assert sent[0]["model"] == "gpt-strong" and _shape(sent[0]) == {"reasoning_effort": "high"}
    assert sent[1]["model"] == "qwen3-32b"
    assert _shape(sent[1]) == {"chat_template_kwargs": {"enable_thinking": True}}, (
        "the routed arm was sent the client's own model's reasoning shape")


def test_an_unrouted_client_sends_the_bytes_it_always_sent(monkeypatch):
    """The fix may change bytes only in the buggy case: no override (or an override naming the
    client's own model) is the client's frozen `reasoning` dict, exactly."""
    client, sent = _client(monkeypatch, llm_model="qwen3-32b", llm_reasoning="high")
    _ask(client)
    with model_override("qwen3-32b"):
        _ask(client)
    assert _shape(sent[0]) == _shape(sent[1]) == client.reasoning == {
        "chat_template_kwargs": {"enable_thinking": True}}


def test_one_arms_reasoning_400_disables_reasoning_for_THAT_model_only(monkeypatch):
    """The glm arm rejects `reasoning_effort` (a litellm UnsupportedParamsError); the deepseek model
    the client was built for accepts it. MUTATION: flip the client-wide `_reasoning_ok` on the arm's
    400 -> the base model's next request goes out without its reasoning."""
    def rejects(kwargs):
        if kwargs["model"] == "glm-arm" and "reasoning_effort" in _shape(kwargs):
            return _reject("litellm.UnsupportedParamsError: openai does not support parameters: "
                           "['reasoning_effort']")
        return None

    client, sent = _client(monkeypatch, rejects=rejects, llm_model="deepseek-strong",
                           llm_reasoning="high")
    with model_override("glm-arm"):
        assert _ask(client) == "ok"          # 400, then the same request without the toggle
    assert _ask(client) == "ok"              # the client's own model
    with model_override("glm-arm"):
        assert _ask(client) == "ok"          # the arm remembers its own rejection

    carried = [(s["model"], "reasoning_effort" in _shape(s)) for s in sent]
    assert carried == [("glm-arm", True), ("glm-arm", False), ("deepseek-strong", True),
                       ("glm-arm", False)], carried
    assert client._reasoning_ok is True, "the arm's rejection disabled the client's own model"


def test_the_client_models_own_400_still_disables_its_toggle_and_leaves_arms_alone(monkeypatch):
    """The historical ratchet is unchanged for the client's own model — and no longer reaches an
    arm that never rejected anything."""
    def rejects(kwargs):
        if kwargs["model"] == "glm-5.1" and "reasoning_effort" in _shape(kwargs):
            return _reject("litellm.UnsupportedParamsError: openai does not support parameters: "
                           "['reasoning_effort']")
        return None

    client, sent = _client(monkeypatch, rejects=rejects, llm_model="glm-5.1", llm_reasoning="high")
    _ask(client)
    assert client._reasoning_ok is False
    with model_override("deepseek-arm"):
        _ask(client)
    assert (sent[-1]["model"], _shape(sent[-1])) == ("deepseek-arm", {"reasoning_effort": "high"})


def test_a_guided_json_400_is_a_bad_request_and_leaves_reasoning_on(monkeypatch):
    """`guided_json` is the field the endpoint named; the reasoning toggle was not. MUTATION: drop
    the guided-json precedence in `_policy_bad_request` -> `_reasoning_ok` flips False and the
    SAME rejected field is re-sent."""
    def rejects(kwargs):
        if "guided_json" in _shape(kwargs):
            return _reject("Unrecognized request argument supplied: guided_json")
        return None

    client, sent = _client(monkeypatch, rejects=rejects, llm_model="deepseek-strong",
                           llm_reasoning="high", llm_guided_json=True)
    with pytest.raises(LLMError):
        client._post({"model": "deepseek-strong", "messages": [{"role": "user", "content": "go"}],
                      "temperature": 0.0, "guided_json": {"type": "object"}})
    assert client._reasoning_ok is True, "a guided_json rejection disabled the reasoning toggle"
    assert len(sent) == 1, "the request was re-sent with the rejected field still attached"
    _ask(client)
    assert _shape(sent[-1]) == {"reasoning_effort": "high"}


def test_a_response_format_400_is_not_blamed_on_reasoning_either(monkeypatch):
    """`llm_guided_json` sends `response_format` beside `guided_json`; a gateway that names that
    one ("does not support parameters: ['response_format']") matched the reasoning keys too."""
    client, _sent = _client(monkeypatch, llm_model="deepseek-strong", llm_reasoning="high")
    exc = _reject("litellm.UnsupportedParamsError: openai does not support parameters: "
                  "['response_format']")
    with pytest.raises(LLMError):
        client._retry_or_raise(exc, 0, use_stream=False)
    assert client._reasoning_ok is True


def test_a_reasoning_config_that_contradicts_itself_FOR_AN_ARM_is_refused_naming_the_arm(
        monkeypatch):
    """`reasoning_body` refuses a depth set in two places, and whether they clash depends on the
    MODEL (`style="auto"`): off on a non-qwen model sends nothing, on a qwen model it sends
    `chat_template_kwargs.enable_thinking` beside the extra's `reasoning.enabled`. A per-role
    client built for that qwen model is refused at construction; an arm reaching it through the
    override is refused at its first request, before anything is sent, and the refusal names it."""
    client, sent = _client(monkeypatch, llm_model="gpt-strong", llm_reasoning="off",
                           llm_reasoning_extra={"reasoning": {"enabled": False}})
    _ask(client)
    assert _shape(sent[0]) == {"reasoning": {"enabled": False}}
    with model_override("qwen3-32b"):
        with pytest.raises(ConfigRefusal, match="qwen3-32b"):
            _ask(client)
    assert len(sent) == 1, "the contradictory request was sent"
