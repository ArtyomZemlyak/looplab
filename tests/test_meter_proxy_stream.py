"""What the metering proxy owes an SSE client: the whole stream, its terminator, and a socket it lets go.

Three defects of `benchmarks/meter/proxy.py::_proxy_stream`, each driven here against the real
`Handler` over real sockets with a fake upstream in front of it. All three are about the BYTES
reaching the client, which this module's own header puts above metering convenience ("the response
body comes back unchanged except for `usage.cost` ...").

1. `meter-swallows-the-done-sentinel`. `data: [DONE]` is held back rather than forwarded in place,
   because a conformant client STOPS there (openai-python's `Stream.__stream__` breaks on it) and
   the synthesized usage frame has to arrive before it. It was then put back on exactly ONE of the
   three exits -- the `estimated_from_deltas` branch, whose ending carries it -- so the dominant
   path (a usage frame and a tidy end: 8,830 of 9,235 recorded rows) and the empty-stream path
   swallowed it. Nothing broke in either arm, which both end on body EOF; but by this file's own
   reading a stream that never reached its terminator was CUT, so a strict observer downstream had
   to read every clean answer as a cut one.

2. `meter-midstream-death-holds-the-keepalive-socket`. The terminating zero-length chunk is written
   inside the forwarding `try`, so an upstream death returned with the chunked body unfinished on a
   connection HTTP/1.1 keeps alive. httpx/litellm pool every connection here, so the arm sat on a
   half-finished body until its own stall timeout -- minutes of dead clock per failure, times the
   retry after it. The urllib-based tests could not see it: they send `Connection: close`. So the
   client here speaks HTTP by hand with `Connection: keep-alive` and measures the EOF.

3. `meter-stream-rows-are-anonymous`. Streaming rows carried neither the completion `id` nor the
   model the GATEWAY reported -- both on every frame, and both recorded by the non-streaming route
   for exactly the correlate-with-the-provider need the cut rows have most -- the estimate dropped
   the pricing table's own answer (a `default` fallback rate read like a pinned one), and nothing
   named the endpoint, though `start_meter.sh` defaults every instance to one `meter.jsonl` and
   this box runs two meters against two differently-priced upstreams.

Nothing here touches a campaign's meter: its own ephemeral ports, its own log file, `--rpm`
effectively unlimited, and no network beyond loopback.
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
    "_meter_proxy_stream_protocol",
    Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

RATE_IN = 1e-6
RATE_OUT = 2e-6
PRICED_MODEL = "known-model"            # the one row in the test price table
UNPRICED_MODEL = "unpriced-model"       # falls back to `default`, and must say so
COMPLETION_ID = "cmpl-stream-1"         # what the GATEWAY calls this call
GATEWAY_MODEL = "gw-model-9"            # deliberately not the model the request asked for
CONTENT = ["tok0", "tok1"]
# How long the raw client waits for a byte before concluding the proxy is HOLDING the connection.
# It bounds only the failing case: a proxy that closes is observed immediately.
HOLD_TIMEOUT_S = 5.0


class _Upstream(BaseHTTPRequestHandler):
    """A gateway that ends its stream the way the request's last message says (`mode=<x>`).

    `tidy`            content, finish, usage, `[DONE]`, terminating chunk -- the dominant shape;
    `nousage`         content, `[DONE]`, terminating chunk -- clean end, no usage frame;
    `emptydone`       `[DONE]` and nothing else;
    `usagethenrst`    content, finish, usage, then a reset instead of `[DONE]`;
    `nothingthenrst`  a reset with the headers sent and no frame at all.

    A non-streamed request is answered as an ordinary JSON completion, so the row the OTHER route
    writes can be compared with the streamed one's.

    THE RESET HAS TO BE OURS: `socketserver.TCPServer.shutdown_request` sends a FIN before closing,
    so a linger-0 left for the server to act on reaches the proxy as a clean EOF. The handler closes
    the socket itself, after a pause long enough for the proxy to have consumed what was sent (a
    reset discards unread bytes).
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _chunk(self, raw: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
        self.wfile.flush()

    def _frame(self, payload: dict) -> None:
        self._chunk(b"data: " + json.dumps(payload).encode() + b"\n\n")

    def _rst(self) -> None:
        time.sleep(0.3)
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.connection.close()
        self.close_connection = True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        mode = body["messages"][-1]["content"].split("mode=", 1)[1].split()[0]
        if not body.get("stream"):
            raw = json.dumps({
                "id": COMPLETION_ID, "object": "chat.completion", "model": GATEWAY_MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if mode == "nothingthenrst":
            self._rst()
            return
        if mode != "emptydone":
            for text in CONTENT:
                self._frame({"id": COMPLETION_ID, "model": GATEWAY_MODEL,
                             "choices": [{"index": 0, "delta": {"content": text}}]})
        if mode in ("tidy", "usagethenrst"):
            self._frame({"id": COMPLETION_ID, "model": GATEWAY_MODEL,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self._frame({"id": COMPLETION_ID, "model": GATEWAY_MODEL, "choices": [],
                         "usage": {"prompt_tokens": 17, "completion_tokens": 5,
                                   "total_tokens": 22}})
        if mode == "usagethenrst":
            self._rst()
            return
        if mode not in ("tidy", "nousage", "emptydone"):
            raise AssertionError(mode)
        self._chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture()
def meter(tmp_path, monkeypatch):
    """(proxy_port, log_path) -- the real proxy in front of the fake gateway, on private ports."""
    # The stream ADAPTER is a different route with its own tests; this file is about the SSE one.
    monkeypatch.delenv("METER_STREAM_ADAPT", raising=False)
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    srv = proxy.Server(("127.0.0.1", 0), proxy.Handler)
    srv.upstream = f"http://127.0.0.1:{up.server_port}/v1"
    srv.api_key = ""
    srv.timeout = 10.0
    srv.max_retries = 0
    price = tmp_path / "pricing.json"
    price.write_text(json.dumps({
        "source": "test", "fetched_at": "now", "cost_basis": "imputed",
        "default": {"input_per_token": RATE_IN, "output_per_token": RATE_OUT},
        "models": {PRICED_MODEL: {"input_per_token": RATE_IN, "output_per_token": RATE_OUT}}}),
        encoding="utf-8")
    srv.pricing = proxy.Pricing(str(price))
    srv.meter = proxy.Meter(tmp_path / "meter.jsonl")
    srv.limiter = proxy.RateLimiter(0)
    # No system proxy: a `$http_proxy` in the environment would turn every call into a 502.
    srv.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv, tmp_path / "meter.jsonl", f"127.0.0.1:{up.server_port}"
    finally:
        srv.shutdown()
        up.shutdown()
        up.server_close()


def _call(srv, *, mode: str, model: str = UNPRICED_MODEL, stream: bool = True) -> tuple[bytes, bool]:
    """Speak HTTP/1.1 BY HAND on a keep-alive connection: `(raw_response, the_proxy_closed_it)`.

    urllib cannot ask this question -- it sends `Connection: close`, so the socket it watches was
    going to end anyway, which is why the hang this file drives survived every urllib-based test.
    The read stops at the terminating zero-length chunk (a complete answer, connection rightly
    kept alive), at EOF (the proxy let go), or at `HOLD_TIMEOUT_S` of silence, which is the
    failure under test and the only case that waits.
    """
    payload = {"model": model, "messages": [{"role": "user", "content": f"hello mode={mode}"}]}
    if stream:
        payload["stream"] = True
    body = json.dumps(payload).encode()
    request = (b"POST /m/A/t/a1/v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               b"Content-Type: application/json\r\nConnection: keep-alive\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sock = socket.create_connection(("127.0.0.1", srv.server_port), timeout=HOLD_TIMEOUT_S)
    got, closed = b"", False
    try:
        sock.sendall(request)
        while True:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break                           # still holding the connection: NOT closed
            except ConnectionResetError:
                closed = True
                break
            if not chunk:
                closed = True
                break
            got += chunk
            if got.endswith(b"0\r\n\r\n") or b"\r\n\r\n" in got and _content_length_done(got):
                break
    finally:
        sock.close()
    return got, closed


def _content_length_done(raw: bytes) -> bool:
    """True once a `Content-Length` response has been read whole (the non-streamed route)."""
    head, sep, rest = raw.partition(b"\r\n\r\n")
    if not sep:
        return False
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return len(rest) >= int(line.split(b":", 1)[1])
    return False


def _sse(raw: bytes) -> bytes:
    """The chunked body, decoded as far as it was actually delivered."""
    head, _sep, rest = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head[:200]
    out = b""
    while rest:
        size_line, sep, tail = rest.partition(b"\r\n")
        if not sep:
            break                               # the size line itself was cut
        try:
            size = int(size_line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += tail[:size]
        rest = tail[size:]
        if rest.startswith(b"\r\n"):
            rest = rest[2:]
    return out


def _events(raw: bytes) -> list:
    """The stream as a conformant client reads it: one payload per blank-line-separated event.

    A payload that does not parse fails here rather than being skipped -- an event glued to its
    neighbour is exactly the corruption `wire_tail` exists to prevent, and skipping it would read
    as a missing frame instead of a broken one.
    """
    out = []
    for block in _sse(raw).split(b"\n\n"):
        data = b"".join(line[len(b"data:"):].lstrip() for line in block.split(b"\n")
                        if line.strip().startswith(b"data:"))
        if data == b"[DONE]":
            out.append("[DONE]")
        elif data:
            out.append(json.loads(data))
    return out


def _row(log: Path) -> dict:
    for _ in range(400):
        if log.exists() and log.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.02)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1, rows
    return rows[0]


def _contents(events: list) -> list:
    return [ch["delta"]["content"] for ev in events if isinstance(ev, dict)
            for ch in ev.get("choices") or [] if (ch.get("delta") or {}).get("content")]


# -- 1. the sentinel ---------------------------------------------------------------------------

def test_a_clean_priced_stream_still_ends_on_its_sentinel(meter):
    """THE DOMINANT PATH, and the one that lost the terminator: usage frame, tidy end, `[DONE]`."""
    srv, log, _host = meter
    raw, _closed = _call(srv, mode="tidy")
    events = _events(raw)
    assert _contents(events) == CONTENT
    assert events[-1] == "[DONE]", events
    assert [e for e in events if e == "[DONE]"] == ["[DONE]"], "exactly one terminator"
    assert raw.endswith(b"0\r\n\r\n"), "and the chunked body is finished"
    # The usage frame is still the gateway's, priced on its way past.
    priced = [e for e in events if isinstance(e, dict) and isinstance(e.get("usage"), dict)]
    assert priced and priced[-1]["usage"]["cost_basis"] == "imputed-default-fallback"
    assert _row(log)["metered"] is True


def test_an_estimated_stream_sends_the_sentinel_exactly_once(meter):
    """The estimate's own ending already carries `[DONE]`; the re-emit must not add a second one,
    and the answer must not be marked cut -- this stream reached its terminator."""
    srv, log, _host = meter
    raw, _closed = _call(srv, mode="nousage")
    events = _events(raw)
    assert _contents(events) == CONTENT
    assert [e for e in events if e == "[DONE]"] == ["[DONE]"], events
    assert events[-1] == "[DONE]"
    priced = events[-2]
    assert priced["choices"] == [] and priced["usage"]["cost_basis"] == "estimated_from_deltas"
    cut = events[-3]
    assert "finish_reason" not in cut["choices"][0], "a stream that saw `[DONE]` was not cut"
    row = _row(log)
    assert row["cost_basis"] == "estimated_from_deltas" and row["cost"] > 0
    assert row.get("stream_aborted") is None


def test_a_stream_carrying_only_the_sentinel_still_delivers_it(meter):
    """The third exit: nothing to price, nothing minted -- and the terminator is still not ours
    to eat."""
    srv, log, _host = meter
    raw, _closed = _call(srv, mode="emptydone")
    assert _events(raw) == ["[DONE]"]
    assert raw.endswith(b"0\r\n\r\n")
    row = _row(log)
    assert row["metered"] is False and row["deltas_seen"] == 0
    assert row["note"] == "streamed response carried no usage frame and no deltas"


# -- 2. the socket -----------------------------------------------------------------------------

def test_a_midstream_death_lets_the_keepalive_socket_go(meter):
    """THE HANG, DRIVEN: the gateway resets after its usage frame, and a pooled client must get an
    EOF now rather than an unfinished chunked body it waits on for its own stall timeout."""
    srv, log, _host = meter
    raw, closed = _call(srv, mode="usagethenrst")
    assert closed is True, "the proxy held a keep-alive connection open after the stream died"
    # Everything the gateway did send was still forwarded, and is still readable.
    assert _contents(_events(raw)) == CONTENT
    row = _row(log)
    assert row["metered"] is True, "the gateway closed the books; the death does not re-price it"
    assert row["error"]


def test_a_death_before_any_frame_also_lets_the_socket_go(meter):
    """Headers are sent as soon as the upstream answers, so even a stream that produced nothing
    leaves a client waiting on a body that will never come."""
    srv, log, _host = meter
    raw, closed = _call(srv, mode="nothingthenrst")
    assert closed is True
    assert raw.startswith(b"HTTP/1.1 200")
    row = _row(log)
    assert row["metered"] is False and row["deltas_seen"] == 0
    assert row["error"]


# -- 3. the row --------------------------------------------------------------------------------

def test_a_streaming_row_names_the_call_the_rate_and_the_endpoint(meter):
    """`id` and the model the GATEWAY reported (not the one the request asked for), the price
    table's own answer, and which upstream priced it."""
    srv, log, host = meter
    _call(srv, mode="tidy", model=UNPRICED_MODEL)
    row = _row(log)
    assert row["id"] == COMPLETION_ID
    assert row["model"] == UNPRICED_MODEL and row["model_reported"] == GATEWAY_MODEL
    assert row["upstream_host"] == host
    assert row["rate_basis"] == "imputed-default-fallback", "an unpriced model says so"
    assert row["cost"] == pytest.approx(17 * RATE_IN + 5 * RATE_OUT)


def test_a_pinned_model_is_not_confusable_with_the_default_rate(meter):
    """The other half of the same field: priced off the table's own row, and it says which."""
    srv, log, _host = meter
    _call(srv, mode="tidy", model=PRICED_MODEL)
    assert _row(log)["rate_basis"] == "imputed"


def test_the_estimate_names_its_rate_in_the_row_and_in_the_frame(meter):
    """The cut rows are the ones that most need it, and the client's accountant never sees the
    log -- so the fallback is stated in the synthesized usage frame too."""
    srv, log, _host = meter
    raw, _closed = _call(srv, mode="nousage", model=UNPRICED_MODEL)
    usage = [e for e in _events(raw) if isinstance(e, dict) and isinstance(e.get("usage"), dict)]
    assert usage[-1]["usage"]["meter_rate_basis"] == "imputed-default-fallback"
    row = _row(log)
    assert row["rate_basis"] == "imputed-default-fallback"
    assert row["cost_basis"] == "estimated_from_deltas", "the two are different questions"
    assert row["id"] == COMPLETION_ID and row["model_reported"] == GATEWAY_MODEL


def test_the_non_streaming_route_names_the_endpoint_too(meter):
    """Two meters sharing one `meter.jsonl` interleave non-streamed calls just as readily."""
    srv, log, host = meter
    raw, _closed = _call(srv, mode="tidy", stream=False)
    assert b'"content": "hi"' in raw or b'"content":"hi"' in raw
    row = _row(log)
    assert row["stream"] is False and row["upstream_host"] == host
    assert row["id"] == COMPLETION_ID and row["model_reported"] == GATEWAY_MODEL


@pytest.mark.parametrize("upstream,expected", [
    ("https://openrouter.ai/api/v1", "openrouter.ai"),
    ("http://127.0.0.1:8801/v1", "127.0.0.1:8801"),
    ("https://key:secret@llm-core-olap.samokat.ru:443/v1", "llm-core-olap.samokat.ru:443"),
    ("", ""),
    ("not a url", ""),
])
def test_the_endpoint_identity_keeps_the_port_and_drops_the_credentials(upstream, expected):
    """A port is what tells two instances on one host apart; a credential in a URL is still a
    credential in a file this proxy appends to on every call."""
    assert proxy._upstream_host(upstream) == expected
