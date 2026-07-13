"""WriteTools: path/secret/protect gating and the permission-mode behavior (deny / ask / inline)."""
from __future__ import annotations

from looplab.tools.write_tools import FileBackups, WriteTools

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


def test_write_grant_scope_hashes_full_payload_beyond_preview(tmp_path):
    from looplab.tools.perm_modes import classify_action

    actions = []
    tool = WriteTools(
        [tmp_path], mode="default", approver=lambda action: actions.append(action) or "deny")
    prefix = "x" * 5000
    tool.execute("write_file", {"path": str(tmp_path / "long.txt"), "content": prefix + "a"})
    tool.execute("write_file", {"path": str(tmp_path / "long.txt"), "content": prefix + "b"})

    assert len(actions) == 2 and actions[0]["preview"] == actions[1]["preview"]
    assert (classify_action(actions[0]).scope_digest
            != classify_action(actions[1]).scope_digest)


def test_write_grant_scope_binds_the_current_file_preimage(tmp_path):
    from looplab.tools.perm_modes import classify_action

    target = tmp_path / "preimage.txt"
    target.write_text("first", encoding="utf-8")
    actions = []
    tool = WriteTools(
        [tmp_path], mode="default", backup_dir=tmp_path / "backups",
        approver=lambda action: actions.append(action) or "deny")
    tool.execute("write_file", {"path": str(target), "content": "desired"})
    target.write_text("changed elsewhere", encoding="utf-8")
    tool.execute("write_file", {"path": str(target), "content": "desired"})

    assert len(actions) == 2
    assert (classify_action(actions[0]).scope_digest
            != classify_action(actions[1]).scope_digest)


def test_reversible_write_aborts_when_recovery_snapshot_fails(tmp_path):
    target = tmp_path / "protected-by-receipt.txt"
    target.write_text("before", encoding="utf-8")
    blocked = tmp_path / "backup-path-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    result = WriteTools(
        [tmp_path], mode="auto", backup_dir=blocked).execute(
            "write_file", {"path": str(target), "content": "after"})

    assert "could not create a recovery snapshot" in result
    assert target.read_text(encoding="utf-8") == "before"


def test_reversible_delete_aborts_when_recovery_snapshot_fails(tmp_path):
    target = tmp_path / "must-survive.txt"
    target.write_text("before", encoding="utf-8")
    blocked = tmp_path / "backup-path-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    result = WriteTools(
        [tmp_path], mode="auto", backup_dir=blocked, approver=ALLOW).execute(
            "delete_file", {"path": str(target)})

    assert "could not create a recovery snapshot" in result
    assert target.read_text(encoding="utf-8") == "before"


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
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert "declined" in WriteTools([tmp_path], mode="default", approver=DENY).execute(
        "delete_file", {"path": str(f)})
    assert f.exists()
    assert "deleted" in WriteTools(
        [tmp_path], mode="auto", approver=ALLOW).execute("delete_file", {"path": str(f)})
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


def test_apply_patch_uses_default_protect_exceptions_but_honors_custom_protect(tmp_path):
    import subprocess

    diff = ("diff --git a/upgrade.py b/upgrade.py\n--- a/upgrade.py\n+++ b/upgrade.py\n"
            "@@ -1 +1 @@\n-before\n+after\n")

    default_repo = tmp_path / "default"
    default_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=default_repo, check=True)
    (default_repo / "upgrade.py").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=default_repo, check=True)
    allowed = WriteTools([default_repo], mode="auto", repo_root=default_repo).execute(
        "apply_patch", {"diff": diff})
    assert "applied" in allowed
    assert (default_repo / "upgrade.py").read_text(encoding="utf-8") == "after\n"

    custom_repo = tmp_path / "custom"
    custom_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=custom_repo, check=True)
    (custom_repo / "upgrade.py").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=custom_repo, check=True)
    refused = WriteTools(
        [custom_repo], mode="auto", repo_root=custom_repo,
        protect=["**/upgrade.py"],
    ).execute("apply_patch", {"diff": diff})
    assert "protected paths" in refused
    assert (custom_repo / "upgrade.py").read_text(encoding="utf-8") == "before\n"


# --- secret-check is root-relative; backup index is gap-safe (assistant review fixes) -------------

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


def test_file_backups_index_survives_a_gap(tmp_path):
    b = FileBackups(tmp_path / "bak")
    f = tmp_path / "a.txt"
    f.write_text("v0")
    b.save(f)
    f.write_text("v1")           # 0.bak = v0
    b.save(f)
    f.write_text("v2")           # 1.bak = v1
    assert b.revert(f) and f.read_text() == "v1"   # pops 1.bak -> gap; content restored to v1
    b.save(f)
    f.write_text("v3")           # next index must be max+1 = 1 (not len()=1 collision-safe)
    # revert now restores the snapshot taken just before v3 (which was v1)
    assert b.revert(f) and f.read_text() == "v1"
