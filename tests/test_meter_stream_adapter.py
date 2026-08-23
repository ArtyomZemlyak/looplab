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
