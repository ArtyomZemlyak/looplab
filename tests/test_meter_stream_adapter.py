"""The meter may ask upstream for a stream the client did not ask for (`METER_STREAM_ADAPT=1`).

WHY IT EXISTS, measured 2026-08-23 on a live campaign: the gateway in front of the models is nginx
with `proxy_read_timeout` at 300 s. Forty arm-A calls came back `504 Gateway Time-out` at latency
300.011 s — byte-identical nginx HTML — because arm A does not stream and nginx waits for the whole
body. Arm B streams, so bytes keep flowing and it survives to the model server's own ~1800 s limit:
0 of its 8,296 calls saw a 504. One arm-A task-arm spent three and a half of its four wall-clock
hours inside those timeouts and reached $0.14 of its $1.00 ceiling.

That is a difference between TRANSPORTS and neither framework chose it, so it is corrected in the
harness rather than in a thing under measurement. OFF by default: enabling it mid-campaign would
split an arm into two halves measured through two transports.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy", Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)


_FRAMES = [
    {"id": "cmpl-1", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
    {"id": "cmpl-1", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
    {"id": "cmpl-1", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
]


class _Upstream(BaseHTTPRequestHandler):
    """Answers ONLY a streamed request, which is the property under test: if the meter forwarded
    the client's non-streaming body unchanged, this returns 400 and the test fails loudly."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not body.get("stream"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"this upstream only answers streams"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for f in _FRAMES:
            self.wfile.write(b"data: " + json.dumps(f).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *_args):
        pass


@pytest.fixture()
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def _meter(upstream_url: str, tmp_path: Path):
    srv = proxy.Server(("127.0.0.1", 0), proxy.Handler)
    srv.upstream = upstream_url
    srv.api_key = ""
    srv.timeout = 30.0
    srv.max_retries = 0
    price = tmp_path / "pricing.json"
    price.write_text(json.dumps({"source": "test", "fetched_at": "now", "cost_basis": "imputed",
                                 "default": {"in": 0.0, "out": 0.0}, "models": {}}), encoding="utf-8")
    srv.pricing = proxy.Pricing(str(price))
    srv.meter = proxy.Meter(tmp_path / "meter.jsonl")
    srv.limiter = proxy.RateLimiter(1000)
    # No system proxy: the meter's own opener is built this way in `main()`, and without it a
    # `$http_proxy` in the environment turns every test into a 502 against somebody's corporate box.
    srv.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _post(base: str) -> tuple:
    req = urllib.request.Request(
        base + "/m/A/t/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def test_off_by_default_the_body_reaches_upstream_unchanged(upstream, tmp_path, monkeypatch):
    monkeypatch.delenv("METER_STREAM_ADAPT", raising=False)
    srv, base = _meter(upstream, tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(base)
        assert err.value.code == 400            # the upstream refused a non-streamed request
    finally:
        srv.shutdown()


def test_on_the_client_gets_one_whole_answer_and_never_sees_a_frame(upstream, tmp_path, monkeypatch):
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    srv, base = _meter(upstream, tmp_path)
    try:
        status, raw = _post(base)
    finally:
        srv.shutdown()
    assert status == 200
    body = json.loads(raw)                      # not SSE: plain JSON, or this raises
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hello"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["completion_tokens"] == 2
    assert body["id"] == "cmpl-1" and body["model"] == "m"


def test_the_adapted_call_is_still_metered_and_says_it_was_adapted(upstream, tmp_path, monkeypatch):
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    srv, base = _meter(upstream, tmp_path)
    try:
        _post(base)
    finally:
        srv.shutdown()
    rows = [json.loads(l) for l in (tmp_path / "meter.jsonl").read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["stream_adapted"] is True and rows[0]["arm"] == "A"
    # The ledger must not lose the call just because its shape changed on the way through.
    assert rows[0]["completion_tokens"] == 2 and rows[0]["metered"] is True

class _AbortingUpstream(BaseHTTPRequestHandler):
    """What the real gateway does at ~1800 s: start a stream, send deltas, then CLOSE — no usage
    frame, no `[DONE]`. Measured 23 times in one campaign, up to 238k deltas at 1830 s."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not body.get("stream"):
            self.send_response(400); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # A reasoning model cut mid-think: everything on `reasoning_content`, `content` still empty.
        for chunk in ("thinking A", "thinking B", "thinking C"):
            self.wfile.write(b"data: " + json.dumps(
                {"id": "cmpl-cut", "model": "m",
                 "choices": [{"index": 0, "delta": {"reasoning_content": chunk}}]}).encode() + b"\n\n")
        self.wfile.flush()
        # and then nothing at all — the connection simply ends

    def log_message(self, *_args):
        pass


@pytest.fixture()
def aborting_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AbortingUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_an_adapted_stream_the_gateway_cuts_still_carries_its_price(aborting_upstream, tmp_path,
                                                                    monkeypatch):
    """The defect this file exists for, and the one its first three tests could not see.

    The estimate was written through `emit()`, which returns immediately when the client is being
    COLLECTED for — so an adapted client got a body with no `usage` and a null finish reason: a
    truncated answer arriving as a clean 200 with nothing for that arm's accountant to read. The
    original fake upstream always sent a usage frame, so the abort path was never exercised.
    """
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    srv, base = _meter(aborting_upstream, tmp_path)
    try:
        status, raw = _post(base)
    finally:
        srv.shutdown()
    assert status == 200
    body = json.loads(raw)

    # 1. The price is THERE, and it is labelled a floor rather than a measurement.
    usage = body.get("usage") or {}
    assert usage.get("completion_tokens", 0) > 0, "a cut that produced deltas must carry a token floor"
    assert usage.get("cost_basis") == "estimated_from_deltas"
    assert "FLOOR" in usage.get("meter_note", "")

    # 2. The answer does not claim to be complete, and says so in the SAME word the client's own
    #    salvage uses — the two disagreeing about one cut is half of the defect.
    assert body["choices"][0]["finish_reason"] == proxy.STREAM_TRUNCATED_FINISH_REASON

    # 3. What the model actually spent the money on survives. Folding reasoning into `content`
    #    would hand the arm a command stream containing the model's private thinking; dropping it
    #    would hand it an empty answer for a call that produced a great deal.
    msg = body["choices"][0]["message"]
    assert msg["content"] == ""
    assert msg["reasoning_content"] == "thinking Athinking Bthinking C"

    # 4. And the meter row agrees with the body it handed over.
    rows = [json.loads(l) for l in (tmp_path / "meter.jsonl").read_text().splitlines() if l.strip()]
    assert rows[-1]["stream_adapted"] is True and rows[-1]["stream_aborted"] is True
    assert rows[-1]["completion_tokens"] == usage["completion_tokens"]


def test_the_two_transports_stamp_the_SAME_word_on_a_cut():
    """This file is a standalone script and cannot import `looplab`, so the word is copied. A copy
    that drifts would have the streamed frame and the reassembled body disagreeing about one cut."""
    src = (Path(__file__).resolve().parents[1] / "looplab/core/llm.py").read_text(encoding="utf-8")
    m = re.search(r'STREAM_TRUNCATED_FINISH_REASON\s*=\s*"([^"]+)"', src)
    assert m, "the client no longer names its truncated finish_reason — find where it moved"
    assert proxy.STREAM_TRUNCATED_FINISH_REASON == m.group(1)
