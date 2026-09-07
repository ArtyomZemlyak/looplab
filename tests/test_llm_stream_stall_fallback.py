"""`Settings.llm_stream_stall_fallback`: does a stalled STREAM retry without SSE, or as a stream?

The historical client (docs/60 §60.9 A7, the engine half) answers "without SSE, and after
`STREAM_STALL_DEGRADE_AFTER` stalls never with SSE again", which is the right trade on an endpoint
that answers the same request fine without a stream while its stream wedges. On the bench stand it
is the wrong one, measured (docs/56 §173-175): the proxy's `proxy_read_timeout 300` bounds the
WHOLE request, an unstreamed generation measures the whole generation against it, and `oldCK9`
sent 58 of 301 calls unstreamed on this fallback's own initiative under `LOOPLAB_LLM_STREAM=1`, 4
of them dead at 300.0 s, $0.10 of its $1.00 on twenty re-sends of one body.

Two layers are driven. The transport SEAM (`_sdk_chat`, the method the client's own docstring
names as the one tests script) gives the per-attempt truth table for all three stall families;
one REAL endpoint — an HTTP server on a private port that stalls the first stream after its
headers and serves the second request — proves the decision holds through httpx, the SDK and the
idle guard rather than only through a scripted method.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import orjson
import pytest

import looplab.core.llm as llm
from looplab.core import tracing
from looplab.core.config import Settings
from looplab.core.llm import (STREAM_STALL_DEGRADE_AFTER, OpenAICompatibleClient,
                              make_llm_client)
from looplab.core.tracing import JsonlSpanExporter, Tracer

_PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
_OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    # Every retry family sleeps `_backoff(attempt)` (2 s at attempt 0) before returning; the wait
    # is not the subject and would make each case here cost seconds for nothing.
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)


# ------------------------------------------------------------------ the transport seam

def _client(**kw) -> OpenAICompatibleClient:
    return OpenAICompatibleClient("m", base_url="http://stub.invalid/v1", max_retries=3, **kw)


def _script(client, first_stream_outcome):
    """Record `use_stream` per attempt; the FIRST streamed attempt meets `first_stream_outcome`
    (an exception to raise or a body to return), every later attempt is served."""
    seen: list[bool] = []

    def fake(payload, use_stream):
        seen.append(use_stream)
        if use_stream and seen.count(True) == 1:
            if isinstance(first_stream_outcome, BaseException):
                raise first_stream_outcome
            return first_stream_outcome
        return json.loads(json.dumps(_OK))

    client._sdk_chat = fake
    return seen


def _stall_families():
    # The three ways `_post` learns a stream stalled, each on its own policy branch:
    #   * a mid-body idle timeout / transport cut -> `_policy_connection`;
    #   * an in-band `data: {"error": …}` frame inside a 200 body -> `_policy_stream_interrupted`
    #     (the SDK builds a BARE `APIError` for exactly that);
    #   * a keepalive-only 200 (headers, heartbeats, no content, no finish) -> `_keepalive_stall`.
    yield pytest.param(openai.APITimeoutError(request=None), id="idle-timeout")
    yield pytest.param(openai.APIError("upstream broke", request=None, body=None), id="inband-error")
    yield pytest.param({"choices": [{"message": {"content": ""}}]}, id="keepalive-only-200")


@pytest.mark.parametrize("stall", list(_stall_families()))
def test_the_default_is_the_historical_client_the_next_attempt_drops_sse(stall):
    client = _client()
    seen = _script(client, stall)
    body = client._post(dict(_PAYLOAD))
    assert body["choices"][0]["message"]["content"] == "hi"
    assert seen == [True, False], (
        "the historical degrade: attempt 2 of a stalled stream goes out WITHOUT SSE")
    assert client._stream_stalls == 1


@pytest.mark.parametrize("stall", list(_stall_families()))
def test_with_the_fallback_off_a_stalled_stream_is_retried_as_a_stream(stall):
    client = _client(stream_stall_fallback=False)
    seen = _script(client, stall)
    body = client._post(dict(_PAYLOAD))
    assert body["choices"][0]["message"]["content"] == "hi"
    assert seen == [True, True], (
        "with the fallback off the retry is a STREAM on the same backoff — the unstreamed attempt "
        "is the one nginx's whole-request timeout kills (docs/56 §173)")
    assert client._stream_stalls == 1, "the stall is still COUNTED; only the decision changed"


def test_the_permanent_degrade_is_taken_only_with_the_fallback_on():
    """`STREAM_STALL_DEGRADE_AFTER` stalls used to switch the client to non-SSE for good."""
    degraded = _client()
    degraded._stream_stalls = STREAM_STALL_DEGRADE_AFTER
    seen = _script(degraded, openai.APITimeoutError(request=None))
    degraded._post(dict(_PAYLOAD))
    assert seen == [False], "historical: past the threshold every attempt is unstreamed"

    kept = _client(stream_stall_fallback=False)
    kept._stream_stalls = STREAM_STALL_DEGRADE_AFTER
    seen = _script(kept, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})
    kept._post(dict(_PAYLOAD))
    assert seen == [True], "fallback off: the counter never disables streaming"


def test_a_non_streaming_client_is_untouched_either_way():
    for fallback in (True, False):
        client = _client(stream=False, stream_stall_fallback=fallback)
        seen = _script(client, openai.APITimeoutError(request=None))
        client._post(dict(_PAYLOAD))
        assert seen == [False], "`stream=False` was never a streaming client; the knob is moot"


def test_the_setting_reaches_the_client_through_the_one_factory():
    assert Settings().llm_stream_stall_fallback is True, "True = the historical client"
    assert make_llm_client(Settings())._stream_stall_fallback is True
    assert make_llm_client(Settings(llm_stream_stall_fallback=False))._stream_stall_fallback is False
    assert OpenAICompatibleClient("m", base_url="http://x/v1")._stream_stall_fallback is True, (
        "a bare client keeps the historical behaviour: the kwarg defaults to the old code path")


# ------------------------------------------------------------------ the per-attempt record

def test_the_generation_span_now_says_per_attempt_whether_the_call_was_streamed(tmp_path):
    """Nothing recorded it: the span carried `model_parameters` and money, never HOW the request
    went out, so docs/56 §173 had to count unstreamed calls in the proxy's ledger. `_post` stamps
    `stream_attempts` on every attempt, so a call that raises mid-ladder still says how it was
    sent."""
    client = _client()
    _script(client, openai.APITimeoutError(request=None))
    tracer = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with tracer.span("propose", new_trace=True, node_id=1):
        with tracing.generation(op="chat", model="m", messages=_PAYLOAD["messages"]):
            client._post(dict(_PAYLOAD))
    recs = [orjson.loads(line) for line in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    gen = next(r for r in recs if r["kind"] == "generation")
    assert gen["attributes"]["stream_attempts"] == [True, False]


def test_annotate_generation_is_silent_when_nothing_is_traced():
    assert tracing.annotate_generation("stream_attempts", [True]) is False


# ------------------------------------------------------------------ one real endpoint

_CHUNK = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
          "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"},
                       "finish_reason": None}]}
_LAST = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
_BODY = {"id": "c", "object": "chat.completion", "created": 1, "model": "m",
         "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                      "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class _Endpoint(ThreadingHTTPServer):
    """Stalls the FIRST streamed request after its headers; serves everything after it."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr):
        super().__init__(addr, _Handler)
        self.requests: list[bool] = []          # `stream` flag of each request body, in order
        self.stalled = threading.Event()        # the first stream is now hanging
        self.release = threading.Event()        # let that handler thread go


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        streamed = bool(body.get("stream"))
        first_stream = streamed and True not in self.server.requests
        self.server.requests.append(streamed)
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/event-stream" if streamed else "application/json")
        self.end_headers()
        try:
            if first_stream:
                # Headers and a heartbeat, then nothing: the shape of a proxied stream that
                # wedged mid-generation. The client's first-event guard is what has to end it.
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                self.server.stalled.set()
                self.server.release.wait(10.0)
                return
            if streamed:
                for chunk in (_CHUNK, _LAST):
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(json.dumps(_BODY).encode())
            self.wfile.flush()
        except OSError:                          # the client hung up on the stalled stream
            return


@pytest.fixture
def endpoint():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = _Endpoint(("127.0.0.1", port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, f"http://127.0.0.1:{port}/v1"
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()


def _live(base_url: str, **kw) -> OpenAICompatibleClient:
    # `header_timeout` is the first-EVENT budget once headers arrive; a heartbeat is not an event,
    # so the stalled stream is cut ~0.4 s after its headers. Small `timeout` keeps the idle guard
    # under it. `max_retries=2`: the stall costs one attempt and the answer must come on the next.
    return OpenAICompatibleClient("m", base_url=base_url, api_key="k", timeout=1.0,
                                  header_timeout=0.4, max_retries=2, **kw)


def test_on_a_real_endpoint_the_historical_client_reissues_a_stalled_stream_without_sse(endpoint):
    server, base_url = endpoint
    started = time.monotonic()
    text = _live(base_url).complete_text(_PAYLOAD["messages"])
    assert text == "hi"
    assert server.stalled.is_set(), "the first stream really hung after its headers"
    assert server.requests == [True, False], (
        f"the endpoint saw {server.requests}: the historical retry of a stalled stream is "
        "UNSTREAMED, which is the request a whole-request proxy timeout then measures in full")
    assert time.monotonic() - started < 8.0, "the stall was cut by the guard, not by luck"


def test_on_a_real_endpoint_the_fallback_off_reissues_a_stalled_stream_as_a_stream(endpoint):
    server, base_url = endpoint
    text = _live(base_url, stream_stall_fallback=False).complete_text(_PAYLOAD["messages"])
    assert text == "hi"
    assert server.stalled.is_set()
    assert server.requests == [True, True], (
        f"the endpoint saw {server.requests}: with `llm_stream_stall_fallback=false` the retry "
        "is a STREAM, so the 300 s wall never measures a whole generation")
