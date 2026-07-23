"""Provider-aware reasoning/thinking request toggle (`llm.reasoning_body` + client injection).
Offline — pure function + a stubbed HTTP layer, no model needed."""
from __future__ import annotations

import json

import looplab.core.llm as llm
from looplab.core.llm import OpenAICompatibleClient, reasoning_body, _retry_after_seconds


def test_retry_after_parsing():
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds("") is None
    assert _retry_after_seconds("5") == 5.0          # integer seconds
    assert _retry_after_seconds("1.5") == 1.5        # fractional seconds (was dropped before)
    assert _retry_after_seconds("garbage") is None   # unparseable -> caller uses exp backoff
    # HTTP-date in the past -> clamped to 0, not negative
    assert _retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_retry_after_zero_falls_back_to_backoff_not_instant(monkeypatch):
    # Regression: a `Retry-After: 0` / negative / past HTTP-date clamps to ra==0.0, which `if ra is not
    # None` honored as sleep(0) — every 429/5xx retry then fired in milliseconds, defeating the backoff
    # (a transient rate-limit blip could dev-crash the run). A non-positive directive must use backoff.
    import httpx
    import openai

    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    resp = httpx.Response(429, headers={"retry-after": "0"},
                          request=httpx.Request("POST", "http://x/v1/chat/completions"))
    calls = {"n": 0}

    class _OK:
        def model_dump(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    def fake_create(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise openai.RateLimitError("rate limited", response=resp, body=None)
        return _OK()

    c = OpenAICompatibleClient("qwen3-30b-a3b", stream=False)
    monkeypatch.setattr(c._sdk.chat.completions, "create", fake_create)
    c.complete_text([{"role": "user", "content": "x"}])
    assert calls["n"] == 2                             # retried once after the 429, then succeeded
    assert slept and slept[0] > 0                      # exp backoff (2.0s), NOT the sleep(0) the bug caused


def test_reasoning_body_unset_is_empty():
    assert reasoning_body("qwen3-coder", "") == {}
    assert reasoning_body("gpt-5", "") == {}


def test_reasoning_body_qwen_enable_disable():
    assert reasoning_body("qwen3-30b-a3b", "on") == {"chat_template_kwargs": {"enable_thinking": True}}
    assert reasoning_body("qwen3-30b-a3b", "off") == {"chat_template_kwargs": {"enable_thinking": False}}
    # qwen style is on/off only — an effort level still maps to enable_thinking=True
    assert reasoning_body("qwen3", "high") == {"chat_template_kwargs": {"enable_thinking": True}}


def test_reasoning_body_effort_for_non_qwen():
    assert reasoning_body("gpt-5", "high") == {"reasoning_effort": "high"}
    assert reasoning_body("gpt-5", "on") == {"reasoning_effort": "medium"}
    # Disabling on an effort-style provider omits the field — OpenAI/OpenRouter reject
    # reasoning_effort="none" (only low|medium|high are valid), so "off" => server default.
    assert reasoning_body("o3", "off") == {}


def test_reasoning_body_forced_style_and_extra():
    # force effort style even on a qwen model
    assert reasoning_body("qwen3", "low", style="effort") == {"reasoning_effort": "low"}
    # escape hatch merged last (e.g. Anthropic-style thinking)
    out = reasoning_body("claude", "on", style="none", extra={"thinking": {"type": "enabled"}})
    assert out == {"thinking": {"type": "enabled"}}


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return json.dumps(self._b).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_injects_reasoning_into_request(monkeypatch):
    captured = {}

    class _FakeResp:
        def model_dump(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        return _FakeResp()

    # The reasoning toggle rides in the SDK's `extra_body` (non-standard provider params). stream=False
    # so _sdk_chat takes the single-call path and returns model_dump().
    c = OpenAICompatibleClient("qwen3-30b-a3b", stream=False,
                               reasoning={"chat_template_kwargs": {"enable_thinking": True}})
    monkeypatch.setattr(c._sdk.chat.completions, "create", fake_create)
    c.complete_text([{"role": "user", "content": "x"}])
    assert captured["kwargs"]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    # and a client with no reasoning sends no toggle (unchanged behavior)
    c2 = OpenAICompatibleClient("qwen3-30b-a3b", stream=False)
    monkeypatch.setattr(c2._sdk.chat.completions, "create", fake_create)
    c2.complete_text([{"role": "user", "content": "x"}])
    assert "extra_body" not in captured["kwargs"]
