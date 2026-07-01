"""Shell/command tool provider for the assistant: run argv commands (no shell) confined to the
allowed roots, capped and timed out, gated by the permission MODE. Under a non-trusted trust_mode the
command runs inside `docker run --network none` (a real boundary) via `command_eval.make_docker_wrap`.

Same `.specs()`/`.execute()` shape as the other providers. Commands are an argv LIST (never a shell
string) so there is no shell-injection surface; the child's environment already has secret-looking
vars scrubbed by `sandbox._run_argv`. In `plan` mode shell is disabled (argv can't be reliably
classified as read-only); in `default`/`acceptEdits` it asks; in `auto` it runs inline.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

from . import _pathsafe
from .knowledge_tools import _fn_spec
from .perm_modes import decide, default_approver

_MAX_OUTPUT = 64_000
_MAX_TIMEOUT = 600.0

# Only these host GIT_* vars are passed through to a `git` child (see exec_argv): the multi-var config
# (which `_run_argv` would partially scrub because GIT_CONFIG_KEY_* contains "KEY") + commit identity.
# Deliberately EXCLUDES credential-bearing vars (GIT_ASKPASS, GIT_SSH_COMMAND, GIT_HTTP_EXTRAHEADER,
# GIT_TOKEN, …) so a token can't reach a git subprocess whose stdout is returned to a remote model.
_GIT_IDENTITY = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}


def git_config_env() -> dict:
    import os as _os
    return {k: v for k, v in _os.environ.items()
            if k.startswith("GIT_CONFIG_") or k in _GIT_IDENTITY}


def _tail(s: str, n: int = 4000) -> str:
    s = s or ""
    return s if len(s) <= n else "…(truncated)…\n" + s[-n:]


class ShellTools:
    def __init__(self, roots, mode: str = "plan", trust_mode: str = "trusted_local",
                 approver: Optional[Callable[[dict], str]] = None, timeout: float = 120.0,
                 max_output: int = _MAX_OUTPUT, image: str = "python:3.12-slim"):
        self._roots = _pathsafe.resolve_roots(roots)
        self.mode = mode
        self.trust_mode = trust_mode
        self.approver = approver or default_approver
        self.timeout = timeout
        self.max_output = max_output
        self.image = image
        self.applied: list[dict] = []
        self._wrap = None            # built lazily on first exec (fails loudly if docker is missing)

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            _fn_spec("run_command",
                     "Run a command as an ARGV LIST (no shell) inside the allowed roots — e.g. "
                     '["python","-m","pytest","-q","tests/test_patch.py"]. Returns exit code + '
                     "stdout/stderr. Pass argv, NOT a shell string. Set background=true for a LONG "
                     "command (full test run, training, build): it returns a task_id immediately; poll "
                     "read_output(task_id) for progress.",
                     {"command": {"type": "array", "items": {"type": "string"}},
                      "cwd": {"type": "string", "description": "working dir (default: repo root)"},
                      "timeout": {"type": "number"},
                      "background": {"type": "boolean"}}, ["command"]),
            _fn_spec("run_tests",
                     "Run the test suite (or a subset) with pytest -q. Convenience wrapper over "
                     "run_command.",
                     {"path": {"type": "string", "description": "a test file/dir (default: all)"}}, []),
            _fn_spec("read_output",
                     "Read NEW output from a background command since your last read, plus its "
                     "running/exited status. Use the task_id from a background run_command.",
                     {"task_id": {"type": "string"}}, ["task_id"]),
            _fn_spec("list_background",
                     "List background commands started this session with their status.", {}, []),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "run_command":
                return self._run(args.get("command"), args.get("cwd"), args.get("timeout"),
                                 background=bool(args.get("background")))
            if name == "run_tests":
                path = args.get("path") or ""
                argv = [sys.executable, "-m", "pytest", "-q"] + ([path] if path else [])
                return self._run(argv, None, None)
            if name == "read_output":
                from .bg_tasks import MANAGER
                r = MANAGER.read(str(args.get("task_id") or ""))
                if not r.get("ok"):
                    return f"({r.get('error')})"
                head = f"[{r['task_id']}] {r['status']}" + (f" exit={r['exit_code']}" if r["exit_code"] is not None else "")
                return head + ("\n" + r["new_output"] if r["new_output"].strip() else " (no new output)")
            if name == "list_background":
                from .bg_tasks import MANAGER
                rows = MANAGER.list()
                return "\n".join(f"{t['task_id']} {t['status']} · {t['cmd'][:70]}" for t in rows) or "(none)"
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never crash the loop
            return f"(error: {e})"

    def _cwd(self, cwd: Optional[str]) -> Optional[Path]:
        if not cwd:
            return self._roots[0] if self._roots else Path.cwd()
        return _pathsafe.resolve_within(self._roots, cwd)

    def _run(self, command, cwd, timeout, background=False) -> str:
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(x, str) for x in command):
            return "(run_command needs a non-empty argv LIST of strings, e.g. [\"ls\",\"-la\"])"
        argv = [str(x) for x in command]
        pretty = " ".join(argv)
        label = ("bg run: " if background else "run: ") + pretty[:80]
        return self.exec_argv(argv, cwd, "shell", label, timeout, background=background)

    def exec_argv(self, argv, cwd, tool_kind: str, label: str, timeout=None, background=False) -> str:
        """Shared gated exec used by run_command AND the git provider (so cwd-confinement, the docker
        wrap and the permission mode are enforced in ONE place). `tool_kind` picks the mode rule
        (shell / git_ro / git_mut)."""
        wd = self._cwd(cwd)
        if wd is None:
            return f"(refused: cwd {cwd} is outside the allowed roots)"
        to = min(float(timeout or self.timeout), _MAX_TIMEOUT)
        pretty = " ".join(argv)
        action = {"tool": argv[0] if argv else "", "tool_kind": tool_kind, "label": label,
                  "verb": f"run `{pretty[:80]}`", "preview": pretty, "cwd": str(wd)}
        d = decide(self.mode, tool_kind)
        if d == "deny":
            return ("(shell is disabled in plan mode. Switch to default/acceptEdits/auto to run "
                    "commands.)")
        if d == "ask":
            verdict = str(self.approver(action) or "deny")
            if not verdict.startswith("allow"):
                return f"(declined by the user: {pretty[:80]})"
        # Under a non-trusted tier, run inside docker (--network none). Built once; loud if unavailable.
        if self.trust_mode and self.trust_mode != "trusted_local" and self._wrap is None:
            from .command_eval import make_docker_wrap
            self._wrap = make_docker_wrap(str(self._roots[0]), self.image, network="none")
        if background:
            from .bg_tasks import MANAGER
            self.applied.append(action)
            tid = MANAGER.start(argv, str(wd), wrap=self._wrap)
            return f"(started background task {tid} — poll read_output(\"{tid}\") for progress)"
        run_argv = self._wrap(argv, str(wd)) if self._wrap else argv
        # `_run_argv` scrubs env vars whose NAME looks secret (…KEY…), which would drop only PART of a
        # multi-var git config (GIT_CONFIG_KEY_0 gone, GIT_CONFIG_COUNT kept) and break `git` with
        # "missing config key". For a git command, pass back ONLY the host's git config + identity vars
        # (NOT credential-bearing GIT_ASKPASS/SSH_COMMAND/HTTP_EXTRAHEADER) so git works without leaking
        # a token into output the model sees.
        env = git_config_env() if (argv and argv[0] == "git") else None
        from .sandbox import _run_argv
        rc, out, err, timed_out = _run_argv(run_argv, str(wd), to, env=env, max_output_bytes=self.max_output)
        if d != "inline" or tool_kind != "git_ro":     # record real mutations/commands (not ro peeks)
            self.applied.append(action)
        head = f"exit={rc}" + (" (TIMEOUT)" if timed_out else "")
        parts = [head]
        if out and out.strip():
            parts.append("stdout:\n" + _tail(out))
        if err and err.strip():
            parts.append("stderr:\n" + _tail(err))
        return "\n".join(parts)
