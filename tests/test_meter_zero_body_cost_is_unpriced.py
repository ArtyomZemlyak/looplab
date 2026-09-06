"""A body `usage.cost: 0.0` from upstream is UNPRICED, not free -- the header rule, on both routes.

THE DEFECT. `_header_cost` has always refused a zero: "LiteLLM emits `x-litellm-response-cost-
original: 0.0` for a model group it has no price for, which is the unpriced case, not a free one".
Thirty lines down, the BODY rule accepted the identical zero as an authoritative invoice --
`{metered: true, cost: 0.0, cost_basis: "upstream"}`, no imputation run -- and its streaming
twin in `_proxy_stream` did the same. The corporate gateway IS LiteLLM and already emits the header
form; the day it stamps the same zero into the body, every call prices at $0 under a green
`metered: true`, which is the founding "budget never binds" defect back, asserted as an invoice.

WHAT IS PINNED. Driven against the real `Handler` over a real socket, on the non-streaming route
and on the streaming one: a zero body cost falls through to imputation, the frame and the row both
say the reported figure was refused, and a POSITIVE body cost is still honoured verbatim -- the
"never overwrite a provider's own invoice" half must not be lost while the zero half is fixed.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy_zero_cost", Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

RATE_IN = 1e-6
RATE_OUT = 2e-6
PIN, POUT = 100, 10
IMPUTED = PIN * RATE_IN + POUT * RATE_OUT


class _Upstream(BaseHTTPRequestHandler):
    """Answers with whatever `usage.cost` the request's last message names (`cost=<x>`), on the
    transport the request asked for. `cost=absent` sends no cost key at all."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _usage(self, body: dict) -> dict:
        text = body["messages"][-1]["content"]
        usage = {"prompt_tokens": PIN, "completion_tokens": POUT, "total_tokens": PIN + POUT}
        spec = text.split("cost=", 1)[1].split()[0]
        if spec != "absent":
            usage["cost"] = json.loads(spec)
        return usage

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        usage = self._usage(body)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            frames = [
                {"id": "cmpl-z", "model": "m",
                 "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
                {"id": "cmpl-z", "choices": [], "usage": usage},
            ]
            for f in frames:
                raw = b"data: " + json.dumps(f).encode() + b"\n\n"
                self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            raw = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        out = json.dumps({"id": "cmpl-z", "object": "chat.completion", "model": "m",
                          "choices": [{"index": 0, "message": {"role": "assistant",
                                                                "content": "hi"},
                                       "finish_reason": "stop"}],
                          "usage": usage}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def meter(tmp_path):
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    srv = proxy.Server(("127.0.0.1", 0), proxy.Handler)
    srv.upstream = f"http://127.0.0.1:{up.server_port}/v1"
    srv.api_key = ""
    srv.timeout = 30.0
    srv.max_retries = 0
    price = tmp_path / "pricing.json"
    price.write_text(json.dumps({
        "source": "test", "fetched_at": "now", "cost_basis": "imputed",
        "default": {"input_per_token": RATE_IN, "output_per_token": RATE_OUT},
        "models": {}}), encoding="utf-8")
    srv.pricing = proxy.Pricing(str(price))
    srv.meter = proxy.Meter(tmp_path / "meter.jsonl")
    srv.limiter = proxy.RateLimiter(1000)
    # No system proxy: a `$http_proxy` in the environment would turn every test into a 502.
    srv.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield (f"http://127.0.0.1:{srv.server_port}/m/A/t/a1/v1/chat/completions",
               tmp_path / "meter.jsonl")
    finally:
        srv.shutdown()
        up.shutdown()


def _call(url: str, *, cost: str, stream: bool) -> dict:
    """The usage the CLIENT reads: the body's for a plain call, the empty-choices frame's for a
    stream (`stream_options.include_usage`'s own rule)."""
    payload = {"model": "m", "messages": [{"role": "user", "content": f"hello cost={cost}"}]}
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    if not stream:
        return json.loads(raw)["usage"]
    for block in raw.split(b"\n\n"):
        line = block.strip()
        if line.startswith(b"data: ") and line != b"data: [DONE]":
            frame = json.loads(line[6:])
            if isinstance(frame.get("usage"), dict) and not frame.get("choices"):
                return frame["usage"]
    raise AssertionError(f"no usage frame reached the streaming client: {raw!r}")


def _row(log: Path) -> dict:
    """The streaming row is written AFTER the client's last byte, so a reader must wait for it."""
    for _ in range(200):
        if log.exists() and log.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.02)
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.parametrize("stream", [False, True])
def test_a_zero_body_cost_is_imputed_and_says_the_zero_was_refused(meter, stream):
    url, log = meter
    usage = _call(url, cost="0.0", stream=stream)
    row = _row(log)
    # 1. The number the accountant reads is the IMPUTED one, on the pinned table.
    assert usage["cost"] == pytest.approx(IMPUTED)
    assert usage["cost_basis"].startswith("imputed")     # `-default-fallback`: `m` is unlisted
    assert row["cost"] == pytest.approx(IMPUTED) and row["cost_basis"].startswith("imputed")
    assert row["metered"] is True
    # 2. And both say the upstream's own figure was seen and refused, so nobody has to guess why
    #    `cost_basis` is not `upstream` on a call whose upstream did name a cost.
    assert usage["meter_upstream_cost_refused"] == 0.0
    assert row["upstream_cost_refused"] == 0.0


@pytest.mark.parametrize("stream", [False, True])
def test_a_positive_body_cost_is_still_the_providers_invoice(meter, stream):
    """The half that must survive the fix: a real OpenRouter's own number is never overwritten."""
    url, log = meter
    usage = _call(url, cost="0.0042", stream=stream)
    row = _row(log)
    assert usage["cost"] == pytest.approx(0.0042) and usage["cost_basis"] == "upstream"
    assert row["cost"] == pytest.approx(0.0042) and row["cost_basis"] == "upstream"
    assert "meter_upstream_cost_refused" not in usage and "upstream_cost_refused" not in row


@pytest.mark.parametrize("stream", [False, True])
def test_an_absent_cost_is_imputed_without_claiming_anything_was_refused(meter, stream):
    """The historical path, byte-for-byte: no key, no refusal note, the imputed price."""
    url, log = meter
    usage = _call(url, cost="absent", stream=stream)
    row = _row(log)
    assert usage["cost"] == pytest.approx(IMPUTED) and usage["cost_basis"].startswith("imputed")
    assert "meter_upstream_cost_refused" not in usage and "upstream_cost_refused" not in row


@pytest.mark.parametrize("reported", [0.0, 0, -1.0, "not a number"])
def test_the_body_rule_agrees_with_the_header_rule(reported):
    """One rule, two carriers: what `_header_cost` refuses, `_body_cost` refuses."""
    assert proxy._body_cost({"cost": reported}) is None
    assert proxy._header_cost({"x-litellm-response-cost-original": str(reported)}) is None
    assert proxy._body_cost({"cost": 0.5}) == 0.5
    assert proxy._header_cost({"x-litellm-response-cost-original": "0.5"}) == 0.5
    assert proxy._body_cost({}) is None and proxy._body_cost({"cost": None}) is None
