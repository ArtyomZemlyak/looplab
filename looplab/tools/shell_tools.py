"""Shell/command tool provider for the assistant: run argv commands (no shell) confined to the
allowed roots, capped and timed out, gated by the permission MODE. Under a non-trusted trust_mode the
command runs inside `docker run --network none` (a real boundary) via `command_eval.make_docker_wrap`.

Same `.specs()`/`.execute()` shape as the other providers. Commands are an argv LIST (never a shell
string) so there is no shell-injection surface; the child's environment already has secret-looking
vars scrubbed by `sandbox._run_argv`. In `plan` mode shell is disabled (argv can't be reliably
classified as read-only); in `default`/`acceptEdits` it asks; in `auto` it runs inline.

TRUST BOUNDARY (important): unlike the read/write/scout providers, shell places NO `looks_secret`
gate on the FILES a command reads — only `cwd` is confined to the roots. Under `trusted_local` this is
by design (the operator runs their own code on their own box; env-var scrubbing is the only hardening,
and the module makes no security claim). But it means that once shell is enabled in `auto` (or
approved in a confirm mode), `run_command ["cat", "~/.ssh/id_rsa"]` returns the key to the model — the
per-file secret gate is NOT a boundary here. The real boundary for untrusted code is the `untrusted`
trust_mode's `docker run --network none` wrap, not the secret gate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

from looplab.core import _pathsafe
from looplab.tools._base import RESULT_CAP, fn_spec
from looplab.tools.perm_modes import decide, default_approver

_MAX_OUTPUT = 64_000
_MAX_TIMEOUT = 600.0

# Per-STREAM tail budgets for run_command's reply. The agent loop caps the COMBINED result at
# RESULT_CAP (head-keep), so giving each stream ~RESULT_CAP alone let a verbose stdout push the whole
# stderr section — the traceback, i.e. the reason the command failed — past the cap, where the loop
# silently dropped it. Split the cap between the streams instead, stderr getting the larger share
# (the exception line lives there), leaving headroom for the exit-code head + section labels +
# truncation notes so head + both tails always fit under RESULT_CAP.
_STDOUT_TAIL = RESULT_CAP // 2 - 200
_STDERR_TAIL = RESULT_CAP // 2 - 100

# Only these host GIT_* vars are passed through to a `git` child (see exec_argv): the multi-var config
# (which `_run_argv` would partially scrub because GIT_CONFIG_KEY_* contains "KEY") + commit identity.
# Deliberately EXCLUDES credential-bearing vars (GIT_ASKPASS, GIT_SSH_COMMAND, GIT_HTTP_EXTRAHEADER,
# GIT_TOKEN, …) so a token can't reach a git subprocess whose stdout is returned to a remote model.
_GIT_IDENTITY = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}

# Substrings in a git-config KEY NAME that mark the pair as credential-bearing. `GIT_CONFIG_KEY_n` /
# `GIT_CONFIG_VALUE_n` pairs (the multi-var config protocol) can smuggle the SAME secret the exclusion
# above blocks — e.g. KEY_0=http.extraheader VALUE_0="Authorization: Bearer <token>" (a common CI
# injection). Passing those through would let `git config --list` print the token into a tool result
# returned to (and traced for) a possibly-remote model. Matched on the config KEY name (not a broad
# "credential", which would wrongly drop the SAFE `credential.interactive=false` prompt-disabler).
_GIT_CRED_KEY_MARKERS = ("extraheader", "askpass", "sshcommand", "proxyauthorization")


def git_config_env() -> dict:
    import os as _os
    env = _os.environ
    out: dict = {}
    # Pass through commit identity + any non-COUNT-protocol GIT_CONFIG_* (GLOBAL/SYSTEM/NOSYSTEM…).
    for k, v in env.items():
        if k in _GIT_IDENTITY:
            out[k] = v
        elif (k.startswith("GIT_CONFIG_") and k != "GIT_CONFIG_COUNT"
              and not k.startswith("GIT_CONFIG_KEY_") and not k.startswith("GIT_CONFIG_VALUE_")):
            out[k] = v
    # Rebuild the KEY_i/VALUE_i sequence CONTIGUOUSLY, dropping credential-bearing pairs. git requires
    # indices 0..COUNT-1 with no gaps, so a pair can't just be deleted (git would 'missing config key'
    # and abort) — survivors are renumbered and COUNT is reset to the kept length.
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except (TypeError, ValueError):
        count = 0
    kept: list = []
    for i in range(max(0, count)):
        key = env.get(f"GIT_CONFIG_KEY_{i}")
        if key is None:
            continue
        val = env.get(f"GIT_CONFIG_VALUE_{i}")
        kname, vval = key.lower(), (val or "").lower()
        credentialish = (any(m in kname for m in _GIT_CRED_KEY_MARKERS)
                         or "authorization:" in vval or "bearer " in vval)
        if credentialish:
            continue
        kept.append((key, val))
    for j, (key, val) in enumerate(kept):
        out[f"GIT_CONFIG_KEY_{j}"] = key
        # ALWAYS emit VALUE_j (empty when the original had none): the child inherits the host env, in
        # which sandbox `_run_argv` scrubs GIT_CONFIG_KEY_* (name contains "KEY") but NOT VALUE_* — so a
        # renumbered survivor with no value would otherwise leave a STALE GIT_CONFIG_VALUE_j (a dropped
        # credential's value) visible at that index. Emitting our own value shadows it.
        out[f"GIT_CONFIG_VALUE_{j}"] = val if val is not None else ""
    if "GIT_CONFIG_COUNT" in env:
        out["GIT_CONFIG_COUNT"] = str(len(kept))
    return out


def _tail(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else "…(truncated)…\n" + s[-n:]


class ShellTools:
    def __init__(self, roots, mode: str = "plan", trust_mode: str = "trusted_local",
                 approver: Optional[Callable[[dict], str]] = None, timeout: float = 120.0,
                 max_output: int = _MAX_OUTPUT, image: str = "python:3.12-slim",
                 default_cwd=None):
        self._roots = _pathsafe.resolve_roots(roots)
        # Where a command runs when the model gives no cwd. The spec promises "default: repo root" —
        # without an explicit value we can only fall back to the first root (which in the assistant's
        # toolset is $HOME: run_tests there would collect every project under the home dir).
        self._default_cwd = Path(default_cwd).resolve() if default_cwd else None
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
            fn_spec("run_command",
                     "Run a command as an ARGV LIST (no shell) inside the allowed roots — e.g. "
                     '["python","-m","pytest","-q","tests/test_patch.py"]. Returns exit code + '
                     f"stdout/stderr, each as a TAIL (stdout ~last {_STDOUT_TAIL} chars, stderr ~last "
                     f"{_STDERR_TAIL} — earlier output is "
                     "dropped, with a truncation note). Pass argv, NOT a shell string. A foreground "
                     f"command is KILLED at `timeout` seconds (default {int(self.timeout)}, hard max "
                     f"{int(_MAX_TIMEOUT)}); set "
                     "background=true for anything longer (full test run, training, build): it returns "
                     "a task_id immediately; poll read_output(task_id) for progress.",
                     {"command": {"type": "array", "items": {"type": "string"}},
                      "cwd": {"type": "string", "description": "working dir (default: repo root)"},
                      "timeout": {"type": "number", "description": "seconds before the command is "
                                  f"killed (default {int(self.timeout)}, max {int(_MAX_TIMEOUT)})"},
                      "background": {"type": "boolean"}}, ["command"]),
            fn_spec("run_tests",
                     "Run the test suite (or a subset) with pytest -q. Convenience wrapper over "
                     "run_command.",
                     {"path": {"type": "string", "description": "a test file/dir (default: all)"}}, []),
            fn_spec("read_output",
                     "Read NEW output from a background command since your last read, plus its "
                     "running/exited status. One bounded chunk per poll: a reply ending with "
                     "'(more output pending — poll read_output again)' means the log has more — the "
                     "next call continues exactly where this reply ended (nothing is skipped). Use "
                     "the task_id from a background run_command.",
                     {"task_id": {"type": "string"}}, ["task_id"]),
            fn_spec("list_background",
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
                from looplab.runtime.bg_tasks import MANAGER
                r = MANAGER.read(str(args.get("task_id") or ""))
                if not r.get("ok"):
                    return f"({r.get('error')})"
                head = f"[{r['task_id']}] {r['status']}" + (f" exit={r['exit_code']}" if r["exit_code"] is not None else "")
                body = ("\n" + r["new_output"]) if r["new_output"].strip() else " (no new output)"
                # Backpressure marker: the manager returned one bounded chunk and left the cursor at
                # its end, so the model knows to poll again instead of assuming it saw everything.
                more = "\n(more output pending — poll read_output again)" if r.get("pending") else ""
                return head + body + more
            if name == "list_background":
                from looplab.runtime.bg_tasks import MANAGER
                rows = MANAGER.list()
                return "\n".join(f"{t['task_id']} {t['status']} · {t['cmd'][:70]}" for t in rows) or "(none)"
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never crash the loop
            return f"(error: {e})"

    def _cwd(self, cwd: Optional[str]) -> Optional[Path]:
        if not cwd:
            return self._default_cwd or (self._roots[0] if self._roots else Path.cwd())
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
        # Clamp to a positive window: a negative/zero `timeout` is truthy and would otherwise reach
        # communicate(timeout<=0) and kill the child instantly.
        to = max(1.0, min(float(timeout or self.timeout), _MAX_TIMEOUT))
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
            from looplab.runtime.command_eval import make_docker_wrap
            self._wrap = make_docker_wrap(str(self._roots[0]), self.image, network="none")
        if background:
            from looplab.runtime.bg_tasks import MANAGER
            self.applied.append(action)
            tid = MANAGER.start(argv, str(wd), wrap=self._wrap)
            return f"(started background task {tid} — poll read_output(\"{tid}\") for progress)"
        full_argv = self._wrap(argv, str(wd)) if self._wrap else argv
        # `run_argv` scrubs env vars whose NAME looks secret (…KEY…), which would drop only PART of a
        # multi-var git config (GIT_CONFIG_KEY_0 gone, GIT_CONFIG_COUNT kept) and break `git` with
        # "missing config key". For a git command, pass back ONLY the host's git config + identity vars
        # (NOT credential-bearing GIT_ASKPASS/SSH_COMMAND/HTTP_EXTRAHEADER) so git works without leaking
        # a token into output the model sees.
        env = git_config_env() if (argv and argv[0] == "git") else None
        from looplab.runtime.sandbox import run_argv
        rc, out, err, timed_out = run_argv(full_argv, str(wd), to, env=env, max_output_bytes=self.max_output)
        if d != "inline" or tool_kind != "git_ro":     # record real mutations/commands (not ro peeks)
            self.applied.append(action)
        head = f"exit={rc}" + (" (TIMEOUT)" if timed_out else "")
        parts = [head]
        if out and out.strip():
            parts.append("stdout:\n" + _tail(out, _STDOUT_TAIL))
        if err and err.strip():
            parts.append("stderr:\n" + _tail(err, _STDERR_TAIL))
        return "\n".join(parts)
