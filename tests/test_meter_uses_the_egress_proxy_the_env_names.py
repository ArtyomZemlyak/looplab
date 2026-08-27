"""The meter force-disabled proxying for every upstream, and one of them is outside the perimeter.

THE DEFECT. `proxy.py` built its opener with `ProxyHandler({})` — an EMPTY mapping, which does not
mean "use the environment", it means "never proxy anything". That was right for the corporate
gateway, which lives inside the perimeter. The second instance of the same file points at
`openrouter.ai`, which does not, and inherited the override: every call went around the box's
egress proxy.

MEASURED 2026-08-27 against the real endpoint with NO credentials, eight attempts per cell:

    curl   via 127.0.0.1:18080   0/8 blocked
    urllib via 127.0.0.1:18080   0/8 blocked
    curl   direct                1/8 blocked
    urllib direct                2/8 blocked

A blocked request answers `403 {"success": false, "error": "Access denied by security policy."}` —
not OpenRouter's error shape, and it arrives for a request carrying no key at all, so it is neither
the key nor the account. Sustained direct traffic makes it worse: the refusal rate on the live
meter climbed 13% -> 25% -> 48% -> 72% -> ~100% over one day and stalled two $1 probes at a fifth
of their budget.

THE ENVIRONMENT ALREADY SPELLS THE RULE: `no_proxy` names the corporate gateway, so
`proxy_bypass('llm-core-olap.samokat.ru')` is True while `proxy_bypass('openrouter.ai')` is False.
`ProxyHandler()` with no argument reads exactly that. The fix is to stop overriding it.

This test drives the REAL meter over a REAL socket at a REAL fake proxy, and asserts the proxy saw
the request — not that the source contains a particular call.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "benchmarks" / "meter" / "proxy.py"

_ANSWER = {
    "id": "1", "model": "m", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}

SEEN: list[str] = []


class _EgressProxy(BaseHTTPRequestHandler):
    """A forward proxy: the request line carries the ABSOLUTE url. Records and answers it."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        SEEN.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def metered(tmp_path):
    SEEN.clear()
    prox = ThreadingHTTPServer(("127.0.0.1", 0), _EgressProxy)
    prox.daemon_threads = True
    threading.Thread(target=prox.serve_forever, daemon=True).start()

    log = tmp_path / "meter.jsonl"
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "source": "t", "fetched_at": "now", "cost_basis": "imputed",
        "default": {"input_per_token": 1e-6, "output_per_token": 2e-6}, "models": {}}),
        encoding="utf-8")
    port = _free_port()
    # An upstream that does NOT exist: if the meter dials it directly the call fails, and if it
    # honours the environment the fake proxy answers. No ambiguity about which path was taken.
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path),
        "http_proxy": f"http://127.0.0.1:{prox.server_port}",
        "https_proxy": f"http://127.0.0.1:{prox.server_port}",
        "no_proxy": "",  # nothing bypasses, so the assertion is about the meter, not about no_proxy
    }
    proc = subprocess.Popen(
        [sys.executable, str(PROXY), "--port", str(port), "--host", "127.0.0.1",
         "--upstream", "http://198.51.100.7:9/v1",  # TEST-NET-2, cannot answer
         "--api-key", "k", "--log", str(log), "--pricing", str(pricing),
         "--rpm", "0", "--timeout", "20"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("meter did not come up")
    try:
        yield port, log
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        prox.shutdown()


def test_an_outside_upstream_is_reached_through_the_environment_s_proxy(metered):
    port, log = metered
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x"}]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/m/T/task/a1/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()

    assert SEEN, ("the meter dialled the upstream directly; the egress proxy the environment names "
                  "never saw the request")
    assert SEEN[0].startswith("http://198.51.100.7:9/"), SEEN
