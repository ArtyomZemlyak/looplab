"""P2b: real token streaming — the llm primitive, run_turn's reply_sink, and the SSE endpoint."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.assistant import run_turn  # noqa: E402
from looplab.core.llm import OpenAICompatibleClient  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _sse(chunks):
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    lines.append("data: [DONE]")
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


def test_complete_text_stream_parses_sse(monkeypatch):
    import types

    def _chunk(text):
        delta = types.SimpleNamespace(content=text)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
                                     usage=None)

    def fake_create(**kwargs):
        for t in ["Hel", "lo", "!"]:
            yield _chunk(t)

    c = OpenAICompatibleClient("m", base_url="http://x/v1")
    monkeypatch.setattr(c._sdk.chat.completions, "create", fake_create)
    assert list(c.complete_text_stream([{"role": "user", "content": "hi"}])) == ["Hel", "lo", "!"]


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


class _StreamFake:
    def __init__(self):
        self.scripted = [_call("final_answer", {"reply": "loop reply (fallback)"})]

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)

    def complete_text_stream(self, messages):
        for p in ["Streamed ", "answer."]:
            yield p

    def complete_text(self, messages):
        return "Streamed answer."


def test_run_turn_streams_final_answer(tmp_path):
    got = []
    res = run_turn(_StreamFake(), tmp_path, [], "hi", "plan", reply_sink=got.append)
    assert "".join(got) == "Streamed answer."
    assert res["reply"] == "Streamed answer."      # the streamed answer wins over the loop's emit reply


class _InterFake:
    """Turn 1: prose + a real tool call (interstitial message). Turn 2: the final emit."""
    def __init__(self):
        self.scripted = [
            {"content": "Let me look at the runs first.",
             "tool_calls": [{"id": "c1", "function": {"name": "list_runs", "arguments": "{}"}}]},
            _call("final_answer", {"reply": "Here is what I found."}),
        ]

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)

    def complete_text_stream(self, messages):
        yield "Here is what I found."

    def complete_text(self, messages):
        return "Here is what I found."


def test_run_turn_surfaces_interstitial_text(tmp_path):
    # The prose the model writes ALONGSIDE a tool round is delivered via on_text (Claude-Desktop feel),
    # distinct from the final streamed answer (reply_sink).
    texts = []
    run_turn(_InterFake(), tmp_path, [], "why did it fail?", "plan", on_text=texts.append)
    assert texts == ["Let me look at the runs first."]


def test_message_stream_emits_text_event(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _InterFake())
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    texts, event = [], None
    with client.stream("POST", f"/api/assistant/sessions/{sid}/message_stream",
                       json={"instruction": "why did it fail?", "mode": "plan"}) as r:
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "text":
                texts.append(json.loads(line.split(":", 1)[1].strip()))
    assert "Let me look at the runs first." in texts   # interstitial prose reached the client as a `text` SSE event


def test_message_stream_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _StreamFake())
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    tokens, done = [], None
    with client.stream("POST", f"/api/assistant/sessions/{sid}/message_stream",
                       json={"instruction": "hi", "mode": "plan"},
                       headers={"Accept-Encoding": "gzip"}) as r:
        # Compression must never buffer a live token stream. Starlette excludes event-stream responses
        # even when the client advertises gzip; X-Accel-Buffering pins the proxy half of the contract.
        assert r.headers.get("Content-Encoding") is None
        assert r.headers.get("X-Accel-Buffering") == "no"
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                if event == "token":
                    tokens.append(data)
                elif event == "done":
                    done = data
    assert "".join(tokens) == "Streamed answer."
    assert done and done["reply"] == "Streamed answer."
    # persisted
    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == "Streamed answer."
