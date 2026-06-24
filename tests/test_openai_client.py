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
