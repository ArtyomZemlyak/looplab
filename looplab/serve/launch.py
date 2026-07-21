"""Pure, fail-closed validation for new-run launches.

The browser, TUI, and direct HTTP callers all launch through ``POST /api/start``.  This module keeps
the expensive/read-only part of that boundary in one place so a launch preview and the real launch
cannot disagree.  It deliberately does not create directories, write files, acquire spawn leases,
construct an LLM client, or start a process.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from looplab.adapters import tasks as task_adapters
from looplab.core.appconfig import load_document
from looplab.core.comparison import canonical_comparison_contract
from looplab.core.config import Settings, canonicalize_parallelism_source
from looplab.serve.appstate import _RESERVED_RUN_IDS
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


_START_FIELDS = {
    "run_id", "task", "task_file", "settings", "chat", "validation_token", "idempotency_key",
}
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _detail(code: str, message: str, field_errors: Optional[dict[str, str]] = None, **extra) -> dict:
    detail: dict[str, Any] = {"code": code, "message": message, "field_errors": field_errors or {}}
    detail.update(extra)
    return detail


def _reject(status: int, code: str, message: str, field: str | None = None, **extra):
    errors = {field: message} if field else {}
    raise HTTPException(status, _detail(code, message, errors, **extra))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def safe_run_dir(root: Path, run_id: Any, *, check_conflict: bool = True) -> Path:
    if not isinstance(run_id, str) or not run_id:
        _reject(400, "invalid_run_id", "run_id is required", "run_id")
    if (len(run_id) > 255 or run_id != run_id.strip() or run_id.endswith((".", " "))
            or ":" in run_id or any(ord(ch) < 32 for ch in run_id)
            or run_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED):
        _reject(400, "invalid_run_id", "run_id is unsafe or filesystem-ambiguous", "run_id")
    requested = root / run_id
    try:
        resolved = requested.resolve()
    except OSError as exc:
        _reject(400, "invalid_run_id", f"run_id cannot be resolved: {exc}", "run_id")
    if resolved == root or resolved.parent != root:
        _reject(400, "invalid_run_id", "run_id must be a plain name, not a path", "run_id")
    if resolved.name.lower() in _RESERVED_RUN_IDS:
        _reject(400, "reserved_run_id", f"run_id {resolved.name!r} is reserved", "run_id")
    if requested.is_symlink():
        _reject(409, "run_path_conflict", "run path is a symbolic link", "run_id")
    if check_conflict and (resolved / "events.jsonl").exists():
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


# Task specs are small (a toy dict, a YAML/JSON config). Cap the WHOLE-file reads below — both
# load_document and _source_fingerprint slurp the file — so an unbounded pseudo-file or a multi-GB
# regular file cannot hang the preflight worker or exhaust memory. Every other launch input is bounded.
_MAX_TASK_FILE_BYTES = 8 * 1024 * 1024  # 8 MiB


def _require_task_file_size(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        _reject(400, "invalid_task_file", f"task_file cannot be read: {exc}", "task_file")
    if size > _MAX_TASK_FILE_BYTES:
        _reject(400, "task_file_too_large",
                f"task_file exceeds the {_MAX_TASK_FILE_BYTES}-byte limit", "task_file")


def _source_fingerprint(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        _require_task_file_size(path)
        stat = _path_stat(path)
        stat["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return stat
    except OSError as exc:
        _reject(422, "task_source_changed", f"task_file cannot be read: {exc}", "task_file")


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
    return base, _sha(dump)


def _resolved_settings(task: dict, saved: dict, file_settings: dict,
                       launch_settings: dict) -> tuple[dict, bool, str]:
    base, base_digest = _base_settings_fingerprint()
    # Saved UI settings < task-file settings < explicit launch settings. Canonicalize aliases in
    # each layer before flattening so a spelling change cannot invert source precedence.
    merged = {
        **canonicalize_parallelism_source(saved),
        **canonicalize_parallelism_source(file_settings),
        **canonicalize_parallelism_source(launch_settings),
    }
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
        """Cheap pre-Popen drift check; it performs only local reads/stats/settings construction."""
        safe_run_dir(srv.root, self.run_id, check_conflict=True)
        source = _source_fingerprint(Path(self.source_task_file) if self.source_task_file else None)
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
        source_path = Path(expanded).resolve()
        if not source_path.exists() or not source_path.is_file():
            _reject(400, "task_file_not_found", f"task_file not found: {source_path}", "task_file")
        _require_task_file_size(source_path)   # bound the read before load_document slurps the file
        try:
            raw_task, raw_file_settings, _out = load_document(source_path)
        except (OSError, ValueError, TypeError) as exc:
            _reject(400, "invalid_task_file", f"could not load task_file: {exc}", "task_file")
        task_input = raw_task
        file_settings = _validate_settings_keys(raw_file_settings, "task-file")
        source_fp = _source_fingerprint(source_path)
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
    # CODEX AGENT: this typed adapter dump is the one task document shared by browser, TUI, and
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
