"""P3: slash commands, background commands, and session share."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from looplab.assistant_commands import expand_command, list_commands  # noqa: E402
from looplab.bg_tasks import BackgroundManager  # noqa: E402
from looplab.server import make_app  # noqa: E402
from looplab.shell_tools import ShellTools  # noqa: E402


def test_slash_command_expansion():
    assert expand_command("/review") != "/review" and "git_diff" in expand_command("/review")
    assert "tests/test_x.py" in expand_command("/test tests/test_x.py")
    assert expand_command("hello") == "hello"          # non-command passthrough
    assert expand_command("/unknown x") == "/unknown x"
    assert {c["name"] for c in list_commands()} >= {"init", "review", "commit", "test"}


def test_background_manager_reads_incrementally(tmp_path):
    mgr = BackgroundManager()
    tid = mgr.start([sys.executable, "-c", "print('a'); import time; time.sleep(0.3); print('b')"], str(tmp_path))
    time.sleep(0.15)
    r1 = mgr.read(tid)
    assert r1["status"] == "running" and "a" in r1["new_output"]
    time.sleep(0.4)
    r2 = mgr.read(tid)
    assert r2["status"] == "exited" and r2["exit_code"] == 0 and "b" in r2["new_output"]
    assert "a" not in r2["new_output"]                 # cursor advanced — only NEW output


def test_shell_background_tool(tmp_path):
    s = ShellTools([tmp_path], mode="auto")
    r = s.execute("run_command", {"command": [sys.executable, "-c", "print('hi')"], "background": True})
    assert "background task" in r
    tid = r.split("task ")[1].split(" ")[0]
    time.sleep(0.3)
    out = s.execute("read_output", {"task_id": tid})
    assert "hi" in out
    assert tid in s.execute("list_background", {})


def test_assistant_commands_endpoint(tmp_path):
    client = TestClient(make_app(tmp_path))
    cmds = client.get("/api/assistant/commands").json()["commands"]
    assert any(c["name"] == "review" for c in cmds)


def test_session_share_roundtrip(tmp_path):
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"title": "t"}).json()["id"]
    # not shared yet -> 404
    assert client.get(f"/api/assistant/shared/{sid}").status_code == 404
    r = client.post(f"/api/assistant/sessions/{sid}/share").json()
    assert r["ok"] and r["url"].endswith(sid)
    shared = client.get(f"/api/assistant/shared/{sid}").json()
    assert shared["meta"]["shared"] is True
