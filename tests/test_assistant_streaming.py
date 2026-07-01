"""P2b: real token streaming — the llm primitive, run_turn's reply_sink, and the SSE endpoint."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import looplab.llm as llm  # noqa: E402
from looplab.assistant import run_turn  # noqa: E402
from looplab.llm import OpenAICompatibleClient  # noqa: E402
from looplab.server import make_app  # noqa: E402


def _sse(chunks):
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    lines.append("data: [DONE]")
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


def test_complete_text_stream_parses_sse(monkeypatch):
    class _Ctx:
        def __init__(self, body): self.body = body
        def __enter__(self): return self.body
        def __exit__(self, *a): return False
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=None: _Ctx(_sse(["Hel", "lo", "!"])))
    c = OpenAICompatibleClient("m", base_url="http://x/v1")
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


def test_message_stream_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.server.make_llm_client", lambda s: _StreamFake())
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    tokens, done = [], None
    with client.stream("POST", f"/api/assistant/sessions/{sid}/message_stream",
                       json={"instruction": "hi", "mode": "plan"}) as r:
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
