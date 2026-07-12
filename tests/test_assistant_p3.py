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


def _drain(mgr, tid):
    """Poll to completion, returning the chunks in cursor order."""
    chunks = []
    r = mgr.read(tid)
    chunks.append(r["new_output"])
    while r["pending"]:
        r = mgr.read(tid)
        chunks.append(r["new_output"])
    return chunks


def _start_finished(mgr, tmp_path):
    """A background task whose child has already exited (so tests can append to its log directly)."""
    tid = mgr.start([sys.executable, "-c", "pass"], str(tmp_path))
    for _ in range(200):
        if mgr._tasks[tid]["proc"].poll() is not None:
            break
        time.sleep(0.05)
    return tid


def test_background_concurrent_polls_lose_nothing(tmp_path):
    """H1: read() must hold the lock across cursor-read → slice → cursor-advance. Two concurrent
    polls that both read the cursor and both `+=` it would jointly advance it past a chunk only one
    of them returned — a permanently SKIPPED chunk. Chunks are unique ordered markers, so the drained
    union must reassemble the exact payload."""
    import threading
    mgr = BackgroundManager()
    tid = _start_finished(mgr, tmp_path)
    payload = "".join(f"<{i:05d}>" for i in range(4000))          # ~28KB of unique ordered markers
    with open(mgr._tasks[tid]["log"], "ab") as f:
        f.write(payload.encode())
    got, lock = [], threading.Lock()

    def _poll():
        while True:
            r = mgr.read(tid)
            with lock:
                if r["new_output"]:
                    got.append(r["new_output"])
            if not r["pending"]:
                return

    threads = [threading.Thread(target=_poll) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # Chunks may be COLLECTED out of order across the two threads; each is a contiguous unique
    # slice, so reassemble by payload position — any lost/duplicated chunk breaks the equality.
    assert "".join(sorted(got, key=payload.index)) == payload


def test_background_read_is_seek_based_and_matches_the_old_chunking(tmp_path):
    """H2a: the incremental seek-read returns exactly the same complementary chunks the whole-log
    read produced for a small log (same budget, same cursor semantics)."""
    from looplab.runtime.bg_tasks import _MAX_READ
    mgr = BackgroundManager()
    tid = _start_finished(mgr, tmp_path)
    payload = "".join(f"<{i:04d}>" for i in range(1667))          # ~10KB of unique markers
    with open(mgr._tasks[tid]["log"], "ab") as f:
        f.write(payload.encode())
    chunks = _drain(mgr, tid)
    assert all(len(c) <= _MAX_READ for c in chunks)
    assert len(chunks) >= 2 and "".join(chunks) == payload        # complementary — nothing lost


def test_background_backlog_over_cap_is_skipped_with_an_explicit_note(tmp_path):
    """H2b: an unread backlog beyond _BACKLOG_CAP is not drained by doomed catch-up polls — the
    cursor jumps to the newest _BACKLOG_CAP bytes and the chunk STARTS with an explicit
    '…(N bytes of older output skipped — full log: …)…' note (honest truncation); within the cap
    nothing is silently lost."""
    from looplab.runtime.bg_tasks import _BACKLOG_CAP
    mgr = BackgroundManager()
    tid = _start_finished(mgr, tmp_path)
    payload = "".join(f"<{i:07d}>" for i in range(40_000))        # 360KB >> the 256KB backlog cap
    log = mgr._tasks[tid]["log"]
    with open(log, "ab") as f:
        f.write(payload.encode())
    chunks = _drain(mgr, tid)
    skipped = len(payload) - _BACKLOG_CAP
    note = f"…({skipped} bytes of older output skipped — full log: {log})…\n"
    assert chunks[0].startswith(note)                             # the drop is announced, with count+path
    recovered = chunks[0][len(note):] + "".join(chunks[1:])
    assert recovered == payload[-_BACKLOG_CAP:]                   # the newest cap-worth arrives intact
    assert not any("skipped" in c for c in chunks[1:])            # the note fires once, on the jump poll


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


def test_kill_background_stops_a_running_task(tmp_path):
    mgr = BackgroundManager()
    tid = mgr.start([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path))
    assert mgr.read(tid)["status"] == "running"
    r = mgr.kill(tid)
    assert r["ok"] and r["status"] == "killed"
    for _ in range(100):                       # SIGTERM is async — poll to the terminal state
        if mgr.read(tid)["status"] == "exited":
            break
        time.sleep(0.05)
    assert mgr.read(tid)["status"] == "exited"
    assert mgr.kill("nope")["ok"] is False     # unknown id degrades gracefully


def test_shell_kill_background_tool(tmp_path):
    s = ShellTools([tmp_path], mode="auto")
    r = s.execute("run_command",
                  {"command": [sys.executable, "-c", "import time; time.sleep(30)"], "background": True})
    tid = r.split("task ")[1].split(" ")[0]
    assert "killed" in s.execute("kill_background", {"task_id": tid})
    assert "(" in s.execute("kill_background", {"task_id": "nope"})   # graceful note, no crash


def test_kill_background_goes_through_ask_mode_approver(tmp_path):
    """arch-review §3 P0-6: kill_background is a side effect, so in the DEFAULT (ask) mode it must ask
    the approver — the old code checked only plan-mode `deny` and killed with no approval."""
    launcher = ShellTools([tmp_path], mode="auto")     # launch in auto (MANAGER is process-global)
    r = launcher.execute("run_command",
                         {"command": [sys.executable, "-c", "import time; time.sleep(30)"], "background": True})
    tid = r.split("task ")[1].split(" ")[0]
    # default mode + DENY approver: the kill is declined and the task survives
    denied = ShellTools([tmp_path], mode="default", approver=lambda a: "deny")
    assert "declined" in denied.execute("kill_background", {"task_id": tid})
    # default mode + ALLOW approver: the kill goes through
    allowed = ShellTools([tmp_path], mode="default", approver=lambda a: "allow_once")
    assert "killed" in allowed.execute("kill_background", {"task_id": tid})


def test_background_timeout_reaps_a_hung_task(tmp_path):
    # a wall-clock budget past which a hung/runaway child is SIGTERM'd (lazily, on read/list) so it
    # can't leak a process for the life of the server.
    mgr = BackgroundManager(max_seconds=0.05)
    tid = mgr.start([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path))
    time.sleep(0.2)                            # let the deadline pass
    r = mgr.read(tid)
    assert r["timed_out"] is True
    for _ in range(100):
        if mgr.read(tid)["status"] == "exited":
            break
        time.sleep(0.05)
    assert mgr.read(tid)["status"] == "exited"


def test_background_watcher_reaps_without_any_poll(tmp_path):
    # The always-on deadline watcher reaps a hung task past its budget even if NOBODY ever calls
    # read()/list(). The old lazy-only enforcement leaked such a process for the life of the server.
    mgr = BackgroundManager(max_seconds=0.1, watch_interval=0.05)
    try:
        tid = mgr.start([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path))
        proc = mgr._tasks[tid]["proc"]
        for _ in range(200):                   # NO read()/list() — only the watcher thread can reap it
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None         # watcher force-killed the hung child
        assert mgr._tasks[tid]["timed_out"] is True
    finally:
        mgr.shutdown()


def test_background_watcher_disabled_when_interval_zero(tmp_path):
    # watch_interval=0 keeps the old lazy-only behavior (no thread) — a hung task is NOT reaped until
    # a read()/list() poll. Guards the opt-out that tests / non-server callers rely on.
    mgr = BackgroundManager(max_seconds=0.1, watch_interval=0)
    tid = mgr.start([sys.executable, "-c", "import time; time.sleep(2)"], str(tmp_path))
    assert mgr._watcher is None                 # no watcher thread spawned
    time.sleep(0.3)                             # deadline passed, but nothing polled
    assert mgr._tasks[tid]["proc"].poll() is None   # still running — lazy-only, not reaped
    assert mgr.read(tid)["timed_out"] is True       # the poll enforces it


def test_background_evicts_oldest_finished_logs(tmp_path):
    mgr = BackgroundManager(max_finished=2)
    tids, logs = [], []
    for _ in range(4):
        tid = mgr.start([sys.executable, "-c", "pass"], str(tmp_path))
        tids.append(tid)
        logs.append(mgr._tasks[tid]["log"])
        for _ in range(100):                   # wait for it to finish before the next start's evict
            t = mgr._tasks.get(tid)
            if t is None or t["proc"].poll() is not None:
                break
            time.sleep(0.02)
    # 4 finished tasks, cap 2 → the oldest is evicted from the registry AND its tmp log unlinked.
    assert tids[0] not in mgr._tasks
    assert not logs[0].exists()
    assert tids[3] in mgr._tasks                # the newest is always retained
    retained = [t for t in mgr._tasks.values() if t["proc"].poll() is not None]
    assert len(retained) <= 3                   # bounded (≤ max_finished + the just-started one)


def test_kill_waits_and_reports_exit_code(tmp_path):
    # arch-review §4 P1-4: kill must WAIT for exit and report the ACTUAL outcome, not fire one SIGTERM
    # and claim success.
    mgr = BackgroundManager()
    tid = mgr.start([sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path))
    r = mgr.kill(tid)
    assert r["ok"] and r["status"] == "killed" and r.get("exit_code") is not None
    assert mgr._tasks[tid]["proc"].poll() is not None      # the process is actually gone


def test_kill_reaps_the_whole_tree(tmp_path):
    # arch-review §4 P1-4: killing the parent must reap its children too (tree kill), not orphan them.
    import os
    import time
    if os.name == "nt":
        import pytest
        pytest.skip("POSIX liveness probe (os.kill(pid, 0))")
    code = ("import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(60)\n")
    mgr = BackgroundManager()
    tid = mgr.start([sys.executable, "-c", code], str(tmp_path))
    child_pid = None
    for _ in range(60):
        out = mgr.read(tid).get("new_output", "")
        tok = out.strip().split()
        if tok and tok[0].isdigit():
            child_pid = int(tok[0]); break
        time.sleep(0.1)
    assert child_pid, "child pid not observed"
    assert mgr.kill(tid)["ok"]
    # the child must be gone within a moment (killpg / taskkill /T reaps the tree)
    gone = False
    for _ in range(50):
        try:
            os.kill(child_pid, 0)          # raises if the process no longer exists
            time.sleep(0.1)
        except OSError:
            gone = True; break
    assert gone, f"child {child_pid} survived the parent kill (tree not reaped)"
