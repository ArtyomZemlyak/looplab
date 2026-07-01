"""WriteTools: path/secret/protect gating and the permission-mode behavior (deny / ask / inline)."""
from __future__ import annotations

from looplab.write_tools import WriteTools

ALLOW = lambda a: "allow_once"      # noqa: E731
DENY = lambda a: "deny"             # noqa: E731


def test_plan_mode_refuses_and_does_not_write(tmp_path):
    w = WriteTools([tmp_path], mode="plan")
    r = w.execute("write_file", {"path": str(tmp_path / "a.txt"), "content": "x"})
    assert "plan mode" in r and not (tmp_path / "a.txt").exists()


def test_auto_writes_inline(tmp_path):
    w = WriteTools([tmp_path], mode="auto")
    r = w.execute("write_file", {"path": str(tmp_path / "a.txt"), "content": "hello"})
    assert "wrote" in r and (tmp_path / "a.txt").read_text() == "hello"
    assert w.applied and w.applied[0]["tool"] == "write_file"


def test_ask_allow_writes_ask_deny_does_not(tmp_path):
    WriteTools([tmp_path], mode="default", approver=ALLOW).execute(
        "write_file", {"path": str(tmp_path / "y.txt"), "content": "1"})
    assert (tmp_path / "y.txt").read_text() == "1"
    r = WriteTools([tmp_path], mode="default", approver=DENY).execute(
        "write_file", {"path": str(tmp_path / "z.txt"), "content": "1"})
    assert "declined" in r and not (tmp_path / "z.txt").exists()


def test_path_escape_refused(tmp_path):
    w = WriteTools([tmp_path], mode="auto")
    assert "outside" in w.execute("write_file", {"path": "/etc/passwd", "content": "x"})
    assert "outside" in w.execute("write_file", {"path": str(tmp_path / ".." / "esc.txt"), "content": "x"})


def test_secret_and_protected_refused(tmp_path):
    w = WriteTools([tmp_path], mode="auto")
    assert "secret" in w.execute("write_file", {"path": str(tmp_path / ".env"), "content": "K=1"})
    assert "protected" in w.execute("write_file", {"path": str(tmp_path / "events.jsonl"), "content": "x"})
    assert not (tmp_path / "events.jsonl").exists()


def test_edit_file_match_counting(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha beta alpha")
    w = WriteTools([tmp_path], mode="auto")
    assert "not found" in w.execute("edit_file", {"path": str(f), "old_str": "zzz", "new_str": "q"})
    assert "appears 2" in w.execute("edit_file", {"path": str(f), "old_str": "alpha", "new_str": "A"})
    r = w.execute("edit_file", {"path": str(f), "old_str": "beta", "new_str": "B"})
    assert "edited" in r and f.read_text() == "alpha B alpha"


def test_delete_gated(tmp_path):
    f = tmp_path / "a.txt"; f.write_text("x")
    assert "declined" in WriteTools([tmp_path], mode="default", approver=DENY).execute(
        "delete_file", {"path": str(f)})
    assert f.exists()
    assert "deleted" in WriteTools([tmp_path], mode="auto").execute("delete_file", {"path": str(f)})
    assert not f.exists()


def test_apply_patch_gated(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path)
    diff = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
            "@@ -1 +1 @@\n-one\n+two\n")
    w = WriteTools([tmp_path], mode="auto", repo_root=tmp_path)
    r = w.execute("apply_patch", {"diff": diff})
    assert "applied" in r and (tmp_path / "a.txt").read_text() == "two\n"
