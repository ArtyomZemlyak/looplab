"""P3: slash commands, background commands, and session share."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from looplab.serve.assistant_commands import expand_command, list_commands  # noqa: E402
from looplab.runtime.bg_tasks import BackgroundManager  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from looplab.tools.shell_tools import ShellTools  # noqa: E402


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


def test_background_read_backpressure_nothing_lost(tmp_path):
    """F7: read() used to advance the cursor past the WHOLE log and then tail-truncate the text —
    output beyond the budget was consumed and unrecoverable. Now each poll returns one bounded chunk
    and advances the cursor ONLY by what it returned, so sequential polls are complementary."""
    from looplab.runtime.bg_tasks import _MAX_READ, BackgroundManager as _BM
    mgr = _BM()
    payload = "".join(f"<{i:04d}>" for i in range(1667))          # ~10KB of unique markers
    tid = mgr.start([sys.executable, "-c", f"import sys; sys.stdout.write({payload!r})"],
                    str(tmp_path))
    for _ in range(200):                                          # wait for the writer to finish
        if mgr._tasks[tid]["proc"].poll() is not None:
            break
        time.sleep(0.05)
    r1 = mgr.read(tid)
    r2 = mgr.read(tid)
    assert len(r1["new_output"]) <= _MAX_READ and len(r2["new_output"]) <= _MAX_READ
    assert r1["pending"] > 0                                      # first poll left output pending
    assert r2["new_output"] and r2["new_output"] != r1["new_output"]
    chunks, r = [r1["new_output"], r2["new_output"]], r2
    while r["pending"]:
        r = mgr.read(tid)
        chunks.append(r["new_output"])
    assert "".join(chunks) == payload                             # complementary chunks — nothing lost


def test_shell_read_output_reports_more_pending(tmp_path):
    """The shell-level read_output reply stays under the loop cap and says when more is pending, so
    the model polls again instead of assuming it saw everything."""
    from looplab.runtime.bg_tasks import MANAGER
    from looplab.tools._base import RESULT_CAP
    s = ShellTools([tmp_path], mode="auto")
    code = "import sys; sys.stdout.write(''.join('<%05d>' % i for i in range(2000)))"   # 14KB, positional
    r = s.execute("run_command", {"command": [sys.executable, "-c", code], "background": True})
    tid = r.split("task ")[1].split(" ")[0]
    for _ in range(200):
        if any(t["task_id"] == tid and t["status"] == "exited" for t in MANAGER.list()):
            break
        time.sleep(0.05)
    out1 = s.execute("read_output", {"task_id": tid})
    assert len(out1) <= RESULT_CAP
    assert "more output pending — poll read_output again" in out1
    assert "<00000>" in out1 and "<00600>" not in out1            # first chunk = the log's HEAD only
    out2 = s.execute("read_output", {"task_id": tid})
    assert "<00600>" in out2 and "<00000>" not in out2            # the next poll CONTINUES the log


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


def test_background_closes_handle_after_exit(tmp_path):
    mgr = BackgroundManager()
    tid = mgr.start([sys.executable, "-c", "print('x')"], str(tmp_path))
    for _ in range(50):
        r = mgr.read(tid)
        if r["status"] == "exited":
            break
        time.sleep(0.05)
    assert mgr._tasks[tid].get("closed") is True
    assert mgr._tasks[tid]["fh"].closed
