"""I2: OpenAI-compatible client against a mock HTTP server (offline, deterministic).
Verifies tool-call parsing, the text/JSON fallback path, and <think> stripping."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from autornd.llm import OpenAICompatibleClient
from autornd.models import Idea
from autornd.parse import parse_structured


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
