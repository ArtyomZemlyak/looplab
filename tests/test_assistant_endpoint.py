"""The general assistant (P0): session persistence, the read-only tool turn, and the HTTP routes.

Uses a scripted fake chat client (like tests/test_agentic_retrieval.py) so nothing hits a network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from looplab.assistant import SessionStore, run_turn, normalize_mode, expand_mentions  # noqa: E402
from looplab.server import make_app  # noqa: E402


# --------------------------------------------------------------------------- scripted fake client
class _FakeChatClient:
    """Scripts assistant messages (a queue of chat() return dicts); records what it received."""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append(list(messages))
        return self.scripted.pop(0)


def _call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _final(reply):
    return _call("final_answer", {"reply": reply})


# --------------------------------------------------------------------------- SessionStore
def test_session_store_crud_and_fork(tmp_path):
    st = SessionStore(tmp_path)
    assert st.list() == []
    m = st.create(title="hello", mode="plan", now=1.0)
    assert m["id"] and m["mode"] == "plan" and m["title"] == "hello"
    st.append(m["id"], {"role": "user", "content": "hi"})
    st.append(m["id"], {"role": "assistant", "content": "yo"})
    got = st.get(m["id"])
    assert [t["content"] for t in got["messages"]] == ["hi", "yo"]
    # fork clones the transcript into a new child pointing back at the source
    child = st.fork(m["id"], now=2.0)
    assert child["id"] != m["id"] and child["parent"] == m["id"]
    assert [t["content"] for t in st.get(child["id"])["messages"]] == ["hi", "yo"]
    assert {s["id"] for s in st.list()} == {m["id"], child["id"]}


def test_session_store_rejects_traversal(tmp_path):
    st = SessionStore(tmp_path)
    for bad in ("../evil", "a/b", ".."):
        try:
            st._sdir(bad)
            assert False, f"expected traversal guard to reject {bad!r}"
        except ValueError:
            pass


def test_expand_mentions(tmp_path):
    # @run:<id> grounds on a real run; @file:<path> injects file contents; both are reported in refs.
    rd = tmp_path / "demo"; rd.mkdir()
    (rd / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"demo","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")
    f = tmp_path / "note.md"; f.write_text("hello file")
    text, refs = expand_mentions(f"look at @run:demo and @file:{f}", tmp_path,
                                 alive_fn=lambda p: False, roots=[tmp_path])
    assert "[@run:demo]" in text and "hello file" in text
    kinds = {r["type"] for r in refs}
    assert kinds == {"run", "file"}
    # an unknown run is left as-is (no crash, no ref)
    text2, refs2 = expand_mentions("see @run:ghost", tmp_path)
    assert refs2 == [] and "@run:ghost" in text2


def test_normalize_mode():
    assert normalize_mode(None) == "plan"
    assert normalize_mode("bogus") == "plan"
    assert normalize_mode("auto") == "auto"


# --------------------------------------------------------------------------- run_turn
def test_run_turn_uses_read_tool_then_answers(tmp_path):
    # A run on disk so list_runs has something to find.
    rd = tmp_path / "demo"; rd.mkdir()
    (rd / "events.jsonl").write_text(
        '{"seq":0,"type":"run_started","data":{"run_id":"demo","task_id":"t","goal":"g","direction":"max"}}\n',
        encoding="utf-8")
    client = _FakeChatClient([_call("list_runs", {}), _final("I see one run: demo.")])
    res = run_turn(client, tmp_path, [], "what runs exist?", "plan", alive_fn=lambda p: False)
    assert res["ok"] and res["reply"] == "I see one run: demo."
    # the tool step was recorded and the tool actually ran (list_runs result reached the model)
    assert any(s["tool"] == "list_runs" for s in res["steps"])
    tool_msgs = [m for turn in client.turns for m in turn if m.get("role") == "tool"]
    assert any("demo" in (m.get("content") or "") for m in tool_msgs)


def test_run_turn_soft_fails_on_client_error(tmp_path):
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("no endpoint")
    res = run_turn(_Boom(), tmp_path, [], "hi", "plan")
    assert res["ok"] is False and "no endpoint" in res["error"]


def test_run_turn_write_with_approval(tmp_path):
    target = tmp_path / "new.txt"
    client = _FakeChatClient([_call("write_file", {"path": str(target), "content": "hi"}), _final("done")])
    res = run_turn(client, tmp_path, [], "make a file", "default", approver=lambda a: "allow_once")
    assert res["ok"] and target.read_text() == "hi"
    assert res["applied"] and res["applied"][0]["tool"] == "write_file"


def test_run_turn_write_declined(tmp_path):
    target = tmp_path / "no.txt"
    client = _FakeChatClient([_call("write_file", {"path": str(target), "content": "x"}), _final("ok")])
    res = run_turn(client, tmp_path, [], "make a file", "default", approver=lambda a: "deny")
    assert res["ok"] and not target.exists() and not res["applied"]


def test_run_turn_propose_run(tmp_path):
    spec = {"run_id": "titanic-baseline", "task": {"kind": "dataset", "goal": "predict survival",
            "direction": "max", "data_path": "/d/train.csv"}, "settings": {"max_nodes": 20}}
    client = _FakeChatClient([_call("propose_run", spec), _final("Proposed a titanic run.")])
    res = run_turn(client, tmp_path, [], "start a titanic run", "plan")
    assert res["ok"] and res["proposals"]
    p = res["proposals"][0]
    assert p["run_id"] == "titanic-baseline" and p["task"]["kind"] == "dataset"
    assert p["settings"]["max_nodes"] == 20


def test_plan_mode_has_no_write_tool(tmp_path):
    # In plan mode the mutating tools are dropped from the schema, so a write attempt is unknown.
    client = _FakeChatClient([_call("write_file", {"path": str(tmp_path / "x"), "content": "y"}),
                              _final("can't in plan mode")])
    res = run_turn(client, tmp_path, [], "write x", "plan")
    assert res["ok"] and not (tmp_path / "x").exists()
    tool_msgs = [m for turn in client.turns for m in turn if m.get("role") == "tool"]
    assert any("unknown tool" in (m.get("content") or "") for m in tool_msgs)


# --------------------------------------------------------------------------- HTTP routes
def test_assistant_endpoints_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.server.make_llm_client",
                        lambda s: _FakeChatClient([_call("list_runs", {}), _final("done — no runs.")]))
    client = TestClient(make_app(tmp_path))

    # create + list
    sid = client.post("/api/assistant/sessions", json={"title": "t"}).json()["id"]
    assert any(s["id"] == sid for s in client.get("/api/assistant/sessions").json()["sessions"])

    # a turn: fast fake -> returns inline (not a job_id)
    r = client.post(f"/api/assistant/sessions/{sid}/message",
                    json={"instruction": "hello", "mode": "plan"}).json()
    assert r.get("ok") and r["reply"] == "done — no runs."

    # persisted: user + assistant turns
    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "done — no runs."

    # fork + delete
    child = client.post(f"/api/assistant/sessions/{sid}/fork").json()
    assert child["parent"] == sid
    assert client.delete(f"/api/assistant/sessions/{sid}").json()["ok"]
    assert client.get(f"/api/assistant/sessions/{sid}").status_code == 404


def test_assistant_permission_pause_resume(tmp_path, monkeypatch):
    """default mode: a write blocks on a permission request; resolving it unblocks the turn thread and
    the file is written (true mid-loop human-in-the-loop, not an 'assume applied' buffer)."""
    import time
    monkeypatch.setenv("LOOPLAB_JOB_INLINE_WAIT", "0.3")     # return {running} fast; the turn blocks on approval
    target = tmp_path / "made.txt"
    monkeypatch.setattr("looplab.server.make_llm_client",
                        lambda s: _FakeChatClient([_call("write_file", {"path": str(target), "content": "yo"}),
                                                   _final("wrote it")]))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "default"}).json()["id"]
    resp = client.post(f"/api/assistant/sessions/{sid}/message",
                       json={"instruction": "make it", "mode": "default"}).json()
    assert resp.get("status") == "running" and resp.get("job_id")

    # a permission request appears; approve it
    req = None
    for _ in range(100):
        pend = client.get(f"/api/assistant/permissions?session={sid}").json()["pending"]
        if pend:
            req = pend[0]; break
        time.sleep(0.1)
    assert req and req["action"]["tool"] == "write_file"
    assert client.post(f"/api/assistant/permissions/{req['id']}", json={"decision": "allow_once"}).json()["ok"]

    # the job now completes and the file is on disk
    result = None
    for _ in range(100):
        j = client.get(f"/api/jobs/{resp['job_id']}").json()
        if j.get("status") == "done":
            result = j; break        # GET /api/jobs spreads the result at top level
        time.sleep(0.1)
    assert result and result["reply"] == "wrote it"
    assert target.read_text() == "yo"


def test_assistant_message_soft_fails_offline(tmp_path, monkeypatch):
    def _boom(s):
        raise RuntimeError("connection refused")
    monkeypatch.setattr("looplab.server.make_llm_client", _boom)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={}).json()["id"]
    r = client.post(f"/api/assistant/sessions/{sid}/message", json={"instruction": "hi"}).json()
    assert r["ok"] is False and "connection refused" in r["error"]
    # the failure reply is still persisted so the transcript isn't lost
    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert msgs[-1]["role"] == "assistant"
