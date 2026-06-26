"""I2: OpenAI-compatible client against a mock HTTP server (offline, deterministic).
Verifies tool-call parsing, the text/JSON fallback path, and <think> stripping."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from looplab.llm import OpenAICompatibleClient
from looplab.models import Idea
from looplab.parse import parse_structured


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        if "tools" in body:
            args = json.dumps({"operator": "improve", "params": {"x": 3.0, "y": -1.0}, "rationale": "r"})
            resp = {"choices": [{"message": {"role": "assistant",
                    "tool_calls": [{"function": {"name": "emit", "arguments": args}}]}}], "usage": {}}
        else:
            # reasoning-model style: <think> with braces, then the JSON answer
            content = '<think>let me {consider} options</think> Here: {"operator": "draft", "params": {"x": 1.0}}'
            resp = {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def base_url():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()


def test_tool_call_path(base_url):
    c = OpenAICompatibleClient("m", base_url=base_url)
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "tool_call")
    assert idea.operator == "improve" and idea.params == {"x": 3.0, "y": -1.0}


def test_text_fallback_strips_think(base_url):
    c = OpenAICompatibleClient("m", base_url=base_url)
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "baml")
    assert idea.operator == "draft" and idea.params == {"x": 1.0}


# --- transient-timeout resilience: a slow/unresponsive endpoint is retried, not fatal ----------
class _OkResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                           "usage": {}}).encode()


def test_post_retries_transient_timeout(monkeypatch):
    """A momentary socket timeout (e.g. Ollama reloading a model) must be retried with backoff and
    then succeed — a single slow response must not abort a long unattended run."""
    import looplab.llm as llm
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("timed out")        # first two attempts time out
        return _OkResp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)   # skip real backoff
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3                                     # retried twice, succeeded on the 3rd


def test_post_raises_after_exhausting_timeouts(monkeypatch):
    """A persistently dead endpoint still surfaces a clean LLMError once retries are exhausted."""
    import looplab.llm as llm

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])


def test_post_fails_fast_on_connection_refused(monkeypatch):
    """A refused connection (endpoint down / wrong base_url) is a STEADY-STATE failure: it must raise
    immediately WITHOUT retry/backoff, so /api/llm/health stays instant and a misconfig surfaces on
    the first call. Only timeouts are retried."""
    import looplab.llm as llm
    calls = {"open": 0, "sleep": 0}

    def fake_urlopen(req, timeout=None):
        calls["open"] += 1
        raise ConnectionRefusedError("connection refused")   # OSError subclass, not a timeout

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: calls.__setitem__("sleep", calls["sleep"] + 1))
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["open"] == 1        # one attempt, no retry
    assert calls["sleep"] == 0       # no backoff sleep on a steady-state failure


def test_post_retries_transient_ssl_eof(monkeypatch):
    """A mid-read TLS EOF (UNEXPECTED_EOF_WHILE_READING — the peer hung up, common over a hosted
    gateway like OpenRouter) is TRANSIENT and must be retried, not abort the run."""
    import ssl
    import urllib.error
    import looplab.llm as llm
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError(ssl.SSLEOFError("EOF occurred in violation of protocol"))
        return _OkResp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3           # retried the transient TLS drop, then succeeded


def test_post_fails_fast_on_tls_cert_error(monkeypatch):
    """A TLS CERT verification error is a steady-state misconfig — fail fast, do NOT retry."""
    import ssl
    import urllib.error
    import looplab.llm as llm
    calls = {"open": 0, "sleep": 0}

    def fake_urlopen(req, timeout=None):
        calls["open"] += 1
        raise urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: calls.__setitem__("sleep", calls["sleep"] + 1))
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    with pytest.raises(llm.LLMError):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["open"] == 1 and calls["sleep"] == 0


def test_h1_guided_json_adds_schema_constraints():
    """H1: with guided_json on, complete_tool sends response_format + guided_json built from the schema."""
    from looplab.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient("m", guided_json=True)
    captured = {}

    def _fake_post(payload):
        captured.update(payload)
        return {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "emit", "arguments": "{}"}}]}}], "usage": {}}

    c._post = _fake_post
    c.complete_tool([{"role": "user", "content": "x"}], {"type": "object", "properties": {}})
    assert captured.get("response_format", {}).get("type") == "json_schema"
    assert "guided_json" in captured


def test_h1_off_by_default_no_constraints():
    from looplab.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient("m")
    captured = {}
    c._post = lambda p: (captured.update(p) or {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "emit", "arguments": "{}"}}]}}], "usage": {}})
    c.complete_tool([{"role": "user", "content": "x"}], {"type": "object"})
    assert "response_format" not in captured and "guided_json" not in captured
