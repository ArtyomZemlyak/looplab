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

That wrap is built from `sandbox.docker_tier_kwargs` — the SAME translation the two eval tiers use —
so this surface is a full member of the tier rather than a container that resembles one. It was not
until 2026-08-15: it passed only `(root, image, network="none")`, which inherits every flag
`docker_run_argv` applies unconditionally and none of the caller-supplied ones, so it ran with no
`--memory` and, under `trust_mode="hostile"`, on the shared-kernel runtime.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from looplab.core import _pathsafe
from looplab.tools._base import RESULT_CAP, ToolCapability, clip, fn_spec
# The per-stream tail budgets moved DOWN to `_base.py` beside `clip`/`fit_rows`: `run_probe`
# (tools/dev_probe.py) reports the same two-stream shape and must not carry a second copy of the
# rule that decides which half of a failure survives the cap. Imported under this module's own
# spelling so its call site below is byte-identical.
from looplab.tools._base import STDOUT_TAIL as _STDOUT_TAIL  # noqa: F401  (kept for the spec text)
from looplab.tools._base import stream_tails as _stream_tails
from looplab.tools.perm_modes import (authorize, decide_action, default_approver,
                                      refusal_for)

_MAX_OUTPUT = 64_000
_MAX_TIMEOUT = 600.0

# Moved verbatim to core/gitenv.py so runtime/bg_tasks imports it DOWNWARD (it was the one
# runtime -> tools upward lazy import). Re-exported here because this module's own git subprocess
# path and the tests (`from looplab.tools.shell_tools import git_config_env`) spell this path.
from looplab.core.gitenv import (_GIT_CRED_KEY_MARKERS, _GIT_IDENTITY,  # noqa: F401
                                 git_config_env)


def _tail(s: str, n: int) -> str:
    """Keep the END of a command stream — that is where the failure is (doc 25 TO-08)."""
    return clip(s or "", n, keep="tail", note="…(truncated)…\n")


class ShellTools:
    def __init__(self, roots, mode: str = "plan", trust_mode: str = "trusted_local",
                 approver: Optional[Callable[[dict], str]] = None, timeout: float = 120.0,
                 max_output: int = _MAX_OUTPUT, image: Optional[str] = None,
                 default_cwd=None, settings=None):
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
        # The operator's container configuration, read through `sandbox.docker_tier_kwargs` at wrap
        # time (see `exec_argv`). `image=` stays an explicit per-instance OVERRIDE and defaults to
        # None — a second hardcoded copy of `Settings.docker_image`'s default is exactly how the two
        # ended up describing different containers on the same box.
        self._settings = settings
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
                     f"stdout/stderr, each as a TAIL (each stream keeps at least ~{RESULT_CAP // 2 - 200} "
                     f"chars when both are long; a short stream donates its unused budget to the other, "
                     f"up to ~{RESULT_CAP - 400} total — earlier output is "
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
                     "next call continues exactly where this reply ended (nothing is skipped). "
                     "Exception: if the unread backlog exceeds ~256KB, the OLDEST unread output is "
                     "dropped and the chunk STARTS with an explicit "
                     "'…(N bytes of older output skipped — full log: <path>)…' note. Use "
                     "the task_id from a background run_command.",
                     {"task_id": {"type": "string"}}, ["task_id"]),
            fn_spec("list_background",
                     "List background commands started this session with their status.", {}, []),
            fn_spec("kill_background",
                     "Stop a still-running background command (SIGTERM to its process group). Use the "
                     "task_id from a background run_command — e.g. to abandon a wedged test run or a "
                     "training you no longer need. A finished/unknown task_id returns a graceful note. "
                     "(Background commands are also auto-reaped after ~2h.)",
                     {"task_id": {"type": "string"}}, ["task_id"]),
        ]

    def capabilities(self) -> list[ToolCapability]:
        specs = {s["function"]["name"]: s["function"]["parameters"] for s in self.specs()}
        rows = []
        for name in ("run_command", "run_tests"):
            rows.append(ToolCapability(
                name=name, effect="execute", risk="high", idempotency="unknown",
                concurrency_safe=False, cancellable=False, approval="policy",
                input_schema=specs[name], source="assistant.shell.permission_mode"))
        for name in ("read_output", "list_background"):
            rows.append(ToolCapability(
                name=name, effect="read", risk="low", idempotency="conditional",
                concurrency_safe=False, cancellable=False, approval="never",
                input_schema=specs[name], source="assistant.background_tasks"))
        rows.append(ToolCapability(
            name="kill_background", effect="control", risk="medium",
            idempotency="idempotent", concurrency_safe=False, cancellable=False,
            approval="policy", input_schema=specs["kill_background"],
            source="assistant.shell.permission_mode"))
        return rows

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "run_command":
                return self._run(args.get("command"), args.get("cwd"), args.get("timeout"),
                                 background=bool(args.get("background")), action_id="run_command")
            if name == "run_tests":
                path = args.get("path") or ""
                argv = [sys.executable, "-m", "pytest", "-q"] + ([path] if path else [])
                return self._run(argv, None, None, action_id="run_tests")
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
            if name == "kill_background":
                # SIGTERM-ing a process group is a side effect, not a read (unlike read_output/
                # list_background): deny it in read-only plan mode AND require the ask-mode APPROVER in
                # `default` mode, exactly like run_command's gate. The old code checked ONLY `deny`, so
                # in the DEFAULT `ask` mode it killed the process-global task with no approval at all
                # (arch-review §3 P0-6: plan-mode deny does not satisfy ask-mode approval semantics).
                from looplab.runtime.bg_tasks import MANAGER
                tid = str(args.get("task_id") or "")
                action = {"tool": "kill_background", "tool_kind": "shell",
                          "label": f"kill background task {tid}",
                          "verb": f"kill background task `{tid}`", "preview": tid, "cwd": ""}
                refusal = authorize(
                    self.mode, self.approver, action,
                    denied="(kill_background is disabled in plan mode. Switch to "
                           "default/acceptEdits/auto.)",
                    declined=f"kill background {tid}")
                if refusal:
                    return refusal
                r = MANAGER.kill(tid)
                return f"[{r['task_id']}] killed" if r.get("ok") else f"({r.get('error')})"
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never crash the loop
            return f"(error: {e})"

    def _cwd(self, cwd: Optional[str]) -> Optional[Path]:
        if not cwd:
            return self._default_cwd or (self._roots[0] if self._roots else Path.cwd())
        return _pathsafe.resolve_within(self._roots, cwd)

    def _run(self, command, cwd, timeout, background=False, action_id="run_command") -> str:
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(x, str) for x in command):
            return "(run_command needs a non-empty argv LIST of strings, e.g. [\"ls\",\"-la\"])"
        argv = [str(x) for x in command]
        pretty = " ".join(argv)
        label = (("bg run: " if background else "run: ")
                 + (pretty[:79] + "…" if len(pretty) > 80 else pretty))
        return self.exec_argv(
            argv, cwd, "shell", label, timeout, background=background, action_id=action_id)

    def exec_argv(self, argv, cwd, tool_kind: str, label: str, timeout=None, background=False,
                  action_id=None) -> str:
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
        structured_preview = json.dumps(argv, ensure_ascii=False)
        argv_digest = hashlib.sha256(json.dumps(
            argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        action = {"tool": str(action_id or (argv[0] if argv else "")),
                  "tool_kind": tool_kind, "label": label,
                  "verb": f"run `{pretty[:80]}`", "preview": structured_preview, "cwd": str(wd),
                  "scope": {"cwd": str(wd), "argv_digest": argv_digest,
                            "background": bool(background), "timeout_seconds": to}}
        # `decide_action` + `refusal_for` rather than `authorize`: the DECISION itself is read again
        # below, where an `inline` read-only git peek is deliberately not recorded in `self.applied`.
        d = decide_action(self.mode, action)
        refusal = refusal_for(
            d, self.approver, action,
            denied=("(shell is disabled in plan mode. Switch to default/acceptEdits/auto to run "
                    "commands.)"),
            declined=pretty[:80])
        if refusal:
            return refusal
        # Under a non-trusted tier, run inside docker (--network none). Built once; loud if unavailable.
        # THIS SURFACE IS A FULL MEMBER OF THE TIER, not a container that merely resembles one.
        # It used to pass `(root, image, network="none")` and nothing else, so it inherited only the
        # flags `docker_run_argv` applies unconditionally (`--rm --network none --pids-limit 1024
        # --cap-drop ALL --security-opt no-new-privileges`) and none of the CALLER-supplied column:
        # measured on shipped defaults it ran with no `--memory 4g`, and under a `hostile`
        # trust mode with no `--runtime runsc` — a shared-kernel container on the tier chosen
        # BECAUSE a shared kernel was not enough. `sandbox_readonly_rootfs`/`sandbox_cpus` could not
        # reach it at all. `docker_tier_kwargs` is the ONE translation the eval tiers use, so this
        # cannot drift from them again; a `settings=None` construction resolves to the SHIPPED
        # defaults rather than to an unbounded container.
        if self.trust_mode and self.trust_mode != "trusted_local" and self._wrap is None:
            from looplab.runtime.command_eval import make_docker_wrap
            from looplab.runtime.sandbox import docker_tier_kwargs
            tier = docker_tier_kwargs(self._settings, trust_mode=self.trust_mode)
            if self.image:                       # explicit per-instance override, else Settings'
                tier["image"] = self.image
            self._wrap = make_docker_wrap(str(self._roots[0]), network="none", **tier)
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
        out_take, err_take = _stream_tails(out or "", err or "")
        if out and out.strip():
            parts.append("stdout:\n" + _tail(out, out_take))
        if err and err.strip():
            parts.append("stderr:\n" + _tail(err, err_take))
        return "\n".join(parts)
