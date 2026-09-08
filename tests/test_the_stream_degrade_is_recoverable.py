"""A transient stall must not spend the rest of a $1 run behind nginx's 300 s window.

The stall-degrade was permanent: two stream stalls and the client never streamed again "for this
client's lifetime". Its rationale was measured where non-streaming is the SAFE mode (glm-5.1:
non-stream 2 s, stream wedges). On this bench it is the DANGEROUS one — the gateway sits behind
`proxy_read_timeout 300`, which without SSE measures the whole generation, and 28 % of `discrete_log`
calls once died there at five minutes each.

Measured 2026-09-04 on `freeB3`: two empty streamed 200s at 11:39:38 and 11:40:36 (60 s, `att=2`,
zero tokens both ways) degraded the client, and the next 51 calls over 23 minutes went unstreamed
with no way back, its prompt growing past 34 k tokens and single answers reaching 107 s. `capB3`
lost a call at exactly 300 s at 12:01:40.

So the degrade now expires: after `STREAM_STALL_RETRY_AFTER` good unstreamed calls one attempt
probes streaming again. Works → the ratchet resets. Stalls → it re-arms for another run of calls.
"""
from __future__ import annotations

import looplab.core.llm as llm

_OK = {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
       "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
_KEEPALIVE_ONLY = {"choices": [{"message": {"role": "assistant", "content": ""}}],
                   "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def _client(monkeypatch, script):
    """A client whose `_sdk_chat` follows `script(use_stream, n)` -> body dict."""
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1", stream=True)
    seen = []

    def sdk_chat(payload, use_stream):
        seen.append(use_stream)
        return script(use_stream, len(seen))
    monkeypatch.setattr(c, "_sdk_chat", sdk_chat)
    return c, seen


def _ask(c):
    return c.complete_text([{"role": "user", "content": "go"}])


def test_two_stalls_still_degrade(monkeypatch):
    """The protection the ratchet exists for is unchanged: after two stalls, calls stop streaming."""
    c, seen = _client(monkeypatch, lambda st, n: _KEEPALIVE_ONLY if st else _OK)
    for _ in range(3):
        _ask(c)
    assert c._stream_stalls >= llm.STREAM_STALL_DEGRADE_AFTER
    assert seen[-1] is False, "still streaming after two stalls; the degrade stopped working"


def test_the_degrade_expires_and_streaming_is_probed_again(monkeypatch):
    """freeB3's shape: stall twice, then a long run of fine unstreamed calls. One of them must
    eventually try SSE again instead of running to the end of the $1 unstreamed."""
    state = {"stream_ok": False}

    def script(use_stream, n):
        if use_stream and not state["stream_ok"]:
            return _KEEPALIVE_ONLY
        return _OK
    c, seen = _client(monkeypatch, script)
    for _ in range(3):
        _ask(c)                                   # degrade
    state["stream_ok"] = True                     # the upstream hiccup passes
    n_before = len(seen)
    for _ in range(llm.STREAM_STALL_RETRY_AFTER + 2):
        _ask(c)
    after = seen[n_before:]
    assert any(after), (
        f"{len(after)} calls after the upstream recovered and not one probed streaming; the "
        "degrade is still permanent")
    assert c._stream_stalls == 0, (
        "the probe succeeded but the ratchet did not reset, so the client is still degraded")
    assert seen[-1] is True, "after a successful probe the client must be streaming again"


def test_the_probe_does_not_fire_every_call(monkeypatch):
    """A degrade that re-probes constantly is the un-degraded client with extra steps: each probe
    that stalls costs a whole stalled request."""
    c, seen = _client(monkeypatch, lambda st, n: _KEEPALIVE_ONLY if st else _OK)
    for _ in range(3):
        _ask(c)
    n_before = len(seen)
    for _ in range(llm.STREAM_STALL_RETRY_AFTER - 2):
        _ask(c)
    assert not any(seen[n_before:]), (
        f"streaming was probed after fewer than {llm.STREAM_STALL_RETRY_AFTER} good calls: "
        f"{seen[n_before:]}")


def test_a_probe_that_stalls_re_arms_the_degrade(monkeypatch):
    """If the endpoint is still wedged, the probe must buy another full run of unstreamed calls,
    not a probe on every subsequent call."""
    c, seen = _client(monkeypatch, lambda st, n: _KEEPALIVE_ONLY if st else _OK)
    for _ in range(3):
        _ask(c)
    for _ in range(llm.STREAM_STALL_RETRY_AFTER + 1):
        _ask(c)                                   # includes one failed probe
    n_before = len(seen)
    for _ in range(3):
        _ask(c)
    assert not any(seen[n_before:]), (
        f"after a failed probe the client probed again immediately: {seen[n_before:]}")
