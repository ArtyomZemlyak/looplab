"""I2: OpenAI-compatible client against a mock HTTP server (offline, deterministic).
Verifies tool-call parsing, the text/JSON fallback path, and <think> stripping."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from looplab.core.llm import OpenAICompatibleClient
from looplab.core.models import Idea
from looplab.core.parse import parse_structured


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
    import looplab.core.llm as llm
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


def test_post_drops_reasoning_on_unsupported_param_400(monkeypatch):
    """A litellm-proxied model (e.g. glm-5.1) returns 400 UnsupportedParamsError for `reasoning_effort`.
    The client must DROP the reasoning toggle and retry — so the model works — and remember it (deepseek
    keeps reasoning; glm-5.1 silently drops it). Without this glm-5.1 hard-fails and 'produces nothing'."""
    import io
    import looplab.core.llm as llm
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(json.loads(req.data.decode()))
        if len(seen) == 1:                                  # first attempt carries reasoning_effort
            body = json.dumps({"error": {"message": "litellm.UnsupportedParamsError: openai does "
                                         "not support parameters: ['reasoning_effort']"}}).encode()
            raise llm.urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
        return _OkResp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("glm-5.1", base_url="http://x/v1",
                                   reasoning={"reasoning_effort": "high"})
    assert c._reasoning_ok is True
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert c._reasoning_ok is False                         # adapted — won't send reasoning again
    assert "reasoning_effort" in seen[0] and "reasoning_effort" not in seen[1]  # dropped on retry
    assert len(seen) == 2


def test_read_stream_watchdog_breaks_a_stalled_connection():
    """A streamed response that BLOCKS in recv() with no new tokens (a stalled generation, or a server
    trickling keepalive bytes without completing a line) must not hang forever: a watchdog SHUTS DOWN
    the underlying socket after `timeout` s — which actually interrupts the blocked recv (resp.close()
    alone does not) — so the read ends and _post retries. Regression for a ~70-min / ~15-min live hang.
    Uses a real socketpair so the socket.shutdown() path (not just the close() fallback) is exercised."""
    import socket
    import time as _t
    import looplab.core.llm as llm

    a, _b = socket.socketpair()                   # _b never sends -> a.recv blocks like a stalled server

    class _SockResp:
        def __init__(self, s):
            self.fp = type("FP", (), {"raw": type("R", (), {"_sock": s})()})()
            self._s = s

        def __iter__(self):
            return self

        def __next__(self):
            data = self._s.recv(100)              # blocks; watchdog shutdown() -> returns b'' (EOF)
            if not data:
                raise StopIteration
            return data

        def close(self):
            try:
                self._s.close()
            except OSError:
                pass

        def read(self):
            return b""

    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1", timeout=2)   # 2s idle limit
    t0 = _t.monotonic()
    with pytest.raises((OSError, TimeoutError)):
        c._read_stream(_SockResp(a))
    assert _t.monotonic() - t0 < 6                # fired near the 2s limit, did NOT hang


def test_post_raises_after_exhausting_timeouts(monkeypatch):
    """A persistently dead endpoint still surfaces a clean LLMError once retries are exhausted."""
    import looplab.core.llm as llm

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
    import looplab.core.llm as llm
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
    import looplab.core.llm as llm
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
    import looplab.core.llm as llm
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


# --- bad-200-body resilience: an empty / keepalive-only / truncated 200 is a transient gateway
# hiccup (OpenRouter ': OPENROUTER PROCESSING' heartbeats, dropped socket) → retry, not crash -----
class _BytesResp:
    """A urlopen context manager whose body is whatever bytes/str the test queues."""
    def __init__(self, payload):
        self._payload = payload if isinstance(payload, bytes) else payload.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


_GOOD = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})


def test_post_retries_empty_200_body(monkeypatch):
    """A 200 with an empty / whitespace body (gateway dropped the socket before the final JSON) must
    be retried with backoff and then succeed — it crashed the DeepSeek run before this fix."""
    import looplab.core.llm as llm
    bodies = iter(["", "  \n \n\n ", _GOOD])      # two bad 200s, then the real payload
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _BytesResp(next(bodies))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3                          # retried twice, succeeded on the 3rd


def test_post_recovers_sse_keepalive_comments(monkeypatch):
    """OpenRouter interleaves ': OPENROUTER PROCESSING' SSE comment lines with the JSON on a slow
    non-streaming call; the body is recovered by dropping comment lines — no retry needed."""
    import looplab.core.llm as llm
    body = f": OPENROUTER PROCESSING\n\n: OPENROUTER PROCESSING\n\n{_GOOD}\n"
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _BytesResp(body)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 1                          # parsed on the first attempt, no retry


def test_post_retries_incomplete_read(monkeypatch):
    """A socket-level truncation (http.client.IncompleteRead from resp.read() — the gateway dropped
    the connection mid-body) is transient and must be retried, not abort the run."""
    import http.client
    import looplab.core.llm as llm
    calls = {"n": 0}

    class _TruncResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): raise http.client.IncompleteRead(b"partial")

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _OkResp() if calls["n"] >= 3 else _TruncResp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    assert c.complete_text([{"role": "user", "content": "go"}]) == "ok"
    assert calls["n"] == 3                            # retried the truncated reads, then succeeded


def test_post_raises_after_persistent_non_json(monkeypatch):
    """A persistently unparseable 200 body still surfaces a clean LLMError once retries are spent."""
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _BytesResp("<html>502 Bad Gateway</html>")

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    with pytest.raises(llm.LLMError, match="non-JSON"):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["n"] == 5                           # 1 + _max_retries attempts, then raises


def test_post_fails_fast_on_error_envelope(monkeypatch):
    """A valid-JSON `{"error": ...}` envelope (genuine bad request) is NOT a transient body — it must
    fail fast at the no-choices check with ONE attempt, not waste the retry budget."""
    import looplab.core.llm as llm
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _BytesResp(json.dumps({"error": {"code": 400, "message": "bad request"}}))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda *_a: None)
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    with pytest.raises(llm.LLMError, match="no choices"):
        c.complete_text([{"role": "user", "content": "go"}])
    assert calls["n"] == 1                           # error envelope fails fast, no retry


def test_h1_guided_json_adds_schema_constraints():
    """H1: with guided_json on, complete_tool sends response_format + guided_json built from the schema."""
    from looplab.core.llm import OpenAICompatibleClient
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
    from looplab.core.llm import OpenAICompatibleClient
    c = OpenAICompatibleClient("m")
    captured = {}
    c._post = lambda p: (captured.update(p) or {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "emit", "arguments": "{}"}}]}}], "usage": {}})
    c.complete_tool([{"role": "user", "content": "x"}], {"type": "object"})
    assert "response_format" not in captured and "guided_json" not in captured


def test_read_stream_falls_back_on_iterable_non_sse_body():
    """A response that ITERATES line-by-line but never sends a `data:` line (a non-streaming endpoint
    behind a streaming request) must be reassembled from its raw lines and parsed as one plain JSON
    chat body — the `_sse_chunks` tail (raw_lines + got_sse) feeding `_read_stream`'s fallback."""
    import looplab.core.llm as llm

    class _IterResp:
        """Iterable urllib-response stand-in with NO usable read() (forces the raw-lines path)."""
        def __init__(self, lines):
            self._lines = lines
            self.fp = None

        def __iter__(self):
            return iter(self._lines)

        def close(self):
            pass

    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "plain"}}],
                       "usage": {"total_tokens": 3}}, indent=1)
    # A multi-line JSON body (plus a leading ':' comment) — line iteration splits at real newlines.
    lines = [b": PROCESSING\n"] + [(ln + "\n").encode() for ln in body.splitlines()]
    c = llm.OpenAICompatibleClient("m", base_url="http://x/v1")
    out = c._read_stream(_IterResp(lines))
    assert out["choices"][0]["message"]["content"] == "plain"
    assert out["usage"] == {"total_tokens": 3}
