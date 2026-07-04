"""ShellTools: argv-only, cwd confinement, and the permission-mode behavior."""
from __future__ import annotations

import sys

from looplab.tools.shell_tools import ShellTools

ALLOW = lambda a: "allow_once"      # noqa: E731
DENY = lambda a: "deny"             # noqa: E731


def test_plan_mode_disables_shell(tmp_path):
    r = ShellTools([tmp_path], mode="plan").execute("run_command", {"command": ["echo", "hi"]})
    assert "disabled in plan" in r


def test_auto_runs_and_captures_output(tmp_path):
    r = ShellTools([tmp_path], mode="auto").execute(
        "run_command", {"command": [sys.executable, "-c", "print('hello123')"]})
    assert "exit=0" in r and "hello123" in r


def test_requires_argv_list(tmp_path):
    r = ShellTools([tmp_path], mode="auto").execute("run_command", {"command": "echo hi"})
    assert "argv LIST" in r


def test_cwd_confined_to_roots(tmp_path):
    r = ShellTools([tmp_path], mode="auto").execute(
        "run_command", {"command": ["echo", "hi"], "cwd": "/etc"})
    assert "outside" in r


def test_ask_allow_and_deny(tmp_path):
    assert "exit=0" in ShellTools([tmp_path], mode="default", approver=ALLOW).execute(
        "run_command", {"command": [sys.executable, "-c", "print(1)"]})
    assert "declined" in ShellTools([tmp_path], mode="default", approver=DENY).execute(
        "run_command", {"command": [sys.executable, "-c", "print(1)"]})


def test_secret_env_not_leaked(tmp_path, monkeypatch):
    # A secret-named env var must not reach the child's stdout (sandbox._run_argv scrubs it).
    monkeypatch.setenv("MY_API_KEY", "supersecret-xyz")
    r = ShellTools([tmp_path], mode="auto").execute(
        "run_command", {"command": [sys.executable, "-c",
                                    "import os;print(os.environ.get('MY_API_KEY','<absent>'))"]})
    assert "supersecret-xyz" not in r and "<absent>" in r
