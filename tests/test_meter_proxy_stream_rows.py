"""A meter row must not contradict the field beside it.

The metering proxy is the ONE ruler both arms are priced by, so every row it writes is evidence
about a campaign's spend. Two rows in the live log say two different things at once:

    {"deltas_seen": 3149, "metered": false, "cost": 0.0,
     "note": "streamed response carried no usage frame and no deltas",
     "error": "BrokenPipeError: [Errno 32] Broken pipe"}

3,149 deltas, and the sentence says there were none. The cause is structural rather than a typo:
`_proxy_stream` synthesises its `estimated_from_deltas` usage frame INSIDE the `try` that forwards
the stream, so any exception while forwarding skips the synthesis and drops through to the
"nothing was produced" branch -- carrying a delta count that falsifies it. Five such rows are in
`/var/tmp/looplab-bench/meter/meter.jsonl`, two of them with deltas.

Whether those tokens should be PRICED is a separate question this file deliberately does not
answer (the client left; the provider still generated and still billed), and the `metered=false`
/ `cost=0.0` half is asserted unchanged. What is fixed is the row lying about itself.

Driven against the REAL proxy over a REAL socket, reproducing the recorded cause exactly: a fake
upstream streams content deltas and the CLIENT hangs up part-way through, so `emit()` raises
`BrokenPipeError` after the counter has already seen several. Nothing here touches the campaign's
meter: its own port, its own log file, and `--rpm 0`.
"""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "benchmarks" / "meter" / "proxy.py"

_FRAME = b'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n'


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Upstream(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    deltas = 5
    pace = 0.0              # seconds between frames; >0 keeps the stream open long enough to
                            # notice a client that walked away


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _ in range(self.server.deltas):
            self.wfile.write(b"%x\r\n" % len(_FRAME) + _FRAME + b"\r\n")
            try:
                self.wfile.flush()
            except OSError:                     # the proxy hung up because its own client did
                return
            if self.server.pace:
                time.sleep(self.server.pace)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture
def meter(tmp_path):
    """(post, log_path) — the real proxy in front of a fake upstream, on private ports."""
    up_port, proxy_port = _free_port(), _free_port()
    upstream = _Upstream(("127.0.0.1", up_port), _Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    log = tmp_path / "meter.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), "--port", str(proxy_port), "--host", "127.0.0.1",
         "--upstream", f"http://127.0.0.1:{up_port}/v1", "--api-key", "k",
         "--log", str(log), "--rpm", "0", "--timeout", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/healthz", timeout=1).read()
            break
        except Exception:                       # noqa: BLE001 - waiting for the listener
            time.sleep(0.05)
    else:                                       # pragma: no cover - the proxy never came up
        proc.kill()
        upstream.shutdown()
        pytest.fail("meter proxy did not start: " + (proc.stdout.read() if proc.stdout else ""))

    body = json.dumps({"model": "deepseek-v4-flash", "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = (b"POST /m/B/testtask/v1/chat/completions HTTP/1.1\r\n"
               b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
               + body)

    def post(hang_up_after: int | None = None):
        """Speak HTTP by hand, because HANGING UP MID-STREAM is the thing under test.

        `hang_up_after` bytes read, then the socket is closed with SO_LINGER 0 so the peer gets an
        RST -- which is what a client process that died looks like to this proxy.
        """
        sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=30)
        try:
            sock.sendall(request)
            got = b""
            while hang_up_after is None or len(got) < hang_up_after:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                got += chunk
            if hang_up_after is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
            return got
        finally:
            sock.close()

    try:
        yield post, log, upstream
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:       # pragma: no cover
            proc.kill()
        upstream.shutdown()
        upstream.server_close()


def _rows(log: Path) -> list[dict]:
    for _ in range(100):
        if log.exists() and log.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.05)
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _broken_pipe_row(post, log, upstream) -> dict:
    upstream.deltas = 400
    upstream.pace = 0.005
    post(hang_up_after=1)
    rows = _rows(log)
    assert len(rows) == 1, rows
    row = rows[0]
    if not row.get("error"):
        pytest.skip("the client hang-up did not reach the proxy as a write error on this kernel")
    return row


def test_a_row_never_claims_no_deltas_while_counting_some(meter):
    """THE CONTRADICTION, driven end to end: real proxy, real socket, real client hang-up."""
    post, log, upstream = meter
    row = _broken_pipe_row(post, log, upstream)
    assert row["deltas_seen"] > 0, row
    assert "no deltas" not in row["note"], row
    assert f"{row['deltas_seen']} forwarded delta(s)" in row["note"], row


def test_the_unpriced_rule_is_unchanged_on_that_row(meter):
    """`metered=false` and `cost` absent-or-zero: "unpriced, and recorded as unpriced -- never $0"
    is the module's rule, and naming the deltas honestly does not quietly start pricing them."""
    post, log, upstream = meter
    row = _broken_pipe_row(post, log, upstream)
    assert row["metered"] is False, row
    assert not row.get("cost"), row
    assert row.get("cost_basis") in ("", None), row


def test_a_clean_stream_with_no_usage_frame_is_still_estimated(meter):
    """The path the estimator was added for, kept working: the gateway ends the stream tidily and
    simply never sends a usage frame."""
    post, log, upstream = meter
    post()
    row = _rows(log)[0]
    assert row["cost_basis"] == "estimated_from_deltas", row
    assert row["stream_aborted"] is True
    assert row["completion_tokens"] == 5
    assert row["cost"] > 0
    assert "FLOOR" in row["note"]


def test_a_stream_that_produced_nothing_says_exactly_that(meter):
    post, log, upstream = meter
    upstream.deltas = 0
    post()
    row = _rows(log)[0]
    assert row["deltas_seen"] == 0
    assert row["metered"] is False
    assert row["note"] == "streamed response carried no usage frame and no deltas"
