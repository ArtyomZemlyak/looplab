"""Task-pinned command execution for the repo Developer.

This is deliberately not a general shell tool.  The operator records complete command specs in the
``RepoTask`` snapshot; the model can only choose one immutable name.  Every invocation gets a fresh
candidate workspace (source seed + current staged overlay + declared inputs), and that directory is
destroyed after output is captured.  Command-created files therefore never become node files; the
only durable authoring channel remains ``write_file``/``edit_file``.

The configured trust tier is preserved.  ``untrusted`` and ``hostile`` use the same hardened Docker
argv builder as command evaluation (network none, cgroups, optional read-only rootfs, gVisor for the
hostile tier). ``trusted_local`` is intentionally the project's documented process tier: suitable
for operator-authored commands on the operator's machine, not a multi-tenant security boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
from types import MappingProxyType
from typing import Any, Mapping, Optional

from looplab.core.envsafe import merge_env
from looplab.core.redact import redact_secrets
from looplab.engine import workspace_seed
from looplab.tools._base import (RESULT_CAP, CancelSignal, ToolCapability, ToolResult,
                                 clip, fn_spec, stream_tails)

_MAX_OUTPUT_BYTES = 64_000


@dataclass(frozen=True)
class DeveloperCommandRuntime:
    """Plain runtime values copied from Settings at the role-construction boundary."""

    trust_mode: str = "trusted_local"
    docker_image: str = "python:3.12-slim"
    sandbox_memory: str = "4g"
    sandbox_cpus: str = ""
    sandbox_readonly_rootfs: str = ""
    sandbox_memory_local: str = ""
    sandbox_fsize_local: str = ""
    seed_mode: str = "auto"
    redact_output: bool = True
    eval_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trust_mode not in {"trusted_local", "untrusted", "hostile"}:
            raise ValueError("developer command trust_mode must be trusted_local, untrusted or hostile")
        object.__setattr__(self, "eval_env", MappingProxyType(
            {str(k): str(v) for k, v in (self.eval_env or {}).items()}))

    @classmethod
    def from_settings(cls, settings) -> "DeveloperCommandRuntime":
        """Snapshot only the command runner's settings at the role-construction boundary."""
        return cls(
            trust_mode=getattr(settings, "trust_mode", "trusted_local"),
            docker_image=getattr(settings, "docker_image", "python:3.12-slim"),
            sandbox_memory=getattr(settings, "sandbox_memory", "4g"),
            sandbox_cpus=getattr(settings, "sandbox_cpus", ""),
            sandbox_readonly_rootfs=getattr(settings, "sandbox_readonly_rootfs", ""),
            sandbox_memory_local=getattr(settings, "sandbox_memory_local", ""),
            sandbox_fsize_local=getattr(settings, "sandbox_fsize_local", ""),
            seed_mode=getattr(settings, "seed_mode", "auto"),
            redact_output=getattr(settings, "redact_output", True),
            eval_env=getattr(settings, "eval_env", {}) or {})


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _overlay_digest(staged) -> str:
    h = hashlib.sha256()
    files = getattr(staged, "files", None) or {}
    for rel in sorted(files):
        h.update(b"F\0" + str(rel).encode("utf-8", errors="replace") + b"\0")
        h.update(str(files[rel] or "").encode("utf-8", errors="replace"))
        h.update(b"\0")
    for rel in sorted(getattr(staged, "deleted", None) or []):
        h.update(b"D\0" + str(rel).encode("utf-8", errors="replace") + b"\0")
    return h.hexdigest()


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


class DevCommandTools:
    """Run one exact operator-pinned command against the Developer's current candidate."""

    def __init__(self, repo_spec: Optional[dict], *, runtime: Optional[DeveloperCommandRuntime] = None,
                 staged=None):
        self.repo_spec = dict(repo_spec or {})
        self.runtime = runtime or DeveloperCommandRuntime()
        self.staged = staged
        rows = []
        for raw in self.repo_spec.get("developer_commands") or ():
            row = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
            rows.append(row)
        self._commands = {str(row.get("name") or ""): row for row in rows if row.get("name")}
        self.policy_digest = _canonical_digest(
            [self._commands[name] for name in sorted(self._commands)])
        self.runtime_digest = _canonical_digest({
            "trust_mode": self.runtime.trust_mode,
            "docker_image": self.runtime.docker_image,
            "sandbox_memory": self.runtime.sandbox_memory,
            "sandbox_cpus": self.runtime.sandbox_cpus,
            "sandbox_readonly_rootfs": self.runtime.sandbox_readonly_rootfs,
            "sandbox_memory_local": self.runtime.sandbox_memory_local,
            "sandbox_fsize_local": self.runtime.sandbox_fsize_local,
            "seed_mode": self.runtime.seed_mode,
            "redact_output": self.runtime.redact_output,
            "eval_env": dict(self.runtime.eval_env),
        })

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        if not self._commands:
            return []
        rendered = []
        for name, spec in self._commands.items():
            purpose = f" — {spec.get('description')}" if spec.get("description") else ""
            rendered.append(f"{name}: {json.dumps(spec.get('command') or [], ensure_ascii=False)}{purpose}")
        spec = fn_spec(
            "run_dev_command",
            "Run ONE exact command the OPERATOR pinned in this task. You select only its name; argv, "
            "cwd, env and timeout cannot be changed. It runs against a DISPOSABLE candidate copy "
            "containing the seeded repo plus your currently staged edits and declared data/reference "
            "inputs. Use it for compilation, focused tests, linters and data validators. Changes "
            "inside the candidate tree are discarded; mounted inputs retain their task/trust-tier "
            "policy. Persist a real fix only through write_file or edit_file. This is not an "
            "arbitrary shell. Available commands:\n" + "\n".join(rendered),
            {"name": {"type": "string", "enum": list(self._commands),
                      "description": "exact operator-pinned command name"}},
            ["name"])
        # This authority surface has exactly one model-authored value. Reject invented siblings at
        # schema-aware transports as well as ignoring them defensively at execution time.
        spec["function"]["parameters"]["additionalProperties"] = False
        return [spec]

    def capabilities(self) -> list[ToolCapability]:
        specs = self.specs()
        if not specs:
            return []
        schema = specs[0]["function"]["parameters"]
        return [ToolCapability(
            name="run_dev_command", effect="execute", risk="high", idempotency="conditional",
            concurrency_safe=False, cancellable=True, approval="task_pinned", input_schema=schema,
            output_schema={"type": "object", "properties": {
                "command": {"type": "string"}, "exit_code": {"type": "integer"},
                "timed_out": {"type": "boolean"}, "cancelled": {"type": "boolean"},
                "duration_ms": {"type": "integer"}, "stdout": {"type": "string"},
                "retention_scope": {"type": "string", "enum": ["candidate_tree"]},
                "stderr": {"type": "string"}, "trust_mode": {
                    "type": "string", "enum": ["trusted_local", "untrusted", "hostile"]}}},
            source="task.developer_commands")]

    def execute(self, name: str, args: dict) -> str:
        return self.execute_result(name, args).content

    def execute_result(self, name: str, args: dict, *, cancel_check=None) -> ToolResult:
        if name != "run_dev_command":
            return ToolResult(content=f"(unknown tool: {name})", is_error=True, retryable=False,
                              provenance={"source": "task.developer_commands"})
        command_name = str((args or {}).get("name") or "")
        spec = self._commands.get(command_name)
        if spec is None:
            allowed = ", ".join(self._commands) or "(none)"
            return ToolResult(
                content=f"(run_dev_command: unknown command {command_name!r}; allowed: {allowed})",
                structured={"command": command_name, "allowed": list(self._commands)},
                is_error=True, retryable=False,
                provenance={"source": "task.developer_commands"},
                receipt={"policy_sha256": self.policy_digest,
                         "runtime_policy_sha256": self.runtime_digest})
        signal = CancelSignal(cancel_check)
        if signal.is_set():
            return ToolResult(content="(cancelled by the user: command not executed)",
                              structured={"command": command_name, "cancelled": True},
                              is_error=True, retryable=True,
                              provenance={"source": "task.developer_commands"},
                              receipt={"policy_sha256": self.policy_digest,
                                       "runtime_policy_sha256": self.runtime_digest})
        try:
            return self._execute(command_name, spec, signal)
        except Exception as exc:  # noqa: BLE001 - tool failures are observations, not loop failures
            return ToolResult(
                content=f"(developer command error: {type(exc).__name__}: {exc})",
                structured={"command": command_name, "launch_error": type(exc).__name__},
                is_error=True, retryable=False,
                provenance={"source": "task.developer_commands", "command": command_name},
                receipt={"policy_sha256": self.policy_digest,
                         "runtime_policy_sha256": self.runtime_digest,
                         "command_sha256": _canonical_digest(spec),
                         "overlay_sha256": _overlay_digest(self.staged)})

    def _execute(self, command_name: str, spec: dict, signal: CancelSignal) -> ToolResult:
        root = Path(tempfile.mkdtemp(prefix="looplab-dev-command-"))
        started = time.monotonic()
        raw_out = raw_err = ""
        try:
            work = root / "work"
            notes, binds = self._materialize(work)
            cwd = (work / str(spec.get("cwd") or ".")).resolve()
            if not _inside(work, cwd) or not cwd.is_dir():
                raise ValueError(f"declared cwd {spec.get('cwd')!r} is not a directory in the candidate")
            argv = list(spec.get("command") or ())
            timeout = float(spec.get("timeout") or 120.0)
            env = merge_env(self.runtime.eval_env, self.repo_spec.get("eval_env"), spec.get("env"),
                            {"CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": "void",
                             "PYTHONDONTWRITEBYTECODE": "1"})
            launch = argv
            mem_bytes = fsize_bytes = None
            if self.runtime.trust_mode in {"untrusted", "hostile"}:
                from looplab.runtime.command_eval import make_docker_wrap
                from looplab.runtime.sandbox import docker_tier_kwargs
                wrap = make_docker_wrap(str(work),
                                        **docker_tier_kwargs(
                                            self.runtime, trust_mode=self.runtime.trust_mode),
                                        binds=binds or None, env=env)
                launch = wrap(argv, str(cwd))
            else:
                from looplab.runtime.sandbox import parse_mem_bytes
                mem_bytes = parse_mem_bytes(self.runtime.sandbox_memory_local)
                fsize_bytes = parse_mem_bytes(self.runtime.sandbox_fsize_local)
            from looplab.runtime.sandbox import run_argv
            rc, raw_out, raw_err, timed_out = run_argv(
                launch, str(cwd), timeout, env=env, max_output_bytes=_MAX_OUTPUT_BYTES,
                cancel=signal, mem_bytes=mem_bytes, fsize_bytes=fsize_bytes)
            # ``run_argv`` historically reports a user cancel through its timeout boolean because
            # both paths tree-kill. The live signal disambiguates them at this typed boundary.
            cancelled = signal.is_set()
            timed_out = bool(timed_out and not cancelled)
        finally:
            shutil.rmtree(root, ignore_errors=True)

        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        # Commands are allowed to inspect real data, so their output gets the durable/prompt secret
        # screen even though the child environment itself was already scrubbed by run_argv.
        out = redact_secrets(raw_out or "", entropy=self.runtime.redact_output)
        err = redact_secrets(raw_err or "", entropy=self.runtime.redact_output)
        content = self._project(command_name, spec, rc, out, err, timed_out, cancelled,
                                duration_ms, notes)
        structured = {
            "command": command_name, "argv": list(spec.get("command") or ()),
            "cwd": str(spec.get("cwd") or "."), "exit_code": int(rc),
            "timed_out": bool(timed_out), "cancelled": bool(cancelled),
            "duration_ms": duration_ms, "stdout": out, "stderr": err,
            "workspace": "disposable", "changes_retained": False,
            "retention_scope": "candidate_tree",
            "trust_mode": self.runtime.trust_mode,
        }
        receipt = {
            "policy_sha256": self.policy_digest,
            "runtime_policy_sha256": self.runtime_digest,
            "command_sha256": _canonical_digest(spec),
            "overlay_sha256": _overlay_digest(self.staged),
            "stdout_sha256": hashlib.sha256((raw_out or "").encode(
                "utf-8", errors="replace")).hexdigest(),
            "stderr_sha256": hashlib.sha256((raw_err or "").encode(
                "utf-8", errors="replace")).hexdigest(),
        }
        failed = bool(rc != 0 or timed_out or cancelled)
        return ToolResult(content=content, structured=structured, is_error=failed,
                          retryable=True if (timed_out or cancelled) else False,
                          provenance={"source": "task.developer_commands",
                                      "command": command_name}, receipt=receipt)

    def _materialize(self, work: Path) -> tuple[list[str], list[tuple[str, bool]]]:
        work.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules")
        notes: list[str] = []
        editables = self.repo_spec.get("editables") or []
        mounts = [r.get("name") for r in self.repo_spec.get("references") or [] if r.get("mount")]
        mounts += list((self.repo_spec.get("data") or {}).keys())
        for editable in editables:
            target = work if editable.get("name") in {"", "."} else work / editable["name"]
            mode = editable.get("seed_mode") or self.runtime.seed_mode or "auto"
            count = workspace_seed.seed_repo_tree(editable["path"], target, ignore, mode)
            notes.append(f"{editable.get('name') or '.'}:{'all' if count < 0 else count}")
        root_editable = next((e for e in editables if e.get("name") in {"", "."}), None)
        collision = next((name for name in mounts if name and (work / name).exists()), None)
        if root_editable and collision:
            raise ValueError(f"mount name {collision!r} collides with the seeded root repo")
        for editable in editables:
            target = work if editable.get("name") in {"", "."} else work / editable["name"]
            workspace_seed.seed_protected_files(
                editable["path"], target, editable.get("protect"),
                reserved_top=(set(mounts) if editable.get("name") in {"", "."} else set()))

        binds: list[tuple[str, bool]] = []
        for ref in self.repo_spec.get("references") or []:
            if ref.get("mount"):
                workspace_seed.link_input(ref["path"], work / ref["name"])
                if (work / ref["name"]).is_symlink():
                    binds.append((ref["path"], True))
        for name, raw in (self.repo_spec.get("data") or {}).items():
            spec = raw if isinstance(raw, dict) else {"path": raw, "mount": True, "edit": False}
            if spec.get("mount", True):
                workspace_seed.link_input(spec["path"], work / name)
                if (work / name).is_symlink():
                    binds.append((spec["path"], not spec.get("edit", False)))
            else:
                workspace_seed.copy_input(spec["path"], work / name, ignore)
        self._apply_overlay(work)
        return notes, binds

    def _apply_overlay(self, work: Path) -> None:
        for rel in sorted(getattr(self.staged, "deleted", None) or []):
            target = work / str(rel)
            if not _inside(work, target):
                raise ValueError(f"staged deletion escapes candidate workspace: {rel!r}")
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)
        for rel, body in sorted((getattr(self.staged, "files", None) or {}).items()):
            target = work / str(rel)
            if not _inside(work, target):
                raise ValueError(f"staged file escapes candidate workspace: {rel!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after mkdir: an existing parent symlink could otherwise redirect the write.
            if not _inside(work, target):
                raise ValueError(f"staged file resolves outside candidate workspace: {rel!r}")
            target.write_text(str(body or ""), encoding="utf-8")

    @staticmethod
    def _project(name, spec, rc, out, err, timed_out, cancelled, duration_ms, notes) -> str:
        state = f"exit={rc}; duration={duration_ms}ms"
        if timed_out:
            state += f"; TIMEOUT after {float(spec.get('timeout') or 0):g}s"
        if cancelled:
            state += "; CANCELLED"
        head = (f"command={name}; argv={json.dumps(spec.get('command') or [], ensure_ascii=False)}\n"
                f"{state}\nworkspace=disposable (candidate-tree changes discarded; mounted inputs "
                f"follow task/trust policy); seed={','.join(notes)}")
        out_take, err_take = stream_tails(out or "", err or "")
        parts = [head]
        if (out or "").strip():
            parts.append("stdout:\n" + clip(out, out_take, keep="tail", note="…(truncated)…\n"))
        if (err or "").strip():
            parts.append("stderr:\n" + clip(err, err_take, keep="tail", note="…(truncated)…\n"))
        if not (out or "").strip() and not (err or "").strip():
            parts.append("(no output)")
        return clip("\n".join(parts), RESULT_CAP, keep="head", reserve=96,
                    note="\n(developer command result clipped)")
