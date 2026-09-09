"""Guard (driven over a real socket): the endpoint probe goes through the env's egress proxy.

The sibling property for the meter is `tests/test_meter_uses_the_egress_proxy_the_env_names.py`.
This drives `one_call` itself, so it asserts the proxy SAW the request rather than pinning source.
"""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEN: list[str] = []

_ANSWER = {"id": "1", "model": "m", "object": "chat.completion",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}


class _EgressProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        SEEN.append(self.path)                    # an absolute URL => it came through the proxy
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _probe_module():
    path = ROOT / "benchmarks" / "meter" / "probe_endpoint.py"
    spec = importlib.util.spec_from_file_location("probe_endpoint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_probe_calls_through_the_proxy_the_environment_names(monkeypatch):
    SEEN.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EgressProxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        out = _probe_module().one_call("http://endpoint.invalid/v1", "k", "m", 8, 10.0)
    finally:
        server.shutdown()
    assert out.get("ok"), out
    assert SEEN and SEEN[0].startswith("http://endpoint.invalid/"), (
        "the probe must reach the endpoint through the env's egress proxy — the campaign it "
        f"chooses the endpoint for does. saw: {SEEN}")
