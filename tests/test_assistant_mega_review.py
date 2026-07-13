"""Regression tests for the mega code-review round over the assistant + chats surface:
attachment/context persistence across turns, permission gating of revert_file, git option-injection,
per-tool cancel, in-place history compaction, the stream-stall guard, and the shell default cwd."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from looplab.agents.agent import drive_tool_loop  # noqa: E402
from looplab.serve.assistant import run_turn  # noqa: E402
from looplab.tools.git_tools import GitTools  # noqa: E402
from looplab.tools.knowledge_tools import KnowledgeWriteTools  # noqa: E402
from looplab.core.llm import OpenAICompatibleClient  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from looplab.tools.shell_tools import ShellTools  # noqa: E402
from looplab.tools.write_tools import WriteTools  # noqa: E402

ALLOW = lambda a: "allow_once"   # noqa: E731
DENY = lambda a: "deny"          # noqa: E731


def _call(name, args, cid="c1"):
    return {"content": "", "tool_calls": [{"id": cid, "function": {"name": name,
                                                                   "arguments": json.dumps(args)}}]}


class _Fake:
    """Scripted chat client; records every messages list it was called with."""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.seen = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.seen.append([dict(m) for m in messages])
        return self.scripted.pop(0)

    def complete_text(self, messages):
        return "SUMMARY"


# ---- raw instruction (attachments / UI context) must survive into later turns -------------------
def test_run_turn_history_prefers_raw_over_display():
    fake = _Fake([_call("final_answer", {"reply": "ok"})])
    history = [{"role": "user", "content": "clean bubble", "raw": "clean bubble\n[ATTACHED-FILE-BODY]"},
               {"role": "assistant", "content": "noted"}]
    run_turn(fake, ".", history, "and now?", "plan")
    convo = fake.seen[0]
    joined = "\n".join(str(m.get("content")) for m in convo)
    assert "[ATTACHED-FILE-BODY]" in joined          # the model sees the FULL prior instruction
    assert "clean bubble" in joined


def test_message_stream_persists_raw_and_share_strips_it(tmp_path, monkeypatch):
    class _C(_Fake):
        def complete_text_stream(self, messages):
            yield "done"
    monkeypatch.setattr("looplab.serve.server.make_llm_client",
                        lambda s: _C([_call("final_answer", {"reply": "done"})]))
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "plan"}).json()["id"]
    with client.stream("POST", f"/api/assistant/sessions/{sid}/message_stream",
                       json={"instruction": "question\n[FILE dump]", "display": "question",
                             "mode": "plan"}) as r:
        for _ in r.iter_lines():
            pass
    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert msgs[0]["content"] == "question"                      # clean bubble for the UI
    assert msgs[0]["raw"] == "question\n[FILE dump]"             # full text kept for later turns
    client.post(f"/api/assistant/sessions/{sid}/share")
    shared = client.get(f"/api/assistant/shared/{sid}").json()["messages"]
    assert all("raw" not in m for m in shared)                   # a share link shows bubbles only


def test_shared_assistant_allowlists_applied_action_fields(tmp_path, monkeypatch):
    secret = "AKIAIOSFODNN7EXAMPLE"

    def fake_turn(*_args, **_kwargs):
        return {"ok": True, "reply": "finished", "mode": "auto",
                "steps": [{"tool": "read_file", "arg": "C:/Users/me/private.txt"}],
                "applied": [{"tool": "write_file", "label": "updated config",
                             "abs_path": "C:/Users/me/private/config.py",
                             "preview": f"+API_KEY={secret}"}],
                "todos": [{"content": "finish", "status": "completed"}],
                "tokens": {"total": 12}, "proposals": [{"task_file": "C:/private/task.json"}]}

    monkeypatch.setattr("looplab.serve.routers.assistant._assistant_run_turn", fake_turn)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"mode": "auto"}).json()["id"]
    assert client.post(f"/api/assistant/sessions/{sid}/message",
                       json={"instruction": "do it", "mode": "auto"}).status_code == 200
    client.post(f"/api/assistant/sessions/{sid}/share")

    messages = client.get(f"/api/assistant/shared/{sid}").json()["messages"]
    payload = str(messages)
    assert "abs_path" not in payload and "preview" not in payload and "proposals" not in payload
    assert "C:/Users/me" not in payload and secret not in payload
    assert messages[-1]["applied"] == [{"label": "updated config", "tool": "write_file"}]


# ---- revert_file is a mutation: mode/approver gated + recorded ----------------------------------
def test_revert_file_tool_is_permission_gated(tmp_path):
    bak = tmp_path / "bak"
    w_auto = WriteTools([tmp_path], mode="auto", backup_dir=bak)
    f = tmp_path / "a.txt"
    assert "wrote" in w_auto.execute("write_file", {"path": str(f), "content": "v0"})
    assert "edited" in w_auto.execute("edit_file", {"path": str(f), "old_str": "v0", "new_str": "v1"})
    w_no = WriteTools([tmp_path], mode="default", approver=DENY, backup_dir=bak)
    assert "declined" in w_no.execute("revert_file", {"path": str(f)})
    assert f.read_text() == "v1"                                 # deny -> disk untouched
    w_yes = WriteTools([tmp_path], mode="default", approver=ALLOW, backup_dir=bak)
    assert "reverted" in w_yes.execute("revert_file", {"path": str(f)})
    assert f.read_text() == "v0"
    assert any(a["tool"] == "revert_file" for a in w_yes.applied)  # surfaced in the turn's applied


# ---- git option injection --------------------------------------------------------------------
def test_git_dash_refs_are_refused(tmp_path):
    g = GitTools(ShellTools([tmp_path], mode="auto"), cwd=tmp_path)
    assert "refused" in g.execute("git_checkout", {"ref": "-f"})
    assert "refused" in g.execute("git_branch", {"name": "-D"})


# ---- remember never crashes the turn -----------------------------------------------------------
def test_remember_tolerates_junk_tags_and_fs_errors(tmp_path):
    k = KnowledgeWriteTools(str(tmp_path / "kb"), mode="auto")
    assert "saved" in k.execute("remember", {"title": "t", "note": "n", "tags": 5})
    blocked = KnowledgeWriteTools(str(tmp_path / "file-not-dir" / "kb"), mode="auto")
    (tmp_path / "file-not-dir").write_text("x")                  # mkdir will fail
    out = blocked.execute("remember", {"title": "t", "note": "n"})
    assert out.startswith("(error")                              # error string, not an exception


def test_remember_is_absent_and_denied_in_read_only_plan(tmp_path):
    from types import SimpleNamespace

    from looplab.serve.assistant import build_tools

    kb = tmp_path / "kb"
    direct = KnowledgeWriteTools(str(kb), mode="plan")
    assert direct.specs() == []
    assert "read-only plan mode" in direct.execute(
        "remember", {"title": "t", "note": "must not persist"})
    assert not kb.exists()

    tools = build_tools(
        tmp_path, mode="plan", settings=SimpleNamespace(knowledge_dir=str(kb)))
    assert "remember" not in {spec["function"]["name"] for spec in tools.specs()}


# ---- cancel mid-turn skips the remaining tool calls ---------------------------------------------
def test_cancel_skips_remaining_tools():
    executed = []

    class _T:
        def specs(self):
            return [{"type": "function", "function": {"name": n, "parameters": {}}}
                    for n in ("t1", "t2")]

        def execute(self, name, args):
            executed.append(name)
            return "ok"

    emit = {"type": "function", "function": {"name": "emit", "parameters": {}}}
    msg = {"content": "", "tool_calls": [
        {"id": "a", "function": {"name": "t1", "arguments": "{}"}},
        {"id": "b", "function": {"name": "t2", "arguments": "{}"}}]}
    fake = _Fake([msg])
    out = drive_tool_loop(fake, _T(), [{"role": "user", "content": "go"}], emit,
                          cancel_check=lambda: bool(executed),   # Stop lands after the FIRST tool ran
                          finalize=lambda a: "emitted", fallback=lambda m: m)
    assert executed == ["t1"]                                    # t2 was never executed
    stubs = [m for m in out if m.get("role") == "tool" and m.get("tool_call_id") == "b"]
    assert stubs and "cancelled" in stubs[0]["content"]          # no dangling tool_call_id


# ---- compaction keeps the caller's list identity (no orphaned trace) ----------------------------
def test_compaction_is_in_place_for_the_caller():
    class _T:
        def specs(self):
            return [{"type": "function", "function": {"name": "read", "parameters": {}}}]

        def execute(self, name, args):
            return "X" * 2000

    emit = {"type": "function", "function": {"name": "emit", "parameters": {}}}
    # DISTINCT args per read so the StuckDetector never fires (the read-dedup that used to stub exact
    # repeats is gone) — this test exercises history COMPACTION, which needs 3 big reads to accumulate.
    fake = _Fake([_call("read", {"f": 1}, "c1"), _call("read", {"f": 2}, "c2"), _call("read", {"f": 3}, "c3"),
                  _call("emit", {"reply": "ok"}, "c4")])
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    drive_tool_loop(fake, _T(), msgs, emit, auto_summary=True, context_budget_chars=1000,
                    finalize=lambda a: "done", fallback=lambda m: "fb")
    # The compaction summary must be visible IN THE CALLER'S list (in-place slice assign),
    # not only in a rebound copy the loop kept privately.
    assert any("Summary of earlier steps" in str(m.get("content")) for m in msgs)


# ---- the final-answer stream can't spin forever on keepalive heartbeats -------------------------
def test_complete_text_stream_bails_on_a_keepalive_stall(monkeypatch):
    class _Ctx:
        def __enter__(self):
            return iter([b": keepalive\n"] * 10000)              # heartbeats, never a token

        def __exit__(self, *a):
            return False
    import looplab.core.llm as llm
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=None: _Ctx())
    c = OpenAICompatibleClient("m", base_url="http://x/v1", timeout=0.0)
    monkeypatch.setattr(c, "complete_text", lambda messages: "FALLBACK")
    t0 = time.monotonic()
    got = list(c.complete_text_stream([{"role": "user", "content": "hi"}]))
    assert got == ["FALLBACK"]                                   # stall detected -> blocking fallback
    assert time.monotonic() - t0 < 5


# ---- run_command default cwd ----------------------------------------------------------------
def test_shell_default_cwd_is_honored(tmp_path):
    sub = tmp_path / "repo"
    sub.mkdir()
    sh = ShellTools([tmp_path], mode="auto", approver=ALLOW, default_cwd=sub)
    out = sh.execute("run_command", {"command": [sys.executable, "-c", "import os; print(os.getcwd())"]})
    assert str(sub) in out
