"""P2b: snapshot revert of file edits, and the visible TODO list."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.assistant import run_turn  # noqa: E402
from looplab.write_tools import FileBackups, WriteTools  # noqa: E402


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _final(reply):
    return _call("final_answer", {"reply": reply})


class _FakeChatClient:
    def __init__(self, scripted):
        self.scripted = list(scripted)

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)


def test_file_backups_restore_and_delete(tmp_path):
    b = FileBackups(tmp_path / "bak")
    f = tmp_path / "a.txt"; f.write_text("v1")
    b.save(f); f.write_text("v2")
    assert b.revert(f) and f.read_text() == "v1"
    # a file that did not exist before is removed on revert
    g = tmp_path / "new.txt"
    b.save(g); g.write_text("created")
    assert b.revert(g) and not g.exists()


def test_write_tool_revert(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("original\n")
    w = WriteTools([tmp_path], mode="auto", backup_dir=tmp_path / "bak")
    w.execute("edit_file", {"path": str(f), "old_str": "original", "new_str": "changed"})
    assert f.read_text() == "changed\n"
    assert w.applied[0].get("abs_path")
    assert "reverted" in w.execute("revert_file", {"path": str(f)})
    assert f.read_text() == "original\n"


def test_run_turn_surfaces_todos(tmp_path):
    seen = []
    todos = [{"content": "step one", "status": "in_progress"}, {"content": "step two", "status": "pending"}]
    client = _FakeChatClient([_call("write_todos", {"todos": todos}), _final("working on it")])
    res = run_turn(client, tmp_path, [], "do a multi-step thing", "plan", on_todos=seen.append)
    assert res["todos"] == todos           # surfaced in the result
    assert seen and seen[-1] == todos      # and streamed live via the sink
