"""An upstream error whose body is not a JSON OBJECT still lands in the ledger and on the client.

THE DEFECT. On the non-streaming route the error branch read `json.loads(raw).get("error")` under
`except ValueError`. `json.loads(b'"internal error"')` -- a JSON string; an array and a bare number
are the same shape, and a busy gateway's own edge really produces them -- parses fine, and `.get`
on the result raises `AttributeError`, which `except ValueError` does not catch. The exception
escaped the handler BEFORE `meter.record(row)` and before any response: the paid request VANISHED
from the ledger (the one thing the meter says it must never do) and the client got a bare
connection drop instead of the legible 500, which the arms' retry loops classify differently.
Driven 2026-08-30: upstream 500 with body `"internal error"` -> client `RemoteDisconnected`, rows
recorded: 0.

Driven here against the real `Handler` over a real socket, for each non-object shape, plus the
object shape whose behaviour must not move.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_meter_proxy_error_shape", Path(__file__).resolve().parents[1] / "benchmarks/meter/proxy.py")
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)


class _Upstream(BaseHTTPRequestHandler):
    """Answers 500 with whatever bytes the request's last message names after `body=`."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        raw = body["messages"][-1]["content"].split("body=", 1)[1].encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


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
        "default": {"input_per_token": 1e-6, "output_per_token": 2e-6}, "models": {}}),
        encoding="utf-8")
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


def _call(url: str, body: str) -> tuple[int, bytes]:
    """`(status, body)` as the client sees them. A dropped connection raises out of here, which is
    the failure this file exists to make impossible."""
    payload = {"model": "m", "messages": [{"role": "user", "content": "body=" + body}]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _rows(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.mark.parametrize("body", ['"internal error"', '["internal", "error"]', "500", "null"])
def test_a_non_object_error_body_is_recorded_and_relayed(meter, body):
    url, log = meter
    status, raw = _call(url, body)
    # 1. The client gets the upstream's status and its bytes, not a connection drop.
    assert status == 500
    assert raw == body.encode()
    # 2. The call is in the ledger, as an unmetered 500 carrying the body as text.
    rows = _rows(log)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["status"] == 500 and row["metered"] is False
    assert row["upstream_error"] == body
    assert row["arm"] == "A" and row["task"] == "t" and row["attempt"] == "a1"


def test_an_object_error_body_still_carries_its_error_member(meter):
    """The shape that always worked, unchanged: the object's `error` member, not its text."""
    url, log = meter
    status, _raw = _call(url, '{"error": {"message": "boom", "code": 500}}')
    assert status == 500
    row = _rows(log)[0]
    assert row["upstream_error"] == {"message": "boom", "code": 500}
    assert row["metered"] is False


def test_an_unparseable_error_body_is_carried_as_text(meter):
    """And the third shape -- not JSON at all, nginx's HTML say -- is text, as it always was."""
    url, log = meter
    status, raw = _call(url, "<html>Gateway Time-out</html>")
    assert status == 500 and raw.startswith(b"<html>")
    row = _rows(log)[0]
    assert row["upstream_error"] == "<html>Gateway Time-out</html>"
