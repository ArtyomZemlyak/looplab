"""A call that failed after N absorbed 429s must not report that it was tried once.

THE DEFECT. `_open_upstream` counts its own attempts and its own backoff sleeps and returns them
beside the response. Both callers bind those counts ONLY on the success path:

    attempts, queued_s = 1, 0.0                       # <- the value that survives a failure
    try:
        resp, attempts, queued_s = self._open_upstream(req)
    except urllib.error.HTTPError as exc:             # <- tuple assignment never ran
        ...
    row = {..., "attempts": attempts, "queued_s": round(queued_s, 2)}

So a request the proxy retried five times before giving up is written down as `attempts: 1,
queued_s: 0.0`. That is not a missing field, it is a false one, and it is false about exactly the
calls a retry counter exists to count. The streaming caller has the other half of the same bug: it
updates the row INSIDE the `try`, so a failure leaves both fields absent altogether.

MEASURED, not supposed. In `meter/meter-gemini.jsonl` on 2026-08-27, the qwen38f probe's failures
split into rows carrying `attempts: 1, queued_s: 0.0` and rows carrying neither -- the two shapes
above -- while the same run's SUCCESSFUL rows show attempts up to 6 and 60.0s of absorbed backoff.
Anyone summing that ledger to ask "how much of this run was spent being throttled" reads a floor
that excludes the worst offenders.

WHY THIS MATTERS BEYOND TIDINESS: the 429-absorbing loop exists so that neither framework's private
retry policy becomes part of the measurement. Its own record of what it absorbed is the only
evidence that it did its job. A count that resets to 1 on failure cannot be told apart from a
gateway that never throttled at all.
"""
from __future__ import annotations

import importlib.util
import json
import socket
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _AlwaysThrottles(BaseHTTPRequestHandler):
    """Answers 429 to everything, with no `Retry-After`, so the proxy falls back to its own
    `min(2 ** attempts, 30)` backoff -- 2s then 4s for the two retries this test allows."""

    protocol_version = "HTTP/1.1"
    hits = 0

    def log_message(self, *_args):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).hits += 1
        body = b'{"error":{"message":"slow down","code":429}}'
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def throttled(tmp_path):
    """(post, rows, upstream) — the real proxy in front of an upstream that only ever throttles."""
    _AlwaysThrottles.hits = 0
    up = ThreadingHTTPServer(("127.0.0.1", 0), _AlwaysThrottles)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()

    log = tmp_path / "meter.jsonl"
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "source": "test", "fetched_at": "now", "cost_basis": "imputed",
        "default": {"input_per_token": 1e-6, "output_per_token": 2e-6}, "models": {}}),
        encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), "--port", str(port), "--host", "127.0.0.1",
         "--upstream", f"http://127.0.0.1:{up.server_port}/v1", "--api-key", "k",
         "--log", str(log), "--pricing", str(pricing), "--rpm", "0", "--timeout", "30",
         "--max-retries", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("proxy did not come up")

    def post(stream: bool):
        body = json.dumps({"model": "m", "stream": stream,
                           "messages": [{"role": "user", "content": "x"}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/m/T/task/a1/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except urllib.error.HTTPError as exc:
            exc.read()

    def rows():
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]

    try:
        yield post, rows
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        up.shutdown()


@pytest.mark.parametrize("stream", [False, True])
def test_a_failed_call_reports_every_attempt_it_actually_made(throttled, stream):
    """Three attempts upstream, and the row must say three.

    `--max-retries 2` means attempt 1, retry, retry, then give up: the guard is
    `attempts > max_retries`, so the raise happens on attempt 3. The upstream counts the same
    three, which is what makes this an assertion about behaviour and not about arithmetic.
    """
    post, rows = throttled
    post(stream=stream)

    ledger = [r for r in rows() if r.get("task") == "task"]
    assert len(ledger) == 1, f"expected one row, got {ledger}"
    row = ledger[0]

    assert row.get("status") == 429
    assert _AlwaysThrottles.hits == 3, f"upstream saw {_AlwaysThrottles.hits} attempts"
    assert row.get("attempts") == 3, f"row claims {row.get('attempts')} attempt(s)"
    # 2s + 4s of backoff were really slept; the field must not read 0.
    assert float(row.get("queued_s") or 0) >= 5.5, f"row claims {row.get('queued_s')}s waited"
