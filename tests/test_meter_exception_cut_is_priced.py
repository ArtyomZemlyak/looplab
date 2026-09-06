"""A stream the UPSTREAM ends by exception is priced like one it ends by EOF -- and says which.

THE DEFECT. `_proxy_stream` forwards inside one `try`, and its `except` had two tenants that look
alike: our CLIENT hanging up (a `BrokenPipeError` out of `emit()`, five rows in the live log) and
the UPSTREAM dying under us (an RST, a socket timeout, a chunked body that ends without its
terminator). The branch that lands there deliberately declines to price the first -- the client
left, what was generated after that reached nobody -- and that reasoning was silently applied to
the second, where everything generated WAS forwarded, or collected for the adapted client, and the
provider generated and billed it. Driven 2026-08-30 with an upstream sending 3 deltas then RST: the
adapted client received status 200, the full forwarded content and a truncation mark, and the row
landed `{metered: false, cost: 0.0, deltas_seen: 3}`. A served answer priced at nothing is the
silent-under-count shape the delta estimator was built to close, live whenever the ~1800 s gateway
cut arrives as an exception instead of a clean EOF.

WHAT IS PINNED, against the real `Handler` over real sockets, for the two ways an upstream death
reaches the proxy as an exception (an RST, a read timeout) on both routes:

* the answer is served -- 200, the forwarded content, `truncated` -- and PRICED from the deltas,
  with the same `estimated_from_deltas` basis and the same two-frame ending a clean cut gets;
* the row is `metered: true`, carries the exception it would have carried unpriced, and is marked
  `stream_cut_by: "upstream_exception"` so no column can confuse it with a gateway EOF, a meter
  ceiling cut, or a client that walked away;
* the two cases the fix must NOT touch keep their bytes: an upstream that died having produced no
  delta (still a 502 on the adapted route, still unpriced), and one that died after the gateway's
  own usage frame (still the gateway's price, still an `error` on the row).

The client-hung-up case staying unpriced is `tests/test_meter_proxy_stream_rows.py`'s, unchanged.
A chunked body that merely ENDS without its terminator is deliberately not in the set: `http.client`
swallows that into a clean EOF while iterating lines (`_peek_chunked` returns `b""` on
`IncompleteRead`), so it is the existing no-usage-frame path, pinned by
`test_meter_stream_adapter.py::test_an_adapted_stream_the_gateway_cuts_still_carries_its_price`.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy_exception_cut",
    Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

RATE_IN = 1e-6
RATE_OUT = 2e-6
DELTAS = 3
PROXY_TIMEOUT_S = 1.0          # the proxy's own upstream socket timeout, for the `timeout` death


class _Upstream(BaseHTTPRequestHandler):
    """Streams `DELTAS` content frames, then dies the way the request's last message says.

    `die=rst`      close with SO_LINGER 0, so the proxy's read gets a reset;
    `die=timeout`  go silent, so the proxy's own socket timeout fires;
    `die=none`     the tidy ending, with a usage frame and `[DONE]` -- the control;
    `usage_then_rst`  a usage frame, then an RST before `[DONE]`;
    `nothing_then_rst`  an RST before any frame at all.

    THE RST HAS TO BE OURS. `socketserver.TCPServer.shutdown_request` sends `SHUT_WR` -- a FIN --
    before it closes, so a linger-0 set and left for the server to act on reaches the proxy as a
    clean EOF, not a reset. The handler closes the socket ITSELF, after a pause long enough for
    the proxy to have consumed the frames already sent (a reset discards unread bytes).
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _frame(self, payload: dict) -> None:
        raw = b"data: " + json.dumps(payload).encode() + b"\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
        self.wfile.flush()

    def _rst(self) -> None:
        time.sleep(0.3)
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.connection.close()
        self.close_connection = True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        die = body["messages"][-1]["content"].split("die=", 1)[1].split()[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if die == "nothing_then_rst":
            self._rst()
            return
        for i in range(DELTAS):
            self._frame({"id": "cmpl-x", "model": "m",
                         "choices": [{"index": 0, "delta": {"content": f"tok{i}"}}]})
        if die == "rst":
            self._rst()
        elif die == "timeout":
            time.sleep(PROXY_TIMEOUT_S * 4)
            self.close_connection = True
        elif die in ("none", "usage_then_rst"):
            self._frame({"id": "cmpl-x", "choices": [{"index": 0, "delta": {},
                                                      "finish_reason": "stop"}]})
            self._frame({"id": "cmpl-x", "choices": [],
                         "usage": {"prompt_tokens": 50, "completion_tokens": DELTAS,
                                   "total_tokens": 53}})
            if die == "usage_then_rst":
                self._rst()
            else:
                raw = b"data: [DONE]\n\n"
                self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
        else:
            raise AssertionError(die)


@pytest.fixture()
def meter(tmp_path):
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    srv = proxy.Server(("127.0.0.1", 0), proxy.Handler)
    srv.upstream = f"http://127.0.0.1:{up.server_port}/v1"
    srv.api_key = ""
    srv.timeout = PROXY_TIMEOUT_S
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


def _call(url: str, *, die: str, stream: bool) -> tuple[int, bytes]:
    payload = {"model": "m", "messages": [{"role": "user", "content": f"hello die={die}"}]}
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _events(raw: bytes) -> list:
    """The stream as a conformant client reads it: one payload per blank-line-separated event.
    A payload that does not parse is a failure here, not a skip."""
    out = []
    for block in raw.split(b"\n\n"):
        data = b"".join(l[len(b"data:"):].lstrip() for l in block.split(b"\n")
                        if l.strip().startswith(b"data:"))
        if data == b"[DONE]":
            out.append("[DONE]")
        elif data:
            out.append(json.loads(data))
    return out


def _row(log: Path) -> dict:
    for _ in range(200):
        if log.exists() and log.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.02)
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1, rows
    return rows[0]


def _assert_priced_exception_cut(row: dict, usage: dict, *, die: str) -> None:
    """What the row and the served usage must agree on, whichever way the upstream died."""
    assert row["metered"] is True and row["cost_basis"] == "estimated_from_deltas", row
    assert row["completion_tokens"] == DELTAS == row["deltas_seen"]
    assert row["cost"] == pytest.approx(DELTAS * RATE_OUT + row["prompt_tokens"] * RATE_IN)
    assert row["cost"] > 0
    # The mark: an abort, cut by the upstream's socket, with the exception still on the row.
    assert row["stream_aborted"] is True
    assert row["stream_cut_by"] == "upstream_exception"
    assert row["error_source"] == "upstream"
    assert row["error"] and row["error"] in row["note"]
    if die == "timeout":
        assert "timed out" in row["error"].lower() or "timeout" in row["error"].lower(), row
    else:
        assert "reset" in row["error"].lower(), row
    assert "FLOOR" in row["note"] and "served" in row["note"]
    # And the client's copy says the same thing in its own fields.
    assert usage["cost_basis"] == "estimated_from_deltas"
    assert usage["completion_tokens"] == DELTAS
    assert usage["cost"] == pytest.approx(row["cost"])
    assert usage["meter_cut_by"] == "upstream_exception"
    assert usage["meter_upstream_error"] == row["error"]
    assert "FLOOR" in usage["meter_note"]


@pytest.mark.parametrize("die", ["rst", "timeout"])
def test_the_adapted_client_gets_a_priced_truncated_answer(meter, monkeypatch, die):
    """THE DEFECT AS DRIVEN: 3 deltas, then the upstream dies; the reassembled 200 must carry
    a price, not a null usage."""
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    url, log = meter
    status, raw = _call(url, die=die, stream=False)
    assert status == 200
    body = json.loads(raw)
    assert body["choices"][0]["message"]["content"] == "tok0tok1tok2"
    assert body["choices"][0]["finish_reason"] == proxy.STREAM_TRUNCATED_FINISH_REASON
    assert body["id"] == "cmpl-x", "the real completion's id must survive the minted ending"
    _assert_priced_exception_cut(_row(log), body["usage"], die=die)


@pytest.mark.parametrize("die", ["rst", "timeout"])
def test_the_streaming_client_gets_the_same_ending_a_clean_cut_gets(meter, monkeypatch, die):
    """The streamed route: the forwarded deltas, then the cut frame, the priced frame and the
    `[DONE]` sentinel -- one door for every cut -- and a body that ENDS, so a pooled client is not
    left holding a half-finished chunked response."""
    monkeypatch.delenv("METER_STREAM_ADAPT", raising=False)
    url, log = meter
    status, raw = _call(url, die=die, stream=True)
    assert status == 200
    events = _events(raw)                       # a glued or unterminated event fails to parse here
    contents = [c["delta"].get("content") for e in events if isinstance(e, dict)
                for c in e.get("choices") or [] if c.get("delta", {}).get("content")]
    assert contents == ["tok0", "tok1", "tok2"]
    assert events[-1] == "[DONE]"
    cut = events[-3]
    assert cut["choices"][0]["finish_reason"] == proxy.STREAM_TRUNCATED_FINISH_REASON
    priced = events[-2]
    assert priced["choices"] == [] and isinstance(priced.get("usage"), dict)
    _assert_priced_exception_cut(_row(log), priced["usage"], die=die)


def test_an_upstream_that_died_before_any_delta_is_still_a_502_and_unpriced(meter, monkeypatch):
    """Nothing was served, so nothing is priced: the historical path, byte-for-byte."""
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    url, log = meter
    status, raw = _call(url, die="nothing_then_rst", stream=False)
    assert status == 502
    assert json.loads(raw)["error"]["type"] == "meter_stream_failed"
    row = _row(log)
    assert row["metered"] is False and not row.get("cost")
    assert row["deltas_seen"] == 0 and row.get("stream_cut_by") is None
    assert row["error"] and "nothing was priced" in row["note"]


def test_a_death_after_the_gateways_own_usage_frame_keeps_the_gateways_price(meter, monkeypatch):
    """The books were closed by the provider; an RST before `[DONE]` is a transport fact and the
    row records it as one, without re-pricing what was already priced."""
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    url, log = meter
    status, raw = _call(url, die="usage_then_rst", stream=False)
    assert status == 200
    body = json.loads(raw)
    assert body["usage"]["prompt_tokens"] == 50
    assert body["usage"]["cost_basis"].startswith("imputed")
    assert body["choices"][0]["finish_reason"] == proxy.STREAM_TRUNCATED_FINISH_REASON
    row = _row(log)
    assert row["metered"] is True and row["cost_basis"].startswith("imputed")
    assert row["prompt_tokens"] == 50 and row["completion_tokens"] == DELTAS
    assert row["error"] and row.get("stream_cut_by") is None and "error_source" not in row


def test_the_tidy_control_is_untouched(meter, monkeypatch):
    monkeypatch.setenv("METER_STREAM_ADAPT", "1")
    url, log = meter
    status, raw = _call(url, die="none", stream=False)
    assert status == 200
    body = json.loads(raw)
    assert body["choices"][0]["finish_reason"] == "stop"
    row = _row(log)
    assert row["metered"] is True and row["cost_basis"].startswith("imputed")
    assert "error" not in row and row.get("stream_aborted") is None
