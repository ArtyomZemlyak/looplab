"""Pure, fail-closed validation for new-run launches.

The browser, TUI, and direct HTTP callers all launch through ``POST /api/start``.  This module keeps
the expensive/read-only part of that boundary in one place so a launch preview and the real launch
cannot disagree.  It deliberately does not create directories, write files, acquire spawn leases,
construct an LLM client, or start a process.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from looplab.adapters import tasks as task_adapters
from looplab.core.atomicio import file_identity
from looplab.core.pathsafe import WINDOWS_RESERVED
from looplab.core.appconfig import load_document, parse_document_text, split_document
from looplab.core.comparison import canonical_comparison_contract
from looplab.core.config import (Settings, canonicalize_parallelism_source,
                                 flatten_parallelism_layers)
from looplab.core.run_deletion import RunDeletionStorageError, load_run_deletion_fence
from looplab.serve.appstate import (
    _DELETE_SERVICE_PREFIXES, _LIFECYCLE_LOCK_PREFIX, _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX,
    _TRACE_CLEAR_RECEIPT_PREFIX)
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


_START_FIELDS = {
    "run_id", "task", "task_file", "settings", "chat", "validation_token", "idempotency_key",
}


def _detail(code: str, message: str, field_errors: Optional[dict[str, str]] = None, **extra) -> dict:
    detail: dict[str, Any] = {"code": code, "message": message, "field_errors": field_errors or {}}
    detail.update(extra)
    return detail


def _reject(status: int, code: str, message: str, field: str | None = None, **extra):
    errors = {field: message} if field else {}
    raise HTTPException(status, _detail(code, message, errors, **extra))


def _lenient_json_bytes(value: Any) -> bytes:
    """Stable bytes for a launch-payload hash — deliberately NOT `core.jsonutil.canonical_json`.

    A launch payload is caller-shaped and may legitimately carry values with no JSON form (a Path, an
    enum, a datetime), so this one coerces with `default=str` and never raises: the hash is an
    identity for deduplicating launches, not a receipt preimage. It also omits `allow_nan=False`,
    which the strict helper treats as decisive. Sharing one NAME with the strict version is what made
    that difference invisible (doc 25 SE-08).
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_lenient_json_bytes(value)).hexdigest()


def safe_run_dir(root: Path, run_id: Any, *, check_conflict: bool = True) -> Path:
    if not isinstance(run_id, str) or not run_id:
        _reject(400, "invalid_run_id", "run_id is required", "run_id")
    if (len(run_id) > 255 or run_id != run_id.strip() or run_id.endswith((".", " "))
            or ":" in run_id or any(ord(ch) < 32 for ch in run_id)
            or run_id.split(".", 1)[0].upper() in WINDOWS_RESERVED):
        _reject(400, "invalid_run_id", "run_id is unsafe or filesystem-ambiguous", "run_id")
    requested = root / run_id
    try:
        resolved = requested.resolve()
    except OSError as exc:
        _reject(400, "invalid_run_id", f"run_id cannot be resolved: {exc}", "run_id")
    if resolved == root or resolved.parent != root:
        _reject(400, "invalid_run_id", "run_id must be a plain name, not a path", "run_id")
    if (resolved.name.lower() in _RESERVED_RUN_IDS
            or resolved.name.lower().startswith((
                _LIFECYCLE_LOCK_PREFIX, _TRACE_CLEAR_RECEIPT_PREFIX,
                _RESET_RECEIPT_PREFIX, *_DELETE_SERVICE_PREFIXES))):
        # Digest/operation-suffixed lifecycle and trace-clear files reserve their whole prefixes.
        _reject(400, "reserved_run_id", f"run_id {resolved.name!r} is reserved", "run_id")
    if requested.is_symlink():
        _reject(409, "run_path_conflict", "run path is a symbolic link", "run_id")
    try:
        deletion_fence = load_run_deletion_fence(requested)
    except RunDeletionStorageError as exc:
        raise HTTPException(503, {
            "code": "run_deletion_fence_unavailable",
            "message": "Deletion ownership cannot be verified for this run name.",
            "field_errors": {"run_id": "repair deletion recovery storage before retrying"},
        }) from exc
    if deletion_fence is not None:
        raise HTTPException(409, {
            "code": "run_deletion_in_progress",
            "operation_id": deletion_fence["operation_id"],
            "message": "This run name is still owned by an unresolved deletion.",
            "field_errors": {"run_id": "retry or observe that exact deletion first"},
        })
    # A run is a DIRECTORY. Reject a file conflict separately so a run_id naming any server-owned
    # file in the root — the settings/secrets/projects stores and their `.lock` siblings today,
    # whatever is added later — fails with a path-specific error. Name-based reservation above still
    # carries the case where the file does not exist yet.
    if resolved.exists() and not resolved.is_dir():
        _reject(409, "run_path_conflict", "run path is an existing file", "run_id")
    # Partial/materialized starts and externally created empty directories own the namespace too.
    # New starts must never infer availability merely from a missing events.jsonl and overwrite them.
    if check_conflict and resolved.exists():
        _reject(409, "run_id_conflict", f"run {run_id!r} already exists", "run_id")
    return resolved


def validate_idempotency_key(value: Any) -> str | None:
    """Return a safe optional launch key; the raw value is never persisted."""
    if value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > 200
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        _reject(400, "invalid_idempotency_key",
                "idempotency_key must be a nonempty string of at most 200 printable characters",
                "idempotency_key")
    return value


def idempotency_key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def launch_request_digest(body: dict) -> str:
    """Stable identity of the requested effects, independent of retry/validation credentials.

    This intentionally describes the raw task-file *reference*, not its current contents. An exact
    retry after Popen/HTTP-response loss must reattach to the durable result even when that source or
    the saved defaults changed after the accepted launch.
    """
    effect = {key: value for key, value in body.items()
              if key not in {"idempotency_key", "validation_token"}}
    return _sha({"version": 1, "request": effect})


_MAX_CHAT_TURNS = 500
_MAX_CHAT_CONTENT = 20_000


def _clean_chat(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        return ()
    # Bound both turn count and per-turn content: this seed chat is folded into the SHA validation
    # token and written verbatim to chat.jsonl, so an unbounded body is a memory/disk amplification
    # (control text is capped the same way).
    return tuple(
        {"role": str(turn["role"]), "content": str(turn.get("content", ""))[:_MAX_CHAT_CONTENT]}
        for turn in value[:_MAX_CHAT_TURNS]
        if isinstance(turn, dict) and turn.get("role") in {"user", "assistant"}
    )


def _validate_settings_keys(settings: Any, source: str) -> dict:
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        _reject(400, "invalid_launch_settings", f"{source} settings must be a JSON object", "settings")
    unknown = sorted(str(k) for k in settings if k not in _ALLOWED_FIELDS)
    secret = sorted(str(k) for k in settings if k in _SECRET_FIELDS)
    if unknown or secret:
        errors: dict[str, str] = {}
        for key in unknown:
            errors[f"settings.{key}"] = "unknown setting"
        for key in secret:
            errors[f"settings.{key}"] = "secrets must be configured through the secret store/environment"
        which = []
        if unknown:
            which.append("unknown: " + ", ".join(unknown))
        if secret:
            which.append("secret: " + ", ".join(secret))
        raise HTTPException(422, _detail(
            "invalid_launch_settings", f"invalid {source} settings ({'; '.join(which)})", errors))
    # ``null`` means "no explicit override", matching the launch form's optional fields.  A secret or
    # unknown key is rejected above even when null, so it can never be smuggled through this filter.
    return {k: v for k, v in settings.items() if v is not None}


def _validation_errors(exc: ValidationError, prefix: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    for row in exc.errors():
        loc = ".".join(str(part) for part in row.get("loc") or ())
        errors[f"{prefix}.{loc}".rstrip(".")] = str(row.get("msg") or "invalid value")
    return errors


def _path_stat(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "mode": stat.st_mode,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def task_file_roots(root: Path) -> list[Path]:
    """Where a launch `task_file` may live — the ONE derivation, shared with the task CATALOGUE.

    `routers/misc.py::list_tasks` offers the operator a pick-list built from exactly these three
    directories, and this is the same list because **the catalogue IS the declaration of what is
    launchable**. Two hand-synced copies would drift in the only two directions that matter: the UI
    offering a file the launcher then refuses, or — the direction that costs something — the launcher
    accepting a path the operator never declared runnable.

    Resolved here so the containment compare below is against real directories: an allowed root that
    is itself a symlink must be compared by its target, or containment answers about a name.
    """
    repo = Path(__file__).resolve().parents[2]        # looplab/serve/launch.py -> repo root
    dirs = [repo / "examples", root]
    env_dir = os.environ.get("LOOPLAB_TASKS_DIR")
    if env_dir:
        dirs.insert(0, Path(env_dir))
    out: list[Path] = []
    for d in dirs:
        try:
            resolved = d.resolve()
        except OSError:                               # an unreachable root is not an open one
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def _confine_task_file(root: Path, expanded: str) -> Path:
    """Resolve a requested `task_file` and REFUSE it unless it sits under a declared root.

    Backlog C3's surviving half. `POST /api/start` took an arbitrary absolute path from the request
    body: `os.path.expandvars(os.path.expanduser(...))` then `Path(...).resolve()`, then straight to
    `load_document`. Nothing compared it to `srv.root` or to anything else. That is a privilege bug
    the UI token does NOT fix — an authenticated operator's own browser session, or any same-origin
    page on a shared JupyterHub, could name `/etc/…`, `~/.ssh/…` or `/proc/self/environ`, and the
    `400 invalid_task_file` message embeds the parser's error, which is an oracle for a file's
    existence, size and partial CONTENT.

    Two orderings are load-bearing:

    * **resolve, THEN contain.** `Path.resolve()` follows every symlink, so a symlink planted inside
      an allowed root that points at `/etc/shadow` resolves outside it and is refused here. Checking
      containment on the unresolved path and then resolving would admit exactly that. This is why
      there is no separate `is_symlink()` rung the way `safe_run_dir` needs one — that function must
      keep a name it is about to CREATE, this one only ever reads.
    * **contain BEFORE `exists()`.** `expandvars` runs on caller text, so `task_file: "$OPENAI_API_KEY"`
      expands to the secret's VALUE and the pre-existing `task_file_not_found` message echoes the
      resolved path back to the caller verbatim. Refusing on containment first means every message
      that echoes a path has already proved the path is under a declared root.

    The refusal names the ALLOWED ROOTS and never the rejected path, for the same reason.
    """
    try:
        source_path = Path(expanded).resolve()
    except OSError as exc:
        _reject(400, "invalid_task_file", f"task_file cannot be resolved: {exc}", "task_file")
    roots = task_file_roots(root)
    for allowed in roots:
        if source_path == allowed or allowed in source_path.parents:
            return source_path
    _reject(400, "task_file_not_allowed",
            "task_file must live under a declared task directory ("
            + ", ".join(str(p) for p in roots)
            + "); set LOOPLAB_TASKS_DIR to declare another",
            "task_file")


# Task specs are small (a toy dict, a YAML/JSON config). Cap the ONE whole-file read below so an
# unbounded pseudo-file or a multi-GB regular file cannot hang the preflight worker or exhaust
# memory. Every other launch input is bounded.
_MAX_TASK_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB
_TASK_FILE_READ_CHUNK = 1 << 20


@dataclass(frozen=True)
class ConfinedTaskFile:
    """One task-file read: the bytes, and the identity of the file they actually came from."""
    path: Path
    data: bytes
    stat_row: dict

    def fingerprint(self) -> dict:
        return {**self.stat_row, "sha256": hashlib.sha256(self.data).hexdigest()}

    def document(self) -> tuple[dict, dict, str | None]:
        """Parse THESE bytes with the shared loader — never by re-opening the name."""
        return split_document(parse_document_text(
            self.data.decode("utf-8-sig"),      # utf-8-sig tolerates a BOM, as `load_document` does
            suffix=self.path.suffix.lower(), label=str(self.path)))


def _read_bounded(fd: int) -> bytes:
    """Read one descriptor to EOF, stopping one byte past the cap so growth is DETECTED, not silently
    truncated into a parse of a prefix. Its own function because it is the window the identity CAS in
    `read_confined_task_file` covers — a test that has to replace the file mid-read patches HERE, and
    a race nobody can drive is a guard nobody has checked."""
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_TASK_FILE_BYTES:
        chunk = os.read(fd, _TASK_FILE_READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def read_confined_task_file(root: Path, expanded: str) -> ConfinedTaskFile:
    """Contain a requested `task_file`, then read it ONCE through a fenced descriptor.

    `_confine_task_file` answers about a NAME, and a name is not what gets parsed. Before this, three
    separate opens of that name followed the one check — `_require_task_file_size`'s `stat`,
    `load_document`'s `read_text`, and `_source_fingerprint`'s `read_bytes` — so on a box where
    anything else can write inside a declared root (the run root IS one, and a node's own eval writes
    under it) a file could pass containment as a plain task JSON and then be replaced by a symlink to
    `/etc/shadow` before `load_document` opened it. The parser's error is embedded in the
    `400 invalid_task_file` message, so that race is the same content oracle containment closes.
    The three reads could also disagree with each other: the launch token's fingerprint described a
    different set of bytes than the ones that were parsed into the run's task.

    Three rungs, in this order:

    * **`O_NOFOLLOW` on the RESOLVED path.** `Path.resolve()` has already canonicalized every
      symlink, so the resolved path contains none by construction and this flag can refuse no
      legitimate launch — an operator's `runs/current-task.json -> examples/foo.json` resolves to the
      target and is admitted exactly as before. The ONLY thing it can catch is a final component that
      became a symlink *after* the resolve, which is precisely the swap.
    * **`fstat` + `S_ISREG`, and `O_NONBLOCK` on the open.** A FIFO planted in a declared root made
      `read_text` block the preflight worker forever; opening non-blocking and refusing a
      non-regular file closes both the hang and the device/`/proc`-style pseudo-file read. It keeps
      the existing `task_file_not_found` code, which `Path.is_file()` already gave these.
    * **an identity CAS across the read.** The `fstat` taken before the read is compared against an
      `lstat` of the same path after it, through `core/atomicio.file_identity` — the canonical
      "same file AND unchanged" tuple, used WHOLE and not as a subset because that is exactly the
      question here: the bytes handed to the parser must be the bytes of the file that passed
      containment. A replacement (new inode), a same-size rewrite, or a growth mid-read all refuse.

    What stays open, said plainly: a DIRECTORY component of the resolved path could still be swapped
    between the resolve and the open, which no `O_NOFOLLOW` can see — closing that needs an
    `openat` walk holding a descriptor per component. It requires write access inside a declared
    root, which on the supported deployment is the operator themselves.
    """
    path = _confine_task_file(root, expanded)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # ELOOP is the swap (or a symlink that appeared under the resolved name); ENXIO is a FIFO
        # with no writer. Both are "this is not the readable regular file you contained".
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ELOOP, errno.ENXIO):
            _reject(400, "task_file_not_found", f"task_file not found: {path}", "task_file")
        _reject(400, "invalid_task_file", f"task_file cannot be read: {exc}", "task_file")
    try:
        try:
            before = os.fstat(fd)
        except OSError as exc:
            _reject(400, "invalid_task_file", f"task_file cannot be read: {exc}", "task_file")
        if not stat.S_ISREG(before.st_mode):
            _reject(400, "task_file_not_found", f"task_file not found: {path}", "task_file")
        if before.st_size > _MAX_TASK_FILE_BYTES:
            _reject(400, "task_file_too_large",
                    f"task_file exceeds the {_MAX_TASK_FILE_BYTES}-byte limit", "task_file")
        try:
            data = _read_bounded(fd)
            after = os.lstat(path)
        except OSError as exc:
            _reject(422, "task_source_changed", f"task_file cannot be read: {exc}", "task_file")
        if len(data) > _MAX_TASK_FILE_BYTES:
            _reject(400, "task_file_too_large",
                    f"task_file exceeds the {_MAX_TASK_FILE_BYTES}-byte limit", "task_file")
        if file_identity(after) != file_identity(before):
            _reject(422, "task_source_changed",
                    "task_file changed while it was being read", "task_file")
    finally:
        os.close(fd)
    return ConfinedTaskFile(path=path, data=data, stat_row={
        "path": str(path),
        "mode": before.st_mode,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
    })


def _task_paths(task: dict) -> list[tuple[str, Path, bool]]:
    """Return ``(field, resolved path, must_be_directory)`` for launch-critical task inputs."""
    rows: list[tuple[str, Path, bool]] = []

    def add(field: str, raw: Any, directory: bool = False) -> None:
        if not isinstance(raw, str) or not raw:
            return
        expanded = os.path.expandvars(os.path.expanduser(raw))
        rows.append((field, Path(expanded).resolve(), directory))

    kind = task.get("kind")
    if kind == "repo":
        add("task.editable_path", task.get("editable_path"), True)
        for index, editable in enumerate(task.get("editables") or []):
            if isinstance(editable, dict):
                add(f"task.editables.{index}.path", editable.get("path"), True)
        for index, reference in enumerate(task.get("references") or []):
            if isinstance(reference, dict):
                add(f"task.references.{index}.path", reference.get("path"))
        for name, spec in (task.get("data") or {}).items():
            add(f"task.data.{name}.path", spec.get("path") if isinstance(spec, dict) else spec)
    elif kind == "dataset":
        add("task.data_path", task.get("data_path"))
        for name, raw in (task.get("data") or {}).items():
            add(f"task.data.{name}", raw)
    return rows


def _validated_path_fingerprints(task: dict) -> list[dict]:
    fingerprints: list[dict] = []
    errors: dict[str, str] = {}
    for field, path, directory in _task_paths(task):
        try:
            if not path.exists():
                errors[field] = f"path does not exist: {path}"
                continue
            if directory and not path.is_dir():
                errors[field] = f"expected a directory: {path}"
                continue
            fingerprints.append({"field": field, **_path_stat(path)})
        except OSError as exc:
            errors[field] = f"path cannot be inspected: {exc}"
    if errors:
        raise HTTPException(422, _detail(
            "invalid_task_paths", "one or more task paths are unavailable", errors))
    return sorted(fingerprints, key=lambda row: (row["field"], row["path"]))


def _base_settings_fingerprint() -> tuple[Settings, str]:
    try:
        base = Settings()
    except ValidationError as exc:
        raise HTTPException(422, _detail(
            "invalid_base_settings", "environment/default settings are invalid",
            _validation_errors(exc, "settings"))) from exc
    except Exception as exc:  # noqa: BLE001 - every settings source must fail before Popen
        _reject(422, "invalid_base_settings", f"environment/default settings are invalid: {exc}",
                "settings")
    dump = base.model_dump(mode="json")
    dump.pop("llm_api_key", None)
    dump.pop("llm_api_key_base_url", None)
    return base, _sha(dump)


def _resolved_settings(task: dict, saved: dict, file_settings: dict,
                       launch_settings: dict) -> tuple[dict, bool, str]:
    base, base_digest = _base_settings_fingerprint()
    # Saved UI settings < task-file settings < explicit launch settings. Canonicalize aliases in
    # each layer before flattening so a spelling change cannot invert source precedence.
    merged = flatten_parallelism_layers([
        canonicalize_parallelism_source(saved),
        canonicalize_parallelism_source(file_settings),
        canonicalize_parallelism_source(launch_settings),
    ])
    inferred = False
    # An env/.env backend is an explicit operator choice too.  Only infer when every higher-precedence
    # source and the base Settings source left it unset.
    if "backend" not in merged and "backend" not in getattr(base, "model_fields_set", set()):
        from looplab.engine.genesis import default_backend
        if default_backend(task.get("kind"), chosen=False) == "llm":
            merged["backend"] = "llm"
            inferred = True
    try:
        resolved = Settings(**merged)
    except ValidationError as exc:
        raise HTTPException(422, _detail(
            "invalid_launch_settings", "launch settings are invalid",
            _validation_errors(exc, "settings"))) from exc
    except Exception as exc:  # noqa: BLE001 - fail before materialization/spawn on any coercion error
        _reject(422, "invalid_launch_settings", f"launch settings are invalid: {exc}", "settings")
    dump = resolved.model_dump(mode="json")
    dump.pop("llm_api_key", None)
    dump.pop("llm_api_key_base_url", None)
    return dump, inferred, base_digest


@dataclass(frozen=True)
class LaunchPreflight:
    run_id: str
    run_dir: Path
    canonical_task: dict
    effective_settings: dict
    source_task_file: str | None
    source_fingerprint: dict | None
    referenced_paths: tuple[dict, ...]
    saved_settings_digest: str
    base_settings_digest: str
    seed_chat: tuple[dict[str, str], ...]
    validation_token: str
    warnings: tuple[str, ...]

    @property
    def canonical_document(self) -> dict:
        return {"task": self.canonical_task, "settings": self.effective_settings}

    def preview(self) -> dict:
        return {
            "run_id": self.run_id,
            "source": "task_file" if self.source_task_file else "inline",
            "source_task_file": self.source_task_file,
            "task": self.canonical_task,
            "settings": self.effective_settings,
            "referenced_paths": list(self.referenced_paths),
        }

    def current_token(self, srv) -> str:
        """Cheap pre-Popen drift check; it performs only local reads/stats/settings construction.

        Deliberately a re-read BY NAME: the whole point is to notice that the source changed between
        the preflight the operator confirmed and the spawn. It goes through the same fenced reader,
        so the re-read is contained and identity-checked too — a source that has become a symlink
        out of a declared root since the preflight is refused rather than hashed.
        """
        safe_run_dir(srv.root, self.run_id, check_conflict=True)
        source = (read_confined_task_file(srv.root, self.source_task_file).fingerprint()
                  if self.source_task_file else None)
        # Re-stat the exact validated fields. Missing/replaced paths fail closed with field detail.
        paths = _validated_path_fingerprints(self.canonical_task)
        saved = srv.settings.load_ui_settings()
        _base, base_digest = _base_settings_fingerprint()
        return _launch_token(
            self.run_id, self.canonical_task, self.effective_settings, source, paths,
            _sha(saved), base_digest, self.seed_chat)


def _launch_token(run_id: str, task: dict, settings: dict, source: dict | None,
                  paths: list[dict] | tuple[dict, ...], saved_digest: str,
                  base_digest: str, seed_chat: tuple[dict[str, str], ...]) -> str:
    return _sha({
        "version": 2,
        "run_id": run_id,
        "task": task,
        "settings": settings,
        "source_task_file": source,
        "referenced_paths": list(paths),
        "saved_settings_digest": saved_digest,
        "base_settings_digest": base_digest,
        "seed_chat": list(seed_chat),
    })


def preflight_start(srv, body: Any) -> LaunchPreflight:
    """Validate and resolve one launch request without any mutation or provider/model operation."""
    if not isinstance(body, dict):
        _reject(400, "invalid_launch_request", "start body must be a JSON object")
    unknown_top = sorted(str(key) for key in body if key not in _START_FIELDS)
    if unknown_top:
        errors = {key: "unknown launch field" for key in unknown_top}
        raise HTTPException(400, _detail(
            "invalid_launch_request", "unknown launch field(s): " + ", ".join(unknown_top), errors))

    run_id = body.get("run_id")
    run_dir = safe_run_dir(srv.root, run_id, check_conflict=True)

    task_value = body.get("task")
    task_file_value = body.get("task_file")
    if task_value is not None and not isinstance(task_value, dict):
        _reject(400, "invalid_task_source", "task must be a JSON object", "task")
    if task_file_value is not None and not isinstance(task_file_value, str):
        _reject(400, "invalid_task_source", "task_file must be a string path", "task_file")
    has_inline = isinstance(task_value, dict) and bool(task_value)
    has_file = isinstance(task_file_value, str) and bool(task_file_value.strip())
    if has_inline == has_file:
        _reject(400, "invalid_task_source",
                "give exactly one nonempty task or task_file", "task")

    if body.get("chat") is not None and not isinstance(body.get("chat"), list):
        _reject(400, "invalid_launch_chat", "chat must be an array", "chat")
    if body.get("validation_token") is not None and not isinstance(body.get("validation_token"), str):
        _reject(400, "invalid_validation_token", "validation_token must be a string", "validation_token")
    validate_idempotency_key(body.get("idempotency_key"))
    seed_chat = _clean_chat(body.get("chat"))

    source_path: Path | None = None
    source_fp: dict | None = None
    file_settings: dict = {}
    if has_file:
        expanded = os.path.expandvars(os.path.expanduser(task_file_value.strip()))
        # C3: contain BEFORE any probe that echoes the path — see `_confine_task_file` — and read
        # the contained file exactly ONCE, so the bytes parsed below and the bytes fingerprinted
        # into the launch token are the same bytes (`read_confined_task_file`).
        source = read_confined_task_file(srv.root, expanded)
        source_path = source.path
        try:
            raw_task, raw_file_settings, _out = source.document()
        except (OSError, ValueError, TypeError) as exc:
            _reject(400, "invalid_task_file", f"could not load task_file: {exc}", "task_file")
        task_input = raw_task
        file_settings = _validate_settings_keys(raw_file_settings, "task-file")
        source_fp = source.fingerprint()
    else:
        task_input = task_value

    try:
        adapter = task_adapters.validate_task(task_input)
    except ValidationError as exc:
        raise HTTPException(422, _detail(
            "invalid_task", "task is invalid", _validation_errors(exc, "task"))) from exc
    except Exception as exc:  # noqa: BLE001 - adapter registries may raise ValueError/KeyError/runtime errors
        _reject(422, "invalid_task", f"task is invalid: {exc}", "task")
    # Some embedders/tests inject a validator returning the canonical dict directly; the production
    # registry returns a Pydantic TaskAdapter. Supporting both keeps the helper dependency-injectable.
    # this typed adapter dump is the one task document shared by browser, TUI, and
    # direct API launches.  In particular it preserves the validated comparison_contract and its
    # canonical contract_id into task.input.json -> task.snapshot.json; never merge the raw body back.
    canonical_task = (dict(adapter) if isinstance(adapter, dict)
                      else adapter.model_dump(mode="json"))
    raw_contract = (canonical_task.get("comparison_contract") if isinstance(adapter, dict)
                    else getattr(adapter, "comparison_contract", None))
    canonical_contract = canonical_comparison_contract(raw_contract)
    if raw_contract is not None and canonical_contract is None:
        _reject(422, "invalid_task", "task comparison_contract is invalid",
                "task.comparison_contract")
    if canonical_contract is None:
        canonical_task.pop("comparison_contract", None)
    else:
        canonical_task["comparison_contract"] = canonical_contract
    referenced_paths = _validated_path_fingerprints(canonical_task)

    launch_settings = _validate_settings_keys(body.get("settings") or {}, "launch")
    saved_settings = srv.settings.load_ui_settings()
    # load_ui_settings already strips unknown/secrets; validate values together with higher sources.
    effective, inferred, base_digest = _resolved_settings(
        canonical_task, saved_settings, file_settings, launch_settings)
    warnings = ("backend=llm was inferred for this generative task",) if inferred else ()
    # The same submit-time warning the CLI prints: an eval `command` that names no in-repo file
    # leaves the score stage's CODE inside the Developer's edit surface, while the Developer's
    # prompt tells it the scoring cannot be rewritten. `adapter` may be an injected dict in tests —
    # the helper is total over that (it isinstance-checks a RepoTask and returns [] otherwise).
    from looplab.adapters.repo_task import (eval_entrypoint_unprotected,
                                            eval_source_tree_command_paths)
    warnings += tuple(eval_entrypoint_unprotected(adapter))
    # docs/29 F1c's third piece: an argv token naming the editable SOURCE tree absolutely reaches
    # the operator's original rather than the node's copy, so no node's edits to it ever take
    # effect. Same channel, same totality over an injected dict `adapter`.
    warnings += tuple(eval_source_tree_command_paths(adapter))
    token = _launch_token(
        run_id, canonical_task, effective, source_fp, referenced_paths,
        _sha(saved_settings), base_digest, seed_chat)
    return LaunchPreflight(
        run_id=run_id,
        run_dir=run_dir,
        canonical_task=canonical_task,
        effective_settings=effective,
        source_task_file=str(source_path) if source_path else None,
        source_fingerprint=source_fp,
        referenced_paths=tuple(referenced_paths),
        saved_settings_digest=_sha(saved_settings),
        base_settings_digest=base_digest,
        seed_chat=seed_chat,
        validation_token=token,
        warnings=warnings,
    )


def preflight_response(plan: LaunchPreflight) -> dict:
    return {
        "ok": True,
        "validation_token": plan.validation_token,
        "preview": plan.preview(),
        "warnings": list(plan.warnings),
    }

# Moved here from `routers/control.py` (doc 25 SR-12): launch policy with no HTTP dependency, which
# `routers/genesis.py` was importing out of a SIBLING ROUTER's privates — route modules stopped being
# independent leaves. Its home is now the module that owns the launch boundary, next to the funnel it
# has to agree with.


def _defaults_backend_llm(task_spec: Optional[dict], task_file: Optional[str],
                          settings: dict, ui_settings: dict, root: Optional[Path] = None) -> bool:
    """True when a launch should default `backend="llm"`: the task normalizes to a GENERATIVE kind
    (the agent writes/edits code) and nobody chose a backend. CLI parity (mega-review P10):
    `looplab run --goal` already defaults backend=llm for these kinds (cli.py's `backend_chosen`
    rule), and `Settings.backend` used to default to "toy" — a repo/dataset run launched over HTTP
    without this got NoOpRepoDeveloper and every node silently re-evaluated the unchanged baseline
    (no error, just a flat run). Since 2026-08-04 that product default is "llm", so the inferred
    value normally EQUALS the resolved one and this rule changes no outcome on a stock deployment.
    It is kept, not deleted, because it is the only thing that makes the card/launch preview state
    the backend a generative task needs, and it becomes load-bearing again the moment a deployment
    sets a different `backend` default (an env/.env/UI value makes it "chosen", so it stays honest).
    A consequence worth knowing when reading the /api/start tests: the resolved `llm` is no longer a
    DEVIATION from the server's own `Settings()` baseline, so it stops travelling to the child as a
    LOOPLAB_BACKEND env var. The child still gets it — every resolved setting is written into the
    canonical unified `task.input.json`, whose `settings:` block reaches `appconfig.build_settings`
    as pydantic init kwargs and therefore outranks env/.env.

    Caller: the GENESIS CARD only, and display-only — so the operator can see and override the
    inferred backend before confirming. /api/start does NOT call this; it applies the same rule over
    its own already-layered settings in `_resolve_settings` above (`"backend" not in merged` plus the
    env check on `base.model_fields_set`). Two spellings of one rule, in one file so they can be read
    against each other: this one starts from a card's loose `(task_spec | task_file, settings,
    ui_settings)` and has to load the task file itself, which `_resolve_settings` has already done by
    the time it runs. Both defer to the same authorities, which is what keeps the card honest — the
    kind→backend rule is `engine/genesis.py::default_backend` (shared with cli.py's genesis
    defaulting), and "chosen" is the same `model_fields_set` test cli.py's `backend_chosen` uses.

    "Chosen" = a `backend` key already in the merged launch/card `settings`, or one the deployment
    set — a UI-saved value, LOOPLAB_BACKEND env, or a `.env` line all land in
    `Settings(**ui).model_fields_set` (and `_spawn_engine` overlays our env ON TOP of os.environ, so
    injecting would clobber it).

    `root` is the server's run root and is what makes the card's file read go through the SAME
    containment `/api/start` applies. A card whose `task_file` the launcher would refuse must not
    quietly read that file anyway to decorate itself: the genesis body takes `task_file` straight
    from the request, and this was the one remaining place a path outside the declared roots was
    still opened (unbounded, and on a FIFO it blocked the request). Its answer is a single bool, so
    the residual oracle was narrow — but the read was not. Omitting `root` keeps the historical
    uncontained read for the CLI-shaped callers that have no server root; the HTTP caller passes it.
    """
    if "backend" in settings:
        return False
    file_settings: dict = {}
    if not (isinstance(task_spec, dict) and task_spec):
        if not task_file:
            return False
        # A catalogue/snapshot launch: the task lives only in the file — read it with the SAME
        # loader the spawned engine uses (cli.py `run` → appconfig.load_document): it handles a
        # YAML catalogue entry, a unified config's `task:` block, and a BOM'd JSON, all of which a
        # raw json.loads mis-reads — so this default can never disagree with the task the engine
        # actually parses out of the very same file (read parity).
        try:
            if root is not None:
                expanded = os.path.expandvars(os.path.expanduser(str(task_file).strip()))
                task_spec, file_settings, _out = read_confined_task_file(root, expanded).document()
            else:
                from looplab.core.appconfig import load_document
                task_spec, file_settings, _out = load_document(Path(task_file))
        except (OSError, ValueError, HTTPException):
            return False                # unreadable/refused/foreign task file → no default
        if not (isinstance(task_spec, dict) and task_spec):
            return False
    from looplab.adapters.tasks import normalize_task
    from looplab.engine.genesis import default_backend
    # Best-effort, NARROW: only the task normalization may soft-fail here — an unnormalizable spec
    # is validate_task's 400 (or the engine's own startup error), never this default's concern.
    try:
        kind = normalize_task(dict(task_spec)).get("kind")
    except (KeyError, TypeError, ValueError):
        return False
    # `chosen=False` probe first: a non-generative kind can never default, so skip the Settings
    # construction (env + saved-UI validation) entirely for it.
    if default_backend(kind, chosen=False) != "llm":
        return False
    try:
        # A unified task file's settings outrank UI/env defaults in the CLI. Treat its backend as an
        # explicit choice too, so the display-only Genesis hint cannot promise llm while the child
        # would actually consume backend=toy from that file.
        selected = {**(ui_settings or {}), **(file_settings or {})}
        return "backend" not in getattr(Settings(**selected), "model_fields_set", set())
    except ValueError:  # pydantic ValidationError ⊂ ValueError — bad saved/env settings fail later,
        return False    # in the spawned engine's own Settings(); don't inject on top of them
