"""Provider-aware reasoning/thinking request toggle (`llm.reasoning_body` + client injection).
Offline — pure function + a stubbed HTTP layer, no model needed."""
from __future__ import annotations

import json

import looplab.llm as llm
from looplab.llm import OpenAICompatibleClient, reasoning_body, _retry_after_seconds


def test_retry_after_parsing():
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds("") is None
    assert _retry_after_seconds("5") == 5.0          # integer seconds
    assert _retry_after_seconds("1.5") == 1.5        # fractional seconds (was dropped before)
    assert _retry_after_seconds("garbage") is None   # unparseable -> caller uses exp backoff
    # HTTP-date in the past -> clamped to 0, not negative
    assert _retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


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

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"choices": [{"message": {"content": "hi"}}], "usage": {}})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    c = OpenAICompatibleClient("qwen3-30b-a3b",
                               reasoning={"chat_template_kwargs": {"enable_thinking": True}})
    c.complete_text([{"role": "user", "content": "x"}])
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": True}
    # and a client with no reasoning sends no toggle (unchanged behavior)
    c2 = OpenAICompatibleClient("qwen3-30b-a3b")
    c2.complete_text([{"role": "user", "content": "x"}])
    assert "chat_template_kwargs" not in captured["body"]
