"""A completion delivered as TOOL CALLS is a generation the meter can see and price.

The closed item (`meter-delta-counter-is-blind-to-tool-calls`, the PRICING half; benchmarks/meter/
proxy.py's docstring): the stream counter read only `content` / `reasoning_content` / `reasoning`, so
a completion that arrived on `delta.tool_calls[].function.arguments` counted ZERO deltas. Cut without
a usage frame -- by the gateway's EOF or by an upstream exception -- it was recorded
`metered: false, cost: 0.0` whatever the calibration said, and the calibration itself never learned
from a tool-call stream, because `observe(0, tokens)` is not evidence.

WHAT IS DELIBERATELY NOT CHANGED, and pinned here so it cannot drift in by accident: the armed delta
CEILING still counts TEXT deltas only, and `deltas_seen` keeps its text-only meaning. Counting tool
calls there moves one arm's cuts and not the other's and can cut inside a tool call's JSON -- the
open item `meter-ceiling-blind-to-tool-call-runaways` owns that measurement.

Driven against the REAL handler over REAL sockets, each test with its own upstream, meter and
ephemeral port. Runaway frames are FAT, for the reason the sibling ceiling test states, and fatter
than its: 4,000 x ~4 kB is ~16 MB, about four times this box's `tcp_wmem` ceiling, so "the upstream
stopped" cannot be confused with "the kernel swallowed the rest" (a thin-frame first draft of this
file failed 3 of 20 runs under load on exactly that).
"""
from __future__ import annotations

import importlib.util
import json
import socket
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy_tool_calls", Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

RATE_IN = 0.00000014
RATE_OUT = 0.00000028
CEILING = 40
FRAGMENTS = 12          # a short tool-call answer, well under the ceiling
RUNAWAY = 4000          # far past the ceiling, so "cut" cannot be confused with "finished"
_FAT = "x" * 4000


def _tool_frame(i: int, *, fat: bool = False) -> dict:
    """One streamed tool-call fragment in the OpenAI shape: the first names the function and
    carries the call id, every later one carries argument bytes only."""
    tc = {"index": 0, "function": {"arguments": _FAT if fat else ('{"x": ' if i == 0 else "1")}}
    if i == 0:
        tc.update({"id": "call_1", "type": "function"})
        tc["function"]["name"] = "run"
    return {"id": "cmpl-t", "object": "chat.completion.chunk", "model": "m", "created": 1,
            "choices": [{"index": 0, "delta": {"tool_calls": [tc]}}]}


def _text_frame(*, fat: bool = False) -> dict:
    return {"id": "cmpl-t", "choices": [{"index": 0, "delta": {"content": _FAT if fat else "tok"}}]}


class _Upstream(BaseHTTPRequestHandler):
    """The request's last message picks the stream.

    `TOOLS` streams tool-call fragments only; `TEXT` streams content only; `MIXED` alternates a
    content frame and a tool-call fragment and never stops (fat frames). The ending: `NOUSAGE`
    ends tidily with `[DONE]` and no usage frame (the gateway-cut shape the estimate exists for),
    `PRICED` ends with a proper usage frame, `RST` resets the socket after the frames (an upstream
    death by exception), and anything else never ends. `written` counts the frames actually
    written, so a test can tell "the meter stopped reading" from "it read everything".
    """

    protocol_version = "HTTP/1.1"
    written: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        said = json.dumps(body.get("messages") or [])
        ending = next((m for m in ("NOUSAGE", "PRICED", "RST") if m in said), "NEVER")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        n = 0
        try:
            for i in range(RUNAWAY if ending == "NEVER" else FRAGMENTS):
                if "MIXED" in said:
                    frame = _text_frame(fat=True) if i % 2 == 0 else _tool_frame(i, fat=True)
                elif "TEXT" in said:
                    frame = _text_frame()
                else:
                    frame = _tool_frame(i)
                self._frame(frame)
                self.wfile.flush()
                n += 1
            if ending == "RST":
                time.sleep(0.3)             # let the proxy consume what was sent; RST drops unread
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                           struct.pack("ii", 1, 0))
                self.connection.close()
            elif ending != "NEVER":
                self._frame({"id": "cmpl-t", "choices": [{"index": 0, "delta": {},
                                                          "finish_reason": "tool_calls"}]})
                if ending == "PRICED":
                    self._frame({"id": "cmpl-t", "choices": [],
                                 "usage": {"prompt_tokens": 90, "completion_tokens": 30,
                                           "total_tokens": 120}})
                raw = b"data: [DONE]\n\n"
                self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except OSError:
            pass                                 # the reader hung up: the ceiling working
        finally:
            type(self).written.append(n)
            self.close_connection = True

    def _frame(self, payload: dict) -> None:
        raw = b"data: " + json.dumps(payload).encode() + b"\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")

    def log_message(self, *_args):
        pass


@pytest.fixture()
def meter(tmp_path):
    _Upstream.written = []
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    srv = proxy.Server(("127.0.0.1", 0), proxy.Handler)
    srv.upstream = f"http://127.0.0.1:{up.server_port}/v1"
    srv.api_key = ""
    srv.timeout = 30.0
    srv.max_retries = 0
    srv.delta_ceiling = CEILING
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
        yield f"http://127.0.0.1:{srv.server_port}/m/A/t/a1", tmp_path / "meter.jsonl", srv
    finally:
        srv.shutdown()
        up.shutdown()


def _call(base: str, words: str) -> bytes:
    payload = {"model": "m", "stream": True,
               "messages": [{"role": "user", "content": f"{words} call the tool"}]}
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        assert resp.status == 200
        return resp.read()       # a cut is handed back as a complete answer, so this must not raise


def _row(path: Path) -> dict:
    """The meter row is written AFTER the client's last byte, so a reader must wait for it."""
    for _ in range(400):
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        time.sleep(0.02)
    raise AssertionError("the meter never recorded a row for a call it answered")


def _usage_frames(raw: bytes) -> list[dict]:
    out = []
    for block in raw.split(b"\n\n"):
        data = b"".join(line[len(b"data:"):].lstrip() for line in block.split(b"\n")
                        if line.strip().startswith(b"data:"))
        if data and data != b"[DONE]":
            frame = json.loads(data)
            if isinstance(frame.get("usage"), dict):
                out.append(frame["usage"])
    return out


def test_the_rule_counts_exactly_what_reassembly_carries():
    """`generation_delta_kind` is the counter's whole rule, and it must agree with `_reassemble`:
    a delta it counts as a tool call is one whose reassembled answer carries a name or argument
    bytes, and one it skips adds nothing to the answer. Driven through `_reassemble` itself rather
    than restated, so the two halves of one file cannot drift apart again."""
    kind = proxy.generation_delta_kind
    assert kind({"content": "a"}) == "text"
    assert kind({"reasoning_content": "a"}) == "text"
    assert kind({"reasoning": "a"}) == "text"
    # A delta carrying both is ONE delta, counted once, as text.
    assert kind({"content": "a", "tool_calls": [{"function": {"arguments": "{"}}]}) == "text"
    for fragment, counted in (
            ({"index": 0, "function": {"arguments": "{"}}, True),
            ({"index": 0, "id": "c", "function": {"name": "run"}}, True),
            ({"index": 0, "function": {"arguments": ""}}, False),
            ({"index": 0, "id": "c", "type": "function"}, False)):
        delta = {"tool_calls": [fragment]}
        assert (kind(delta) == "tool_call") is counted, fragment
        message = proxy.Handler._reassemble([{"choices": [{"delta": delta}]}])["choices"][0]["message"]
        carried = any(tc["function"]["name"] or tc["function"]["arguments"]
                      for tc in message.get("tool_calls") or [])
        assert carried is counted, fragment
    for nothing in ({}, {"role": "assistant"}, {"content": ""}, {"tool_calls": []},
                    {"tool_calls": "junk"}, {"tool_calls": [None, 3]}, None, "x", []):
        assert kind(nothing) == "", nothing


def test_a_cut_tool_call_stream_is_priced_not_recorded_as_free(meter):
    """THE DEFECT, DRIVEN: a completion delivered purely as tool calls, ended without a usage frame.
    Before the fix this row read `deltas_seen: 0, metered: false, cost: 0.0`."""
    base, log, _srv = meter
    raw = _call(base, "TOOLS NOUSAGE")
    row = _row(log)
    assert row["deltas_seen"] == 0, "the text counter keeps its text-only meaning"
    assert row["tool_call_deltas_seen"] == FRAGMENTS, row
    assert row["metered"] is True and row["cost"] > 0, row
    assert row["cost_basis"] == "estimated_from_deltas", row
    # The client's accountant reads the frame, not this log: it carries the split too.
    usage = _usage_frames(raw)[-1]
    assert usage["meter_forwarded_deltas"] == 0
    assert usage["meter_forwarded_tool_call_deltas"] == FRAGMENTS
    assert usage["completion_tokens"] >= FRAGMENTS
    # The note names what was forwarded in the row's OWN terms: a sum printed as "12 forwarded
    # deltas" beside `deltas_seen: 0` would contradict the field next to it.
    assert f"priced from 0 text + {FRAGMENTS} tool-call forwarded deltas" in row["note"], row


def test_an_upstream_death_under_a_tool_call_stream_is_a_priced_cut(meter):
    """The exception path: `if usage_frame_seen or not <anything forwarded>: raise`. On the text
    counter alone a tool-call-only stream that died by RST re-raised and was recorded unpriced; it
    is a served, priced cut like any other now."""
    base, log, _srv = meter
    _call(base, "TOOLS RST")
    row = _row(log)
    assert row.get("stream_cut_by") == "upstream_exception", row
    assert row["tool_call_deltas_seen"] == FRAGMENTS and row["metered"] is True, row
    assert row["cost"] > 0, row
    assert f"0 text + {FRAGMENTS} tool-call forwarded deltas (a FLOOR)" in row["note"], row


def test_the_armed_ceiling_still_counts_text_only(meter):
    """PINNED ON PURPOSE (`meter-ceiling-blind-to-tool-call-runaways` is open): a runaway that
    alternates text and tool-call frames is cut after CEILING TEXT deltas -- the tool-call fragments
    in between do not advance it -- and the cut lands on a text frame. The upstream really stops."""
    base, log, _srv = meter
    _call(base, "MIXED")
    row = _row(log)
    assert row["stream_cut_by"] == "meter_delta_ceiling", row
    assert row["deltas_seen"] == CEILING, row
    # The fragments in between did not advance it: the cut came at the CEILING-th text frame. (Not
    # "never mid-call" -- the call opened by the previous fragment is left half-written, as it
    # always could be; that is the open item's second half.)
    assert row["tool_call_deltas_seen"] == CEILING - 1, row
    assert f"{CEILING} text + {CEILING - 1} tool-call forwarded deltas" in row["note"], row
    assert row["cost"] > 0 and row["stream_aborted"] is True, row
    for _ in range(200):
        if _Upstream.written:
            break
        time.sleep(0.02)
    assert _Upstream.written and _Upstream.written[0] < RUNAWAY, _Upstream.written


def test_a_priced_tool_call_stream_is_calibration_evidence(meter):
    """`observe(0, tokens)` is refused as "nobody measured", so while the counter was blind no
    tool-call stream ever reached the calibration. A gateway-priced one now does, with every
    fragment it forwarded, and the gateway's own usage still wins on the row."""
    base, log, srv = meter
    _call(base, "TOOLS PRICED")
    row = _row(log)
    assert row["cost_basis"] != "estimated_from_deltas" and row["completion_tokens"] == 30, row
    assert row["deltas_seen"] == 0 and row["tool_call_deltas_seen"] == FRAGMENTS, row
    assert sum(srv.tokens_per_delta._calls) == 1
    assert sum(srv.tokens_per_delta._deltas) == FRAGMENTS
    assert sum(srv.tokens_per_delta._tokens) == 30


def test_a_text_stream_says_its_tool_call_share_is_zero(meter):
    """The new key is written at zero as well: its PRESENCE marks a row counted by a proxy that
    saw tool-call fragments. And a text-only row's note keeps its historical bytes."""
    base, log, _srv = meter
    _call(base, "TEXT NOUSAGE")
    row = _row(log)
    assert row["deltas_seen"] == FRAGMENTS and row["tool_call_deltas_seen"] == 0, row
    assert row["cost"] > 0, row
    assert f"priced from {FRAGMENTS} forwarded deltas (a FLOOR)" in row["note"], row


def test_a_calibrated_price_names_both_counters_in_the_frame(meter):
    """Once a bucket calibrates, the synthesized frame's prose prices the SUM of both counters and
    names the split -- the sentence the client's accountant reads beside `meter_forwarded_deltas`."""
    base, log, srv = meter
    for _ in range(proxy.TokensPerDelta.MIN_CALLS):
        _call(base, "TOOLS PRICED")          # 12 fragments, 30 completion tokens: 2.5 per delta
    raw = _call(base, "TOOLS NOUSAGE")
    usage = _usage_frames(raw)[-1]
    assert usage["meter_completion_tokens_basis"] == "estimated_from_calibrated_deltas", usage
    assert usage["completion_tokens"] == 30
    assert (f"completion_tokens is 0 text + {FRAGMENTS} tool-call forwarded deltas priced at 2.5"
            in usage["meter_note"]), usage["meter_note"]
