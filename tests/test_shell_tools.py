"""ShellTools: argv-only, cwd confinement, and the permission-mode behavior."""
from __future__ import annotations

import json
import sys

from looplab.tools.shell_tools import ShellTools, git_config_env

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


# --- git_config_env: credential stripping while keeping indices contiguous ------------------------

def test_git_config_env_drops_extraheader_credential_and_keeps_indices_contiguous(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("GIT_CONFIG") or k.startswith("GIT_AUTHOR"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "3")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.interactive")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "http.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "Authorization: Bearer SECRETTOKEN")
    monkeypatch.setenv("GIT_CONFIG_KEY_2", "url.https://github.com/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_2", "git@github.com:")
    env = git_config_env()
    assert "SECRETTOKEN" not in json.dumps(env)                 # credential dropped
    n = int(env["GIT_CONFIG_COUNT"])
    assert n == 2                                               # count reflects survivors
    assert all(f"GIT_CONFIG_KEY_{i}" in env for i in range(n))  # indices contiguous (git needs this)
    keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(n)}
    assert "credential.interactive" in keys                     # safe non-credential config kept


def test_git_config_env_shadows_stale_value_for_valueless_survivor(monkeypatch):
    """A renumbered survivor with no original value must emit an EMPTY VALUE so it shadows a stale
    GIT_CONFIG_VALUE_i (a dropped credential) the child inherits from the host env."""
    for k in list(__import__("os").environ):
        if k.startswith("GIT_CONFIG") or k.startswith("GIT_AUTHOR"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Bearer TOKEN")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.name")   # survivor WITHOUT a paired VALUE
    env = git_config_env()
    assert "TOKEN" not in json.dumps(env)
    n = int(env["GIT_CONFIG_COUNT"])
    for i in range(n):                                     # every kept index emits an authoritative VALUE
        assert f"GIT_CONFIG_VALUE_{i}" in env


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
