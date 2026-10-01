"""Local Messages fixture for REAL Claude Code MCP transport acceptance.

This is a scripted tool-call source, not an LLM or a scientific decision maker.
No scoring response is fabricated: the scenario consumes Claude's actual MCP
tool_result blocks. Keep it bound to loopback and run Claude with --bare, default
permissions, isolated settings and a synthetic API key, never a user's account.
The SSE vocabulary follows Claude's official Messages streaming contract.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


def tool_result(block):
    """Decode the actual MCP JSON, refusing client permission/tool failures first."""
    if block.get("is_error"):
        raise RuntimeError("Claude refused or failed the MCP tool call")
    content = block.get("content")
    if isinstance(content, str):
        return json.loads(content)
    if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
        raise ValueError("Expected JSON MCP text content")
    texts = [item["text"] for item in content if item.get("type") == "text"]
    if len(texts) != 1:
        raise ValueError("Expected one JSON MCP result block")
    return json.loads(texts[0])


class ToolFixture:
    def __init__(self, scenario):
        self.scenario = scenario
        self.current = next(scenario)
        self.calls = []
        self.results = []
        self.error = None
        self.done = False

    def message(self, body):
        if self.done:
            raise ValueError("Unexpected request after fixture completion")
        if self.calls:
            wanted = f"tool_local_{len(self.calls)}"
            blocks = [block for message in body.get("messages", [])
                      for block in message.get("content", [])
                      if isinstance(block, dict) and block.get("type") == "tool_result"
                      and block.get("tool_use_id") == wanted]
            if len(blocks) != 1:
                raise ValueError("Claude did not return the expected MCP tool result")
            self.results.append(blocks[0])
            try:
                self.current = self.scenario.send(blocks[0])
            except StopIteration:
                self.done = True
        if self.done:
            content = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "Local scripted MCP acceptance completed."}
            reason = "end_turn"
        else:
            name, arguments = self.current
            name = "mcp__looplab__" + name
            if name not in {tool["name"] for tool in body.get("tools", [])}:
                raise ValueError("Claude did not discover the requested LoopLab tool")
            self.calls.append(name)
            content = {"type": "tool_use", "id": f"tool_local_{len(self.calls)}",
                       "name": name, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
            reason = "tool_use"
        return [
            {"type": "message_start", "message": {
                "id": f"msg_local_{len(self.calls)}", "type": "message", "role": "assistant",
                "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": content},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]


@contextmanager
def local_provider(scenario):
    fixture = ToolFixture(scenario)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            # No request bodies, auth headers, prompts or tool inputs are logged.
            if int(self.headers.get("Content-Length", "0")) > 4 * 1024 * 1024:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.split("?", 1)[0] == "/v1/messages/count_tokens":
                payload = b'{"input_tokens":1}'
                content_type = "application/json"
            elif self.path.split("?", 1)[0] == "/v1/messages":
                try:
                    events = fixture.message(body)
                except (ValueError, RuntimeError, AssertionError, KeyError, TypeError) as exc:
                    fixture.error = exc
                    self.send_error(500, "Local acceptance assertion failed")
                    return
                payload = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                                  for event in events).encode()
                content_type = "text/event-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
