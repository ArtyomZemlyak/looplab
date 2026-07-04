"""GitTools: read-only verbs run in any mode; mutating verbs follow the ask/deny rule."""
from __future__ import annotations

import subprocess

from looplab.tools.git_tools import GitTools
from looplab.tools.shell_tools import ShellTools

ALLOW = lambda a: "allow_once"      # noqa: E731
DENY = lambda a: "deny"             # noqa: E731


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    return tmp_path


def test_readonly_verbs_run_in_plan_mode(tmp_path):
    _repo(tmp_path)
    g = GitTools(ShellTools([tmp_path], mode="plan"), cwd=tmp_path)
    assert "exit=0" in g.execute("git_status", {})     # read-only verb runs even in plan mode
    assert "exit=" in g.execute("git_log", {})         # runs (128 before any commit exists)
    assert "?? a.txt" in g.execute("git_status", {})


def test_mutating_verbs_denied_in_plan(tmp_path):
    _repo(tmp_path)
    g = GitTools(ShellTools([tmp_path], mode="plan"), cwd=tmp_path)
    assert "disabled in plan" in g.execute("git_add", {"paths": ["a.txt"]})


def test_mutating_verbs_ask(tmp_path):
    _repo(tmp_path)
    g_yes = GitTools(ShellTools([tmp_path], mode="default", approver=ALLOW), cwd=tmp_path)
    assert "exit=0" in g_yes.execute("git_add", {"paths": ["a.txt"]})
    g_no = GitTools(ShellTools([tmp_path], mode="default", approver=DENY), cwd=tmp_path)
    assert "declined" in g_no.execute("git_commit", {"message": "x"})


def test_auto_commits(tmp_path):
    _repo(tmp_path)
    g = GitTools(ShellTools([tmp_path], mode="auto"), cwd=tmp_path)
    g.execute("git_add", {"paths": ["a.txt"]})
    assert "exit=0" in g.execute("git_commit", {"message": "init"})
    assert "init" in g.execute("git_log", {})
