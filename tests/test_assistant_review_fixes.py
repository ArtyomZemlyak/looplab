"""Regression tests for the code-review fixes on the assistant work."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.bg_tasks import BackgroundManager  # noqa: E402
from looplab.shell_tools import git_config_env  # noqa: E402
from looplab.write_tools import FileBackups, WriteTools  # noqa: E402


def test_secret_check_is_root_relative_not_absolute(tmp_path):
    # A root whose absolute path contains a secret-named component (e.g. ".docker") must NOT poison
    # every file under it — the secret check is on the root-relative path.
    root = tmp_path / ".docker" / "workspace"
    root.mkdir(parents=True)
    w = WriteTools([root], mode="auto")
    assert "wrote" in w.execute("write_file", {"path": str(root / "a.py"), "content": "x"})
    assert (root / "a.py").read_text() == "x"
    # a real secret INSIDE the workspace is still refused (relative path matches)
    assert "secret" in w.execute("write_file", {"path": str(root / ".env"), "content": "K=1"})
    assert "secret" in w.execute("write_file", {"path": str(root / ".ssh" / "id_rsa"), "content": "k"})


def test_git_config_env_excludes_credentials(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_ASKPASS", "/leak/askpass")
    monkeypatch.setenv("GIT_HTTP_EXTRAHEADER", "Authorization: Bearer secrettoken")
    env = git_config_env()
    assert env.get("GIT_CONFIG_KEY_0") == "user.name" and "GIT_CONFIG_COUNT" in env
    assert "GIT_AUTHOR_NAME" in env
    assert "GIT_ASKPASS" not in env and "GIT_HTTP_EXTRAHEADER" not in env


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


def test_file_backups_index_survives_a_gap(tmp_path):
    b = FileBackups(tmp_path / "bak")
    f = tmp_path / "a.txt"; f.write_text("v0")
    b.save(f); f.write_text("v1")           # 0.bak = v0
    b.save(f); f.write_text("v2")           # 1.bak = v1
    assert b.revert(f) and f.read_text() == "v1"   # pops 1.bak -> gap; content restored to v1
    b.save(f); f.write_text("v3")           # next index must be max+1 = 1 (not len()=1 collision-safe)
    # revert now restores the snapshot taken just before v3 (which was v1)
    assert b.revert(f) and f.read_text() == "v1"
