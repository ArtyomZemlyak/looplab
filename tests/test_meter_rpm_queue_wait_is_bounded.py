"""The meter's RPM queue may not hold a request longer than its client will wait for it.

THE MEASUREMENT (doc 56 §170, 2026-09-03, 26,004 ledger rows). `RateLimiter.acquire` is a 60 s
sliding window at `--rpm 45` and it BLOCKS before the upstream request is opened. Both arms' clients
give up on a first byte at 45 s (`llm_header_timeout = 45.0` in every probe's config snapshot).
39 requests waited more than 0.5 s in that queue and 23 came back an EMPTY 200 -- 59 %, against
0.0154 % of the 25,968 that did not wait; 23 of the 27 empty 200s in the corpus had queued. The
shape: a client abandons the socket at 45 s, the proxy wins its slot at 60 s, opens the upstream,
spends the slot, streams nothing to nobody and writes a clean-looking row.

THE FIX. `--rpm-max-wait` (default 40 s, under the 45 s window) bounds the queue: a request the
window cannot admit within the bound is answered 429 AT ONCE -- on the projection, since the
earliest admission is when the oldest stamp leaves the window and there is nothing to wait for
past that -- and the refusal is a row of its own `kind`, because its status is the same 429 an
upstream throttle answers and `check_money` keys on status.

Driven two ways: the limiter's own truth table under a fake clock, and the real proxy in a real
subprocess in front of a real upstream, which must see exactly one request.
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

_SPEC = importlib.util.spec_from_file_location("_meter_proxy_rpm_bound", PROXY)
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)


# -- the bound itself ---------------------------------------------------------------------------

def test_the_default_bound_is_under_the_clients_first_byte_window():
    """40 s against 45 s: the whole point is that the proxy answers before the client leaves."""
    from looplab.core.config import Settings
    header_timeout = float(Settings.model_fields["llm_header_timeout"].default)
    assert header_timeout == 45.0, "doc 56 §170's premise moved; re-measure the bound against it"
    assert 0 < proxy.RPM_MAX_WAIT_DEFAULT < header_timeout
    assert proxy.RateLimiter(45).max_wait == proxy.RPM_MAX_WAIT_DEFAULT


class _Clock:
    """`time.time` / `time.sleep` for the limiter, so a 35 s wait costs no wall clock and a
    competitor can be made to win the slot during a sleep."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept: list = []
        self.on_sleep = None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(proxy.time, "time", c.time)
    monkeypatch.setattr(proxy.time, "sleep", c.sleep)
    return c


def test_a_wait_past_the_bound_is_refused_at_once_on_the_projection(clock):
    lim = proxy.RateLimiter(1, max_wait=40.0)
    assert lim.acquire() == 0.0                 # the window admits the first
    with pytest.raises(proxy.RpmQueueRefused) as err:
        lim.acquire()                           # the second needs the full 60 s: past the bound
    assert clock.slept == [], "a refusal must not hold the request first"
    assert err.value.waited == 0.0
    assert err.value.retry_after == pytest.approx(60.0)
    assert err.value.max_wait == 40.0
    assert lim.refused == 1


def test_a_wait_within_the_bound_is_served_and_counted(clock):
    lim = proxy.RateLimiter(1, max_wait=40.0)
    lim.acquire()
    clock.now += 25.0                           # the slot opens in 35 s: inside the bound
    assert lim.acquire() == pytest.approx(35.0)
    assert sum(clock.slept) == pytest.approx(35.0)
    assert lim.waited_s == pytest.approx(35.0) and lim.refused == 0


def test_zero_restores_the_unbounded_queue(clock):
    """`--rpm-max-wait 0`: the historical behaviour, a full minute held if that is what it takes."""
    lim = proxy.RateLimiter(1, max_wait=0)
    lim.acquire()
    assert lim.acquire() == pytest.approx(60.0)
    assert lim.refused == 0


def test_a_request_that_reached_the_bound_under_contention_is_refused_with_its_wait(clock):
    """The projection is exact for one waiter and a FLOOR under contention: every waiter re-checks
    the window on its own clock, so a slot that opened can be taken by another. A request that has
    spent the whole bound in the queue is refused on the next check, and the row says how long."""
    lim = proxy.RateLimiter(1, max_wait=5.0)
    lim.acquire()
    clock.now += 55.0                           # projected 5 s: exactly the bound, admitted to wait

    def competitor_wins():
        lim._stamps = [clock.now]               # someone else took the slot that just opened
    clock.on_sleep = competitor_wins
    with pytest.raises(proxy.RpmQueueRefused) as err:
        lim.acquire()
    assert err.value.waited == pytest.approx(5.0)
    assert lim.waited_s == pytest.approx(5.0) and lim.refused == 1


# -- the proxy, end to end ---------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Upstream(BaseHTTPRequestHandler):
    """A normal completion, counting what reaches it: the refused request must NOT."""

    protocol_version = "HTTP/1.1"
    hits = 0

    def log_message(self, *_args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).hits += 1
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for f in ({"id": "c", "choices": [{"index": 0, "delta": {"content": "ok"},
                                                "finish_reason": "stop"}]},
                      {"id": "c", "choices": [], "usage": usage}):
                raw = b"data: " + json.dumps(f).encode() + b"\n\n"
                self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            raw = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        out = json.dumps({"id": "c", "object": "chat.completion", "model": "m",
                          "choices": [{"index": 0, "message": {"role": "assistant",
                                                                "content": "ok"},
                                       "finish_reason": "stop"}],
                          "usage": usage}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def meter(tmp_path):
    """The real proxy, `--rpm 1 --rpm-max-wait 1`: the first call takes the minute's only slot,
    so the second projects a ~60 s wait against a 1 s bound."""
    _Upstream.hits = 0
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
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
         "--log", str(log), "--pricing", str(pricing), "--rpm", "1", "--rpm-max-wait", "1",
         "--timeout", "30", "--max-retries", "2"],
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
            f"http://127.0.0.1:{port}/m/B/task/a1/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def rows():
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def health():
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            return json.loads(resp.read())

    try:
        yield post, rows, health
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        up.shutdown()


@pytest.mark.parametrize("stream", [False, True])
def test_the_second_call_is_refused_now_and_the_ledger_says_by_whom(meter, stream):
    post, rows, health = meter
    status, _h, _b = post(stream=False)
    assert status == 200

    t0 = time.time()
    status, headers, raw = post(stream=stream)
    took = time.time() - t0
    # 1. Immediately, with the endpoint's own vocabulary: a 429 the client's retry loop knows.
    assert status == 429
    assert took < 5.0, f"a refusal held the socket for {took:.1f}s"
    body = json.loads(raw)
    assert body["error"]["type"] == "meter_rpm_queue" and body["error"]["code"] == 429
    retry_after = int(headers.get("Retry-After"))
    assert 1 <= retry_after <= 60
    # 2. The upstream never saw it: the slot was not spent on a call nobody would read.
    assert _Upstream.hits == 1
    # 3. The row is this proxy's refusal, not the gateway's 429, and it is countable as such.
    ledger = rows()
    assert len(ledger) == 2, ledger
    row = ledger[1]
    assert row["status"] == 429 and row["metered"] is False
    assert row["kind"] == proxy.RPM_QUEUE_REFUSED_KIND == "rpm_queue_refused"
    assert row["refused_by"] == "meter"
    assert row["retry_after_s"] == retry_after
    assert row["stream"] is stream and row["path"] == "/chat/completions"
    assert row["arm"] == "B" and row["task"] == "task" and row["attempt"] == "a1"
    assert row["attempts"] == 1, "the refused attempt was an attempt"
    assert float(row["queued_s"]) < 1.0 and row["req_sha"]
    assert "rpm-max-wait" in row["error"]
    # 4. And the health snapshot counts it, so a watchdog can see refusals climbing.
    assert health()["rpm_refused"] == 1
    # 5. The served call carries no `kind`: the word is the refusal's alone.
    assert ledger[0]["status"] == 200 and "kind" not in ledger[0]


def test_the_option_is_on_the_command_line_and_in_the_environment():
    """`--rpm-max-wait` and `METER_RPM_MAX_WAIT`, defaulting to the bound, `0` meaning unbounded."""
    src = PROXY.read_text(encoding="utf-8")
    assert '"--rpm-max-wait"' in src and "METER_RPM_MAX_WAIT" in src
    assert proxy.RateLimiter(10, max_wait=0).max_wait == 0.0
    assert proxy.RateLimiter(10, max_wait=None).max_wait == 0.0
