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
                     "stdout/stderr. Pass argv, NOT a shell string.",
                     {"command": {"type": "array", "items": {"type": "string"}},
                      "cwd": {"type": "string", "description": "working dir (default: repo root)"},
                      "timeout": {"type": "number"}}, ["command"]),
            _fn_spec("run_tests",
                     "Run the test suite (or a subset) with pytest -q. Convenience wrapper over "
                     "run_command.",
                     {"path": {"type": "string", "description": "a test file/dir (default: all)"}}, []),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "run_command":
                return self._run(args.get("command"), args.get("cwd"), args.get("timeout"))
            if name == "run_tests":
                path = args.get("path") or ""
                argv = [sys.executable, "-m", "pytest", "-q"] + ([path] if path else [])
                return self._run(argv, None, None)
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never crash the loop
            return f"(error: {e})"

    def _cwd(self, cwd: Optional[str]) -> Optional[Path]:
        if not cwd:
            return self._roots[0] if self._roots else Path.cwd()
        return _pathsafe.resolve_within(self._roots, cwd)

    def _run(self, command, cwd, timeout) -> str:
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(x, str) for x in command):
            return "(run_command needs a non-empty argv LIST of strings, e.g. [\"ls\",\"-la\"])"
        argv = [str(x) for x in command]
        pretty = " ".join(argv)
        return self.exec_argv(argv, cwd, "shell", f"run: {pretty[:80]}", timeout)

    def exec_argv(self, argv, cwd, tool_kind: str, label: str, timeout=None) -> str:
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
        run_argv = argv
        if self.trust_mode and self.trust_mode != "trusted_local":
            if self._wrap is None:
                from .command_eval import make_docker_wrap
                self._wrap = make_docker_wrap(str(self._roots[0]), self.image, network="none")
            run_argv = self._wrap(argv, str(wd))
        # `_run_argv` scrubs env vars whose NAME looks secret (…KEY…), which would drop only PART of a
        # multi-var git config (GIT_CONFIG_KEY_0 gone, GIT_CONFIG_COUNT kept) and break `git` with
        # "missing config key". For a git command, pass the host's GIT_* vars back so git sees the exact
        # host config it needs (identity, proxy routing) — restoring, not weakening, correctness.
        import os as _os
        env = ({k: v for k, v in _os.environ.items() if k.startswith("GIT_")}
               if argv and argv[0] == "git" else None)
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
