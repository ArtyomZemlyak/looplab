"""I2: OpenAI-compatible client against a mock HTTP server (offline, deterministic).
Verifies tool-call parsing, the text/JSON fallback path, and <think> stripping."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from looplab.core.llm import (
    OpenAICompatibleClient,
    _apply_native_tool_calls,
    _extract_native_tool_calls,
    _tool_call_slot,
)
from looplab.core.models import Idea
from looplab.core.parse import parse_structured


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        if "tools" in body:
            args = json.dumps({"operator": "improve", "params": {"x": 3.0, "y": -1.0}, "rationale": "r"})
            resp = {"choices": [{"message": {"role": "assistant",
                    "tool_calls": [{"function": {"name": "emit", "arguments": args}}]}}], "usage": {}}
        else:
            # reasoning-model style: <think> with braces, then the JSON answer
            content = '<think>let me {consider} options</think> Here: {"operator": "draft", "params": {"x": 1.0}}'
            resp = {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def base_url():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()


def test_tool_call_path(base_url):
    # stream=False: the mock server returns a plain JSON body (not SSE), so exercise the non-stream
    # path directly instead of the SDK's stream-then-degrade dance on a non-SSE body.
    c = OpenAICompatibleClient("m", base_url=base_url, stream=False)
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "tool_call")
    assert idea.operator == "improve" and idea.params == {"x": 3.0, "y": -1.0}


def test_text_fallback_strips_think(base_url):
    c = OpenAICompatibleClient("m", base_url=base_url, stream=False)
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "baml")
    assert idea.operator == "draft" and idea.params == {"x": 1.0}


# --- transport resilience (openai SDK): built on a mocked `_sdk_chat` seam. The live path drives
# the openai SDK over httpx, so a stall/timeout/refused surfaces as an openai exception, not a
# urllib one; these assert the retry / reasoning-drop / fail-fast / stall-degrade POLICY around it.
import httpx as _httpx
import openai as _openai

_OK_BODY = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {}}
_EMPTY_STREAM = {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": None}],
                 "usage": {}}
_REQ = _httpx.Request("POST", "http://x/v1/chat/completions")


def _timeout_exc():
    return _openai.APITimeoutError(request=_REQ)


def _conn_exc(cause):
    e = _openai.APIConnectionError(message="connection error", request=_REQ)
    e.__cause__ = cause
    return e


def _status_exc(cls, code, body):
    return cls("err", response=_httpx.Response(code, request=_REQ, json=body), body=body)


def test_post_retries_transient_timeout(monkeypatch):
    """A momentary read/connect timeout (httpx -> openai.APITimeoutError) must be retried with backoff
    and then succeed — a single slow response must not abort a long unattended run."""
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _timeout_exc()
        return dict(_OK_BODY)

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3                                     # retried twice, succeeded on the 3rd


def test_post_drops_reasoning_on_unsupported_param_400(monkeypatch):
    """A litellm-proxied model (glm-5.1) returns 400 UnsupportedParamsError for `reasoning_effort`.
    The client must DROP the reasoning toggle and retry — so the model works — and remember it (deepseek
    keeps reasoning; glm-5.1 drops it). The seam sees `_reasoning_ok` flip off between attempts."""
    import looplab.core.llm as llm
    calls = {"n": 0, "reasoning_at_first": None}

    def fake(payload, use_stream):
        calls["n"] += 1
        if calls["n"] == 1:
            calls["reasoning_at_first"] = None   # first attempt still has _reasoning_ok True
            body = {"error": {"message": "litellm.UnsupportedParamsError: openai does not support "
                              "parameters: ['reasoning_effort']"}}
            raise _status_exc(_openai.BadRequestError, 400, body)
        return dict(_OK_BODY)

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("glm-5.1", base_url="http://x/v1",
                                   reasoning={"reasoning_effort": "high"})
    monkeypatch.setattr(c, "_sdk_chat", fake)
    assert c._reasoning_ok is True
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert c._reasoning_ok is False and calls["n"] == 2      # adapted — won't send reasoning again


def test_read_stream_watchdog_breaks_a_stalled_connection():
    """A streamed response that BLOCKS in recv() with no new tokens (a stalled generation, or a server
    trickling keepalive bytes without completing a line) must not hang forever: a watchdog SHUTS DOWN
    the underlying socket after `timeout` s — which actually interrupts the blocked recv (resp.close()
    alone does not) — so the read ends and _post retries. Regression for a ~70-min / ~15-min live hang.
    Uses a real socketpair so the socket.shutdown() path (not just the close() fallback) is exercised."""
    import socket
    import time as _t
    import looplab.core.llm as llm

    a, _b = socket.socketpair()                   # _b never sends -> a.recv blocks like a stalled server

    class _SockResp:
        def __init__(self, s):
            self.fp = type("FP", (), {"raw": type("R", (), {"_sock": s})()})()
            self._s = s

        def __iter__(self):
            return self

        def __next__(self):
            data = self._s.recv(100)              # blocks; watchdog shutdown() -> returns b'' (EOF)
            if not data:
                raise StopIteration
            return data

        def close(self):
            try:
                self._s.close()
            except OSError:
                pass

        def read(self):
            return b""

    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1", timeout=2)   # 2s idle limit
    t0 = _t.monotonic()
    with pytest.raises((OSError, TimeoutError)):
        c._read_stream(_SockResp(a))
    assert _t.monotonic() - t0 < 6                # fired near the 2s limit, did NOT hang


def test_post_raises_after_exhausting_timeouts(monkeypatch):
    """A persistently dead endpoint still surfaces a clean LLMError once retries are exhausted."""
    import looplab.core.llm as llm

    def fake(payload, use_stream):
        raise _timeout_exc()

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])


def test_post_fails_fast_on_connection_refused(monkeypatch):
    """A refused connection (endpoint down / wrong base_url) is a STEADY-STATE failure: raise
    immediately WITHOUT retry/backoff, so /api/llm/health stays instant. Only timeouts / transient
    resets are retried."""
    import looplab.core.llm as llm
    calls = {"n": 0, "sleep": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        raise _conn_exc(_httpx.ConnectError("connection refused"))   # httpx.ConnectError -> fail fast

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: calls.__setitem__("sleep", calls["sleep"] + 1))
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["n"] == 1 and calls["sleep"] == 0       # one attempt, no backoff on a steady-state fail


def test_post_retries_transient_ssl_eof(monkeypatch):
    """A mid-read TLS EOF / protocol error (the peer hung up, common over a hosted gateway) is
    TRANSIENT and must be retried, not abort the run."""
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _conn_exc(_httpx.RemoteProtocolError("server disconnected"))
        return dict(_OK_BODY)

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3           # retried the transient TLS/protocol drop, then succeeded


def test_post_fails_fast_on_tls_cert_error(monkeypatch):
    """A TLS CERT verification error is a steady-state misconfig — fail fast, do NOT retry."""
    import ssl
    import looplab.core.llm as llm
    calls = {"n": 0, "sleep": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        raise _conn_exc(ssl.SSLCertVerificationError("certificate verify failed"))

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: calls.__setitem__("sleep", calls["sleep"] + 1))
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["n"] == 1 and calls["sleep"] == 0


# --- bad-body resilience: a keepalive-only / empty STREAM is a transient gateway hiccup → retry;
# a mid-read protocol drop → retry; a persistent SDK protocol error / a no-choices body → surface.
def test_post_retries_empty_stream(monkeypatch):
    """A stream that yields NOTHING usable (keepalive-only heartbeats — content/tool_calls/reasoning
    and finish_reason all empty) is a mid-stream stall: retried with backoff, then succeeds."""
    import looplab.core.llm as llm
    seq = iter([_EMPTY_STREAM, _OK_BODY])         # one empty stream degrades to non-stream, then good
    calls = {"stream_flags": []}

    def fake(payload, use_stream):
        calls["stream_flags"].append(use_stream)
        return dict(next(seq))

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")   # streams by default -> empty_stream fires
    monkeypatch.setattr(c, "_sdk_chat", fake)
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    # first attempt streamed and stalled empty; the retry degraded to a non-stream read and succeeded
    assert calls["stream_flags"] == [True, False]


def test_post_retries_transient_protocol_drop(monkeypatch):
    """A mid-read protocol drop (the gateway dropped the connection mid-body → openai.APIConnectionError
    wrapping httpx.RemoteProtocolError) is transient and must be retried, not abort the run."""
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _conn_exc(_httpx.RemoteProtocolError("peer closed connection without complete body"))
        return dict(_OK_BODY)

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3                            # retried the truncated reads, then succeeded


def test_post_raises_after_persistent_sdk_error(monkeypatch):
    """A persistent SDK-level protocol error (unparseable body etc.) surfaces a clean LLMError once
    retries are spent, never a raw openai exception into the run."""
    import looplab.core.llm as llm

    def fake(payload, use_stream):
        raise _openai.APIError("unparseable body", request=_REQ, body=None)

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])


def test_post_fails_fast_on_no_choices_body(monkeypatch):
    """A body with no `choices` (an `{"error": ...}` envelope the SDK surfaced as a dict) fails fast
    at the no-choices check with ONE attempt, not the retry budget."""
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        return {"error": {"code": 400, "message": "bad request"}}   # no "choices"

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    with pytest.raises(llm.LLMError, match="no choices"):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["n"] == 1                           # no-choices body fails fast, no retry


def test_post_wraps_raw_decode_error_as_llmerror(monkeypatch):
    """A gateway 200 with an empty/keepalive-only body makes the SDK raise a RAW json.JSONDecodeError
    (not an openai.APIError). It must be retried like a transient hiccup and surface as a clean
    LLMError — NOT escape _post and abort the run (the role layer only retries/falls back on LLMError)."""
    import json as _json
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake(payload, use_stream):
        calls["n"] += 1
        raise _json.JSONDecodeError("Expecting value", "", 0)   # raw decode error, not openai.*

    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c, "_sdk_chat", fake)
    with pytest.raises(llm.LLMError, match="unparseable"):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["n"] == 5                            # 1 + _max_retries, then clean LLMError


def test_sdk_chat_forwards_guided_json_and_response_format(monkeypatch):
    """complete_tool with guided_json must forward BOTH response_format (kwarg) and guided_json
    (extra_body) to the SDK — the vLLM/SGLang constrained-decoding path."""
    import looplab.core.llm as llm
    captured = {}

    class _Resp:
        def model_dump(self):
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "emit", "arguments": "{}"}}]}}], "usage": {}}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _Resp()

    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1", stream=False, guided_json=True)
    monkeypatch.setattr(c._sdk.chat.completions, "create", fake_create)
    c.complete_tool([{"role": "user", "content": "go"}], {"type": "object", "properties": {}})
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["extra_body"]["guided_json"] == {"type": "object", "properties": {}}


def test_h1_guided_json_adds_schema_constraints():
    """H1: with guided_json on, complete_tool sends response_format + guided_json built from the schema."""
    from looplab.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient("m", guided_json=True)
    captured = {}

    def _fake_post(payload):
        captured.update(payload)
        return {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "emit", "arguments": "{}"}}]}}], "usage": {}}

    c._post = _fake_post
    c.complete_tool([{"role": "user", "content": "x"}], {"type": "object", "properties": {}})
    assert captured.get("response_format", {}).get("type") == "json_schema"
    assert "guided_json" in captured


def test_h1_off_by_default_no_constraints():
    from looplab.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient("m")
    captured = {}
    c._post = lambda p: (captured.update(p) or {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "emit", "arguments": "{}"}}]}}], "usage": {}})
    c.complete_tool([{"role": "user", "content": "x"}], {"type": "object"})
    assert "response_format" not in captured and "guided_json" not in captured


def test_read_stream_falls_back_on_iterable_non_sse_body():
    """A response that ITERATES line-by-line but never sends a `data:` line (a non-streaming endpoint
    behind a streaming request) must be reassembled from its raw lines and parsed as one plain JSON
    chat body — the `_sse_chunks` tail (raw_lines + got_sse) feeding `_read_stream`'s fallback."""
    import looplab.core.llm as llm

    class _IterResp:
        """Iterable urllib-response stand-in with NO usable read() (forces the raw-lines path)."""
        def __init__(self, lines):
            self._lines = lines
            self.fp = None

        def __iter__(self):
            return iter(self._lines)

        def close(self):
            pass

    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "plain"}}],
                       "usage": {"total_tokens": 3}}, indent=1)
    # A multi-line JSON body (plus a leading ':' comment) — line iteration splits at real newlines.
    lines = [b": PROCESSING\n"] + [(ln + "\n").encode() for ln in body.splitlines()]
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    out = c._read_stream(_IterResp(lines))
    assert out["choices"][0]["message"]["content"] == "plain"
    assert out["usage"] == {"total_tokens": 3}


# ─────────────────────────────── native tool-call recovery + stream reassembly (mega review) ─────

class _FakeResp:
    """A minimal iterable/closable stand-in for a urllib SSE response."""
    def __init__(self, lines):
        self._lines = lines
        self.fp = None

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


def _sse(*chunks):
    lines = [("data: " + json.dumps(c) + "\n").encode() for c in chunks]
    lines.append(b"data: [DONE]\n")
    return _FakeResp(lines)


def test_native_tool_call_quoted_prose_is_not_executed():
    """Un-fenced prose that merely QUOTES the tool syntax must NOT be lifted into a real tool call
    (it would execute e.g. delete_file on documentation text)."""
    txt = ('To call a tool you write: invoke name="delete_file" with parameter name="arguments">'
           '{"path": "main.py"}</parameter> and close with </invoke>. That is the syntax.')
    calls, _clean = _extract_native_tool_calls(txt)
    assert calls is None


def test_native_tool_call_real_leak_is_recovered():
    """A genuine leaked native block (opening tag present) is still recovered."""
    leaked = ('<｜DSML｜invoke name="emit"><｜DSML｜parameter name="arguments">'
              '{"x": 1}</｜DSML｜parameter></｜DSML｜invoke>')
    calls, _clean = _extract_native_tool_calls(leaked)
    assert calls and calls[0]["function"]["name"] == "emit"
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}


# --- streaming reassembly: these exercise the LIVE `_accumulate_stream` path (the SDK client streams
# by default), feeding it real SDK-shaped chunk objects (model_construct = lenient, like the SDK's own
# streaming decoder, so a provider-omitted `index` is None rather than a validation error). ----------
from openai.types.chat import ChatCompletionChunk  # noqa: E402
from openai.types.chat.chat_completion_chunk import (  # noqa: E402
    Choice as _ChunkChoice, ChoiceDelta as _Delta,
    ChoiceDeltaToolCall as _DToolCall, ChoiceDeltaToolCallFunction as _DFunc)


def _tc(index=None, id=None, name=None, args=None):
    return _DToolCall.model_construct(index=index, id=id, type="function",
                                      function=_DFunc.model_construct(name=name, arguments=args))


def _chunk(*, content=None, reasoning=None, tool_calls=None, finish=None, usage=None):
    delta = _Delta.model_construct(content=content, reasoning=reasoning, tool_calls=tool_calls)
    return ChatCompletionChunk.model_construct(
        id="x", object="chat.completion.chunk", created=0, model="m", usage=usage,
        choices=[_ChunkChoice.model_construct(index=0, delta=delta, finish_reason=finish)])


def _accum(chunks):
    return OpenAICompatibleClient._accumulate_stream(iter(chunks))["choices"][0]["message"]


def test_accumulate_stream_content_and_reasoning():
    """The live streaming reassembly joins content + reasoning deltas (non-standard fields survive the
    SDK's extra='allow' delta)."""
    msg = _accum([_chunk(content="Hel", reasoning="th"), _chunk(content="lo", reasoning="ink"),
                  _chunk(finish="stop")])
    assert msg["content"] == "Hello" and msg["reasoning"] == "think"


def test_streamed_tool_calls_without_index_stay_separate():
    """Providers that omit `index` (one whole call per delta) must not collapse both calls into slot 0."""
    msg = _accum([_chunk(tool_calls=[_tc(id="a", name="f1", args='{"x":1}')]),
                  _chunk(tool_calls=[_tc(id="b", name="f2", args='{"y":2}')]),
                  _chunk(finish="tool_calls")])
    calls = msg["tool_calls"]
    assert [(t["function"]["name"], t["function"]["arguments"]) for t in calls] == [
        ("f1", '{"x":1}'), ("f2", '{"y":2}')]


def test_streamed_single_call_fragments_stay_merged():
    """Fragments of ONE call (id/name only on the first delta) must not split into two calls."""
    msg = _accum([_chunk(tool_calls=[_tc(id="a", name="f1", args='{"x":')]),
                  _chunk(tool_calls=[_tc(args='1}')]),
                  _chunk(finish="tool_calls")])
    calls = msg["tool_calls"]
    assert len(calls) == 1 and calls[0]["function"]["arguments"] == '{"x":1}'


def test_tool_call_slot_prefers_explicit_index():
    assert _tool_call_slot({}, {"index": 3}) == 3
    assert _tool_call_slot({0: {"id": "a", "function": {"name": "f", "arguments": []}}},
                           {"index": 0}) == 0


def test_streamed_single_call_with_echoed_name_stays_merged():
    """A provider that ECHOES function.name on every continuation delta (no index/id) must not split
    one call into invalid-JSON fragments."""
    msg = _accum([_chunk(tool_calls=[_tc(name="emit", args='{"x":')]),
                  _chunk(tool_calls=[_tc(name="emit", args='1}')]),
                  _chunk(finish="tool_calls")])
    calls = msg["tool_calls"]
    assert len(calls) == 1 and calls[0]["function"]["arguments"] == '{"x":1}'


def test_streamed_two_indexless_noid_calls_split_on_complete_args():
    """Two distinct index-less, id-less calls (each with complete JSON args) must NOT merge into one."""
    msg = _accum([_chunk(tool_calls=[_tc(name="f1", args='{"a":1}')]),
                  _chunk(tool_calls=[_tc(name="f2", args='{"b":2}')]),
                  _chunk(finish="tool_calls")])
    assert len(msg["tool_calls"]) == 2


def test_native_recovery_ignores_markup_quoted_in_code_blocks():
    quoted = ('Here is how the DSML template looks:\n```\n<tool_calls><invoke name="run_command">'
              '<parameter name="command">rm -rf /</parameter></invoke></tool_calls>\n```\nSafe.')
    calls, clean = _extract_native_tool_calls(quoted)
    assert calls is None and clean == quoted                       # quoted example: untouched
    msg = {"role": "assistant", "content": quoted}
    assert _apply_native_tool_calls(msg).get("tool_calls") is None
    assert msg["content"] == quoted                                # reply not truncated

    leaked = ('<tool_calls><invoke name="read_file"><parameter name="path">/tmp/x</parameter>'
              '</invoke></tool_calls>')
    calls2, clean2 = _extract_native_tool_calls(leaked)            # a REAL leak still recovers
    assert calls2 and calls2[0]["function"]["name"] == "read_file"
    assert clean2 == ""


def test_llm_transport_error_is_clean_and_falls_back():
    from looplab.core.llm import OpenAICompatibleClient, LLMError
    from looplab.core.models import Idea
    from looplab.core.parse import ParseError, parse_structured
    client = OpenAICompatibleClient(model="x", base_url="http://127.0.0.1:9/v1", timeout=2.0)
    with pytest.raises(LLMError):                 # raw URLError no longer escapes
        client.complete_text([{"role": "user", "content": "hi"}])
    # parse_structured treats it as a parse failure -> ParseError (the role layer then falls back)
    with pytest.raises(ParseError):
        parse_structured(client, [{"role": "user", "content": "hi"}], Idea, "tool_call")


def test_stream_idle_guard_kills_keepalive_trickle():
    """A stream that yields NO events (an SSE keepalive-comment trickle the SDK filters out) must not
    hang: the idle-guard watchdog SHUTS DOWN the underlying socket after idle_limit and surfaces
    APITimeoutError — httpx's per-read timeout alone can't catch this (keepalive bytes reset it), and
    resp.close() alone can't unblock a kernel recv (verified live) — only socket.shutdown() does."""
    import threading
    from looplab.core.llm import _stream_with_idle_guard
    shot = threading.Event()

    class _Sock:                       # exposed via the httpx network_stream extension
        def shutdown(self, _how):
            shot.set()                 # shutdown() is what unblocks the blocked read

    class _NS:
        def get_extra_info(self, _k):
            return _Sock()

    class _Resp:
        request = None
        extensions = {"network_stream": _NS()}
        def close(self):
            pass

    class _Stream:
        response = _Resp()
        def __iter__(self):
            return self
        def __next__(self):
            shot.wait(timeout=5)       # block like a stalled read until the watchdog shuts the socket
            raise StopIteration

    with pytest.raises(_openai.APITimeoutError):
        list(_stream_with_idle_guard(_Stream(), idle_limit=0.3))
    assert shot.is_set()               # the watchdog reached the socket and shut it down


def test_stream_idle_guard_disabled_passes_through():
    """idle_limit<=0 (tests / plain iterators) just passes events through, no watchdog."""
    from looplab.core.llm import _stream_with_idle_guard
    assert list(_stream_with_idle_guard(iter([1, 2, 3]), idle_limit=0)) == [1, 2, 3]


def test_module_import_safe_without_live_deps_and_guard_raises(monkeypatch):
    """Regression: the openai-SDK migration added a top-level `import openai`/`import httpx` to
    core.llm, but core.config imports a constant from core.llm — so a MISSING openai broke the
    WHOLE-package import (offline engine, replay, config) with a bare ModuleNotFoundError. The import
    is now guarded: the module loads with the names None, and constructing the live client fails with
    a clear LLMError instead of an opaque `NoneType has no attribute 'OpenAI'`."""
    import looplab.core.config as _cfg  # must import even in a stripped env  # noqa: F401
    import looplab.core.llm as llm
    monkeypatch.setattr(llm, "openai", None)
    monkeypatch.setattr(llm, "httpx", None)
    with pytest.raises(llm.LLMError, match="openai"):
        llm.OpenAICompatibleClient("m", base_url="http://x/v1")


def test_openai_and_httpx_are_core_dependencies():
    """Regression guard: openai + httpx must be DECLARED runtime deps (not dev-only / undeclared) —
    the live transport imports them and core.config transitively pulls core.llm at import time, so a
    plain `pip install looplab` that omitted them shipped a package that crashed on import."""
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    names = {d.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for d in deps}
    assert {"openai", "httpx"} <= names, f"openai/httpx must be core deps; got {sorted(names)}"
