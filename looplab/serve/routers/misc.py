"""Miscellaneous routes: UI default settings (+ the secret store), the task catalogue, LLM health,
the GPU monitor, files-as-truth authoring and the memory viewer. Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4).

ORDER IS LOAD-BEARING inside this router and for its placement: the generic authoring route
`GET /api/{kind}` full-matches ANY single-segment /api GET, so every such literal route must be
registered BEFORE it — this router therefore registers settings/tasks/health/gpu AND `/api/memory`
first (memory before `/api/{kind}`, else it's swallowed as an unknown kind → 404, the empty-Memory-
panel bug), and is included LAST among the /api routers by `make_app`."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import anyio
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from looplab.core.atomicio import (
    _ensure_strict_parent, atomic_write_text, strict_atomic_write_bytes,
    strict_atomic_write_text, strict_fsync_parent,
)
from looplab.core.config import Settings
from looplab.core.pathsafe import is_reparse
from looplab.events.eventstore import EventStoreLockError, _interprocess_lock
from looplab.serve.http import json_object, request_body_contract
from looplab.serve.assistant import safe_provider_failure
from looplab.serve.settings_store import (
    _ALLOWED_FIELDS, _SECRET_API_FIELDS, _SECRET_FIELDS,
)
from looplab.serve.settings_ui_schema import (
    SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_ETAG, SETTINGS_UI_SCHEMA_VERSION,
)
from looplab.core.redact import bounded_redacted_tree, redact_persisted_text


_MEMORY_TIER_LIMIT = 200
_MEMORY_SOURCE_BYTES = 2 * 1024 * 1024
_MEMORY_SOURCE_ROWS = 1000
# Bounds for the files-as-truth authoring routes. `knowledge_dir` is agent-writable (the engine's own
# `remember` tool), so `GET /api/{kind}` needs a hard ceiling or the loop can grow its own OOM; the
# name guard keeps `PUT /api/{kind}/{name}` to the authored-markdown surface `list_author` can show.
_AUTHOR_MAX_FILES = 500
_AUTHOR_MAX_BYTES = 256 * 1024
_AUTHOR_OPERATION_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_LLM_HEALTH_OPERATION_MAX = 256
_LLM_HEALTH_OPERATION_TTL_S = 10 * 60.0
_LLM_HEALTH_MAX_TOKENS = 4
_LLM_HEALTH_TIMEOUT_MIN_S = 0.25
_LLM_HEALTH_TIMEOUT_MAX_S = 60.0
_AUTHOR_REVISION_RE = re.compile(r"\A(?:missing|sha256:[0-9a-f]{64})\Z")
_AUTHOR_RESULT_REVISION_RE = re.compile(r"\A(?:missing|oversized|sha256:[0-9a-f]{64})\Z")
_AUTHOR_TARGET_ROOT_ID_RE = re.compile(r"\Aroot-sha256:[0-9a-f]{64}\Z")
_AUTHOR_OPERATION_SCHEMA = "looplab.authoring-operation/v1"
_AUTHOR_MISSING_REVISION = "missing"
_AUTHOR_OVERSIZED_REVISION = "oversized"
# Reuse the already-reserved root-sidecar namespace. Unlike `assistant/<sid>`, this directory has no
# user-addressable recursive-delete route, so an operation receipt cannot be erased through another API.
_AUTHOR_STATE_PARENT = ".command-locks"
_AUTHOR_RECEIPT_DIR = ".authoring-operations"
_AUTHOR_RECEIPT_LOCK = ".authoring-operations.lock"
_AUTHOR_MAX_RECEIPTS = 4096
_AUTHOR_RECEIPT_FIELDS = frozenset({
    "schema", "operation_id", "kind", "name", "target_root", "expected_revision",
    "target_root_id", "desired_revision", "status", "result_revision", "code",
    "created_at", "updated_at",
})
_AUTHOR_THREAD_LOCK = threading.Lock()
_ETAG_TOKEN = re.compile(r'(?:W/)?"[!#-~\x80-\xff]*"')
_SECRET_KEY_PATTERN = rf"^(?:{'|'.join(re.escape(key) for key in sorted(_SECRET_API_FIELDS))})$"


def _valid_author_name(value: object) -> bool:
    """Accept every safe, visible markdown basename, including spaces and Unicode."""
    return (isinstance(value, str)
            and 3 < len(value) <= 255
            and value.endswith(".md")
            and "/" not in value and "\\" not in value
            and Path(value).name == value
            and not any(unicodedata.category(ch).startswith("C") for ch in value))


class SettingsUIField(BaseModel):
    """One inert editor-field descriptor served to the React settings surfaces."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str
    options: list[str] | None = None
    help: str | None = None
    placeholder: str | int | float | None = None
    warning: str | None = None
    warningTitle: str | None = None
    warningTone: str | None = None
    agents: list[str] | None = None
    minimum: int | float | None = None
    exclusiveMinimum: int | float | None = None
    maximum: int | float | None = None
    # The catalogue loader (settings_ui_schema._load_schema) maps a Settings `lt=` bound to
    # `exclusiveMaximum` and carries an `essential` bool; this forbid-extras response model must accept
    # both, or the first field using either turns GET /api/settings/schema/{v} into a 500.
    exclusiveMaximum: int | float | None = None
    essential: bool | None = None
    nullable: bool
    # The default this row's copy was reviewed against — pinned in the catalogue and verified against
    # the live model at load (settings_ui_schema._check_pinned_default), so it is display data the
    # browser can trust rather than a second, drifting copy. Every row carries one, but it stays
    # OPTIONAL here because a Settings field may legitimately default to None, which serializes
    # identically to an absent key: requiring it at this boundary would buy nothing the loader does
    # not already enforce precisely. Declared explicitly because this model forbids extras.
    default: bool | int | float | str | list | dict | None = None


class SettingsUIGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    sub: str
    fields: list[SettingsUIField]


class SettingsUIRolePill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short: str
    title: str


class SettingsUISchemaResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: int = Field(alias="schema")
    groups: list[SettingsUIGroup]
    agent_role_pills: dict[str, SettingsUIRolePill]
    revision: str


class CredentialStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["none", "stored", "environment", "dotenv"]
    stored: bool
    effective: bool
    active: bool
    clearable: bool
    status: Literal[
        "active", "missing", "unbound", "incomplete", "endpoint_mismatch", "ambient_override",
    ]


class SettingsSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    overrides: dict[str, Any]
    defaults: dict[str, Any]
    settings_revision: str
    secret_revision: str
    credential: CredentialStatusResponse


class LLMHealthRequest(BaseModel):
    """One paid provider probe bound to the saved Settings snapshot the owner displayed."""

    model_config = ConfigDict(extra="forbid")

    expected_settings_revision: Annotated[str, Field(min_length=1, max_length=256)]
    expected_secret_revision: Annotated[str, Field(min_length=1, max_length=256)]
    operation_id: str = Field(
        min_length=36, max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    replay_only: bool = False


class SettingsUpdateRequest(BaseModel):
    """Preferred CAS-aware request envelope; legacy flat dictionaries remain accepted."""

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    # Optional so the published OpenAPI is nullable (not the self-contradictory {"type":"string",
    # "default":null}); _expected_revision treats an explicit null the same as absent (no CAS).
    expected_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None


class LegacySettingsUpdateRequest(BaseModel):
    """Compatibility body with setting keys directly at the top level."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={"not": {"required": ["settings"]}},
    )

    expected_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None


class SettingsUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    settings: dict[str, Any]
    overrides: dict[str, Any]
    settings_revision: str
    secret_revision: str
    credential: CredentialStatusResponse


class SecretUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=_SECRET_KEY_PATTERN)
    value: str | None = None
    expected_settings_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None
    expected_secret_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None
    # Compatibility with the previous secret-only CAS field.
    expected_revision: Annotated[Optional[str], Field(min_length=1, max_length=256)] = None


class SecretUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    key: str
    set: bool
    settings_revision: str
    secret_revision: str
    credential: CredentialStatusResponse


class AuthoringOperationRequest(BaseModel):
    """One replayable file mutation bound to the exact source revision the editor displayed."""

    model_config = ConfigDict(extra="forbid")

    text: str
    expected_revision: str = Field(
        min_length=7, max_length=71,
        pattern=r"^(?:missing|sha256:[0-9a-f]{64})$",
    )
    expected_target_root_id: str = Field(
        min_length=76, max_length=76,
        pattern=r"^root-sha256:[0-9a-f]{64}$",
    )


class AuthoringOperationResponse(BaseModel):
    """Public, content-free receipt; the submitted text is represented only by its revision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(alias="schema")
    operation_id: str
    kind: Literal["prompts", "skills", "knowledge"]
    name: str
    target_root_id: str
    expected_revision: str
    desired_revision: str
    status: Literal["prepared", "succeeded", "conflict"]
    result_revision: str | None
    code: str | None
    created_at: int
    updated_at: int
    ok: bool
    replayable: bool


def _if_none_match(value: str | None, current: str) -> bool:
    """Use RFC 9110 weak comparison for an If-None-Match validator list."""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    if value == "*":
        return True
    target = current[2:] if current.startswith("W/") else current
    matched = False
    position = 0
    while position < len(value):
        while position < len(value) and value[position] in " \t,":
            position += 1
        if position == len(value):
            break
        match = _ETAG_TOKEN.match(value, position)
        if match is None:
            return False
        candidate = match.group(0)
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == target:
            matched = True
        position = match.end()
        while position < len(value) and value[position] in " \t":
            position += 1
        if position < len(value) and value[position] != ",":
            return False
    return matched


_MEMORY_ROW_BYTES = 128 * 1024


def _expected_revision(body: dict) -> Optional[str]:
    """Parse the optional opaque CAS token without assigning it client-visible semantics."""
    if "expected_revision" not in body:
        return None
    revision = body["expected_revision"]
    if revision is None:      # explicit null == absent (no CAS), matching the now-nullable request schema
        return None
    if not isinstance(revision, str) or not revision or len(revision) > 256:
        raise HTTPException(400, "expected_revision must be a non-empty opaque string")
    return revision


def _expected_named_revision(body: dict, name: str) -> Optional[str]:
    """Parse one optional named opaque CAS token."""
    if name not in body or body[name] is None:
        return None
    revision = body[name]
    if not isinstance(revision, str) or not revision or len(revision) > 256:
        raise HTTPException(400, f"{name} must be a non-empty opaque string")
    return revision


def _revision_conflict(resource: str, expected: str, current: str) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": f"{resource}_revision_conflict",
        "resource": resource,
        "message": f"{resource.capitalize()} changed after this form was loaded; reload and retry.",
        "expected_revision": expected,
        "current_revision": current,
    })


def _memory_text(value, maximum: int, *, entropy: bool = True) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return " ".join(redact_persisted_text(
        value, max_chars=maximum, entropy=entropy, single_line=True).split())


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


# See the genesis sibling for why the character cell is derived from the node budget rather than
# chosen: this projection only ever meant to bound NODES.
_MEMORY_NODES = 96
_MEMORY_STR_CAP = 500


def _bounded_json_value(value):
    """Bound one case param tree by depth, fanout and a shared scalar-item budget.

    The walk is `core/redact.py::bounded_redacted_tree`, shared with the span/trace sanitizer, the
    advisory payloads and the genesis evidence projection (doc 25 SR-06). Masking a secret-NAMED key
    is why sharing matters here: `redact_persisted_text`'s entropy pass only catches high-entropy
    strings, so a short wordlike credential like `{"api_key": "tok-abcd"}` would otherwise reach the
    memory-browser response intact — and the classification happens on the ORIGINAL key (8d1bcda),
    which is a rule that had to hold in every copy and did not.
    """
    truncated = [False]
    projected = bounded_redacted_tree(
        value, [_MEMORY_NODES * _MEMORY_STR_CAP], [_MEMORY_NODES],
        max_items=32, max_depth=2, str_cap=_MEMORY_STR_CAP, key_cap=80,
        truncated=truncated)
    return projected, truncated[0]


def _project_memory_row(tier: str, row) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    if tier == "cases":
        task_id = _memory_text(row.get("task_id"), 500, entropy=False)
        if not task_id:
            return None
        params, params_truncated = _bounded_json_value(row.get("params", {}))
        out = {"task_id": task_id, "goal": _memory_text(row.get("goal"), 1000),
               "direction": row.get("direction") if row.get("direction") in ("min", "max") else "min",
               "metric": _finite_number(row.get("metric")), "params": params}
        rationale = _memory_text(row.get("rationale"), 1000)
        if rationale:
            out["rationale"] = rationale
        if params_truncated:
            out["params_truncated"] = True
        return out
    if tier == "lessons":
        statement = _memory_text(row.get("statement"), 1000)
        if not statement:
            return None
        out = {"statement": statement}
        for key, maximum in (("run_id", 500), ("task_id", 500), ("role", 40),
                             ("kind", 80), ("outcome", 48), ("claim_stance", 24)):
            value = _memory_text(row.get(key), maximum, entropy=key not in ("run_id", "task_id"))
            if value:
                out[key] = value
        for key in ("delta", "confidence"):
            value = _finite_number(row.get(key))
            if value is not None:
                out[key] = value
        evidence_count = row.get("evidence_count")
        if isinstance(evidence_count, int) and not isinstance(evidence_count, bool):
            out["evidence_count"] = max(0, evidence_count)
        return out
    note = _memory_text(row.get("note") or row.get("statement"), 4000)
    if not note:
        return None
    out = {"note": note}
    for key, maximum in (("run_id", 500), ("task_id", 500), ("at", 120)):
        value = _memory_text(row.get(key), maximum, entropy=key not in ("run_id", "task_id", "at"))
        if value:
            out[key] = value
    return out


def _read_memory_tier(path: Path, tier: str) -> tuple[list[dict], dict]:
    receipt = {"limit": _MEMORY_TIER_LIMIT, "returned": 0, "skipped": 0,
               "source_window_truncated": False, "unavailable": False}
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - _MEMORY_SOURCE_BYTES)
            preceding = b"\n"
            if start:
                handle.seek(start - 1)
                preceding = handle.read(1)
            handle.seek(start)
            raw = handle.read(end - start)
    except FileNotFoundError:
        return [], receipt
    except OSError:
        receipt["unavailable"] = True
        return [], receipt

    receipt["source_window_truncated"] = start > 0
    if start and preceding != b"\n":
        boundary = raw.find(b"\n")
        if boundary < 0:
            receipt["skipped"] = 1
            return [], receipt
        raw = raw[boundary + 1:]
        receipt["skipped"] += 1
    encoded = raw.splitlines()
    if len(encoded) > _MEMORY_SOURCE_ROWS:
        encoded = encoded[-_MEMORY_SOURCE_ROWS:]
        receipt["source_window_truncated"] = True
    projected = []
    for line in encoded:
        if not line.strip():
            continue
        if len(line) > _MEMORY_ROW_BYTES:
            receipt["skipped"] += 1
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            receipt["skipped"] += 1
            continue
        safe = _project_memory_row(tier, row)
        if safe is None:
            receipt["skipped"] += 1
            continue
        projected.append(safe)
    if len(projected) > _MEMORY_TIER_LIMIT:
        projected = projected[-_MEMORY_TIER_LIMIT:]
        receipt["source_window_truncated"] = True
    receipt["returned"] = len(projected)
    return projected, receipt


class _AuthoringFailure(RuntimeError):
    """Stable HTTP-facing failure for the authoring operation store."""

    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool):
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def _authoring_http_failure(exc: _AuthoringFailure, operation_id: str | None = None) -> HTTPException:
    detail: dict[str, Any] = {
        "code": exc.code,
        "message": str(exc),
        "retryable": exc.retryable,
    }
    if operation_id is not None:
        detail["operation_id"] = operation_id
    return HTTPException(
        exc.status_code, detail, headers={"Cache-Control": "private, no-store"})


def _author_revision(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _author_target_root_id(root: Path) -> str:
    # `resolve()` is performed by the caller. normcase closes aliases on case-insensitive desktop
    # filesystems without exposing the canonical path itself on the wire.
    canonical = os.path.normcase(os.path.abspath(os.fspath(root)))
    material = f"looplab-authoring-root-v1\0{canonical}".encode("utf-8")
    return f"root-sha256:{hashlib.sha256(material).hexdigest()}"


def _author_state_parent(srv, *, create: bool) -> Path:
    parent = srv.root.resolve() / _AUTHOR_STATE_PARENT
    try:
        entry = parent.lstat()
    except FileNotFoundError:
        if not create:
            return parent
        try:
            _ensure_strict_parent(parent)
            entry = parent.lstat()
        except (OSError, TimeoutError, RuntimeError) as exc:
            raise _AuthoringFailure(
                503, "authoring_receipt_unavailable",
                "The authoring metadata namespace could not be created.", retryable=True) from exc
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring metadata namespace could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring metadata namespace is not a trusted directory.", retryable=False)
    if create:
        try:
            strict_fsync_parent(parent)
        except (OSError, TimeoutError, RuntimeError) as exc:
            raise _AuthoringFailure(
                503, "authoring_receipt_unavailable",
                "The authoring metadata namespace could not be durably confirmed.",
                retryable=True) from exc
    return parent


def _author_operation_path(srv, operation_id: str) -> Path:
    directory = _author_state_parent(srv, create=False) / _AUTHOR_RECEIPT_DIR
    try:
        entry = directory.lstat()
    except FileNotFoundError:
        return directory / f"{operation_id}.json"
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt store could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt store is not a trusted directory.", retryable=False)
    return directory / f"{operation_id}.json"


def _author_operation_lock_path(srv) -> Path:
    path = _author_state_parent(srv, create=True) / _AUTHOR_RECEIPT_LOCK
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "The authoring operation lock could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_lock_invalid",
            "The authoring operation lock is not a trusted regular file.", retryable=False)
    return path


def _valid_author_receipt(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _AUTHOR_RECEIPT_FIELDS:
        return False
    operation_id = value.get("operation_id")
    kind = value.get("kind")
    name = value.get("name")
    target_root = value.get("target_root")
    target_root_id = value.get("target_root_id")
    expected = value.get("expected_revision")
    desired = value.get("desired_revision")
    status_value = value.get("status")
    result = value.get("result_revision")
    code = value.get("code")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    if (value.get("schema") != _AUTHOR_OPERATION_SCHEMA
            or not isinstance(operation_id, str)
            or _AUTHOR_OPERATION_RE.fullmatch(operation_id) is None
            or kind not in ("prompts", "skills", "knowledge")
            or not _valid_author_name(name)
            or not isinstance(target_root, str) or not target_root or len(target_root) > 4096
            or not Path(target_root).is_absolute()
            or not isinstance(target_root_id, str)
            or _AUTHOR_TARGET_ROOT_ID_RE.fullmatch(target_root_id) is None
            or target_root_id != _author_target_root_id(Path(target_root))
            or not isinstance(expected, str) or _AUTHOR_REVISION_RE.fullmatch(expected) is None
            or not isinstance(desired, str) or _AUTHOR_REVISION_RE.fullmatch(desired) is None
            or desired == _AUTHOR_MISSING_REVISION
            or status_value not in ("prepared", "succeeded", "conflict")
            or type(created_at) is not int or created_at < 0 or created_at > 9_000_000_000_000_000
            or type(updated_at) is not int or updated_at < created_at
            or updated_at > 9_000_000_000_000_000):
        return False
    if status_value == "prepared":
        return result is None and code is None
    if status_value == "succeeded":
        return result == desired and code is None
    return (isinstance(result, str)
            and _AUTHOR_RESULT_REVISION_RE.fullmatch(result) is not None
            and code in {"authoring_revision_conflict", "authoring_intervening_write"})


def _load_author_receipt(path: Path) -> dict[str, Any] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt path is not a trusted regular file.", retryable=False)
    try:
        with path.open("rb") as handle:
            raw = handle.read(8193)
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt could not be read.", retryable=True) from exc
    if len(raw) > 8192:
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt is larger than its protocol bound.", retryable=False)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt is malformed.", retryable=False) from exc
    if not _valid_author_receipt(value):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt does not satisfy the v1 protocol.", retryable=False)
    return value


def _save_author_receipt(path: Path, receipt: dict[str, Any]) -> None:
    if not _valid_author_receipt(receipt):
        raise AssertionError("refusing to persist an invalid authoring receipt")
    encoded = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        strict_atomic_write_text(path, encoded)
    except (OSError, TimeoutError, RuntimeError) as exc:
        # The strict helper can fail after the atomic replace but before confirming the parent
        # directory. Treat that outcome as indeterminate. Repeating this exact PUT republishes the
        # same prepared/terminal receipt before it trusts the record or mutates the authored file.
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt could not be durably published.", retryable=True) from exc


def _public_author_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    public = {key: receipt[key] for key in (
        "schema", "operation_id", "kind", "name", "target_root_id", "expected_revision",
        "desired_revision", "status", "result_revision", "code", "created_at", "updated_at",
    )}
    public["ok"] = receipt["status"] == "succeeded"
    public["replayable"] = receipt["status"] == "prepared"
    return public


def _author_target_revision(target: Path) -> str:
    try:
        entry = target.lstat()
    except FileNotFoundError:
        return _AUTHOR_MISSING_REVISION
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_target_unavailable",
            "The authored file could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISREG(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_target_untrusted",
            "The authored path is not a trusted regular file.", retryable=False)
    try:
        with target.open("rb") as handle:
            raw = handle.read(_AUTHOR_MAX_BYTES + 1)
    except FileNotFoundError:
        return _AUTHOR_MISSING_REVISION
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_target_unavailable",
            "The authored file could not be read.", retryable=True) from exc
    if len(raw) > _AUTHOR_MAX_BYTES:
        return _AUTHOR_OVERSIZED_REVISION
    return _author_revision(raw)


def _assert_author_receipt_capacity(srv) -> None:
    """Bound the idempotency set without forgetting an accepted operation UUID.

    TTL/LRU deletion would let an old timed-out request be applied again after its tombstone vanished.
    A hard cap is the bounded policy that preserves permanent replay safety.
    """
    directory = _author_state_parent(srv, create=False) / _AUTHOR_RECEIPT_DIR
    try:
        entry = directory.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt store could not be inspected.", retryable=True) from exc
    if is_reparse(entry) or not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_receipt_invalid",
            "The authoring receipt store is not a trusted directory.", retryable=False)
    try:
        with os.scandir(directory) as entries:
            for index, _entry in enumerate(entries, start=1):
                if index >= _AUTHOR_MAX_RECEIPTS:
                    raise _AuthoringFailure(
                        409, "authoring_receipt_quota_exhausted",
                        f"The authoring receipt limit ({_AUTHOR_MAX_RECEIPTS}) is exhausted; "
                        "new operation identities are disabled to preserve idempotency.",
                        retryable=False)
    except _AuthoringFailure:
        raise
    except OSError as exc:
        raise _AuthoringFailure(
            503, "authoring_receipt_unavailable",
            "The authoring receipt store could not be enumerated.", retryable=True) from exc


def _resolved_author_root(directory: Path | None, kind: str) -> Path:
    if directory is None:
        raise _AuthoringFailure(
            400, "authoring_directory_unconfigured",
            f"No {kind} directory is configured.", retryable=False)
    try:
        _ensure_strict_parent(directory)
        root = directory.resolve(strict=True)
        entry = root.stat()
        strict_fsync_parent(root)
    except (OSError, TimeoutError, RuntimeError) as exc:
        raise _AuthoringFailure(
            503, "authoring_directory_unavailable",
            f"The configured {kind} directory is unavailable.", retryable=True) from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise _AuthoringFailure(
            409, "authoring_directory_invalid",
            f"The configured {kind} path is not a directory.", retryable=False)
    return root


def _configured_author_root(directory: Path | None, kind: str) -> Path:
    """Resolve configuration identity without creating directories (safe for receipt GETs)."""
    if directory is None:
        raise _AuthoringFailure(
            409, "authoring_operation_target_changed",
            f"The configured {kind} directory is no longer available for this operation.",
            retryable=False)
    try:
        return directory.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _AuthoringFailure(
            503, "authoring_directory_unavailable",
            f"The configured {kind} directory identity could not be inspected.",
            retryable=True) from exc


def _current_author_directory(srv, kind: str) -> Path | None:
    """Read one configured-directory snapshot (mutation callers hold the settings lock)."""
    settings = srv.global_settings()
    configured = {
        "prompts": settings.prompt_dir,
        "skills": settings.skills_dir,
        "knowledge": settings.knowledge_dir,
    }.get(kind)
    return Path(configured) if configured else None


def _new_author_receipt(*, operation_id: str, kind: str, name: str, target_root: Path,
                        target_root_id: str, expected_revision: str, desired_revision: str,
                        status_value: str,
                        result_revision: str | None = None,
                        code: str | None = None) -> dict[str, Any]:
    now = time.time_ns() // 1_000_000
    return {
        "schema": _AUTHOR_OPERATION_SCHEMA,
        "operation_id": operation_id,
        "kind": kind,
        "name": name,
        "target_root": str(target_root),
        "target_root_id": target_root_id,
        "expected_revision": expected_revision,
        "desired_revision": desired_revision,
        "status": status_value,
        "result_revision": result_revision,
        "code": code,
        "created_at": now,
        "updated_at": now,
    }


def _same_author_operation(receipt: dict[str, Any], *, operation_id: str, kind: str,
                           name: str, target_root_id: str, expected_revision: str,
                           desired_revision: str) -> bool:
    return (receipt["operation_id"] == operation_id
            and receipt["kind"] == kind
            and receipt["name"] == name
            and receipt["target_root_id"] == target_root_id
            and receipt["expected_revision"] == expected_revision
            and receipt["desired_revision"] == desired_revision)


def _run_author_operation(srv, *, kind: str, name: str, operation_id: str, text_bytes: bytes,
                          expected_revision: str,
                          expected_target_root_id: str) -> dict[str, Any]:
    desired_revision = _author_revision(text_bytes)
    receipt_path = _author_operation_path(srv, operation_id)
    try:
        # Hold both locks for the full transition. The settings lock makes "current root" a real
        # precondition rather than a Path captured just before the authoring lock was acquired.
        with _AUTHOR_THREAD_LOCK, _interprocess_lock(
                _author_operation_lock_path(srv), required=True), \
                srv.settings.ui_settings_transaction():
            directory = _current_author_directory(srv, kind)
            receipt = _load_author_receipt(receipt_path)
            if receipt is not None:
                if not _same_author_operation(
                        receipt, operation_id=operation_id, kind=kind, name=name,
                        target_root_id=expected_target_root_id,
                        expected_revision=expected_revision,
                        desired_revision=desired_revision):
                    raise _AuthoringFailure(
                        409, "authoring_operation_conflict",
                        "That operation id is already bound to a different authoring payload.",
                        retryable=False)
                configured_root = _configured_author_root(directory, kind)
                configured_root_id = _author_target_root_id(configured_root)
                if (receipt["target_root_id"] != configured_root_id
                        or expected_target_root_id != configured_root_id):
                    raise _AuthoringFailure(
                        409, "authoring_operation_target_changed",
                        "The configured authoring directory changed after this operation began.",
                        retryable=False)
                # Re-publish the exact terminal record before trusting it. This is harmless after a
                # confirmed write and closes strict_atomic_write's visible-but-unconfirmed failure gap.
                if receipt["status"] != "prepared":
                    _save_author_receipt(receipt_path, receipt)
                    return _public_author_receipt(receipt)

            if receipt is None:
                _assert_author_receipt_capacity(srv)
                listed_root = _configured_author_root(directory, kind)
                if _author_target_root_id(listed_root) != expected_target_root_id:
                    raise _AuthoringFailure(
                        409, "authoring_operation_target_changed",
                        "The configured authoring directory changed after its list snapshot.",
                        retryable=False)

            root = _resolved_author_root(directory, kind)
            current_target_root_id = _author_target_root_id(root)
            if current_target_root_id != expected_target_root_id:
                raise _AuthoringFailure(
                    409, "authoring_operation_target_changed",
                    "The configured authoring directory changed after its list snapshot.",
                    retryable=False)
            target = root / name
            if receipt is not None and receipt["target_root_id"] != current_target_root_id:
                raise _AuthoringFailure(
                    409, "authoring_operation_target_changed",
                    "The configured authoring directory changed while this operation was pending.",
                    retryable=False)

            if receipt is None:
                current_revision = _author_target_revision(target)
                if current_revision != expected_revision:
                    receipt = _new_author_receipt(
                        operation_id=operation_id, kind=kind, name=name, target_root=root,
                        target_root_id=current_target_root_id,
                        expected_revision=expected_revision, desired_revision=desired_revision,
                        status_value="conflict", result_revision=current_revision,
                        code="authoring_revision_conflict")
                    _save_author_receipt(receipt_path, receipt)
                    return _public_author_receipt(receipt)
                receipt = _new_author_receipt(
                    operation_id=operation_id, kind=kind, name=name, target_root=root,
                    target_root_id=current_target_root_id,
                    expected_revision=expected_revision, desired_revision=desired_revision,
                    status_value="prepared")
            # A prepared record may be the visible side of a failed strict publication. Confirm the
            # same immutable intent before inspecting/replaying its file effect.
            _save_author_receipt(receipt_path, receipt)

            current_revision = _author_target_revision(target)
            if current_revision not in {expected_revision, desired_revision}:
                receipt = {
                    **receipt,
                    "status": "conflict",
                    "result_revision": current_revision,
                    "code": "authoring_intervening_write",
                    "updated_at": max(receipt["updated_at"], time.time_ns() // 1_000_000),
                }
                _save_author_receipt(receipt_path, receipt)
                return _public_author_receipt(receipt)

            # This second observation narrows the only unavoidable gap left by writers that do not
            # participate in the authoring lock (manual editors and generic owner file tools). The
            # CAS guarantee is exact across UI/API writers; an uncoordinated filesystem writer can
            # still race the atomic replace, so the postcondition below also fails closed.
            precommit_revision = _author_target_revision(target)
            if precommit_revision not in {expected_revision, desired_revision}:
                receipt = {
                    **receipt,
                    "status": "conflict",
                    "result_revision": precommit_revision,
                    "code": "authoring_intervening_write",
                    "updated_at": max(receipt["updated_at"], time.time_ns() // 1_000_000),
                }
                _save_author_receipt(receipt_path, receipt)
                return _public_author_receipt(receipt)

            # Rewriting identical bytes is intentional after an ambiguous target publication: it
            # obtains a fresh durability receipt without changing the operation's semantic result.
            try:
                strict_atomic_write_bytes(target, text_bytes)
            except (OSError, TimeoutError, RuntimeError) as exc:
                raise _AuthoringFailure(
                    503, "authoring_target_unavailable",
                    "The authored file write could not be durably confirmed.", retryable=True) from exc
            if _author_target_revision(target) != desired_revision:
                raise _AuthoringFailure(
                    503, "authoring_target_unavailable",
                    "The authored file did not match the submitted payload after its write.",
                    retryable=True)
            receipt = {
                **receipt,
                "status": "succeeded",
                "result_revision": desired_revision,
                "code": None,
                "updated_at": max(receipt["updated_at"], time.time_ns() // 1_000_000),
            }
            _save_author_receipt(receipt_path, receipt)
            return _public_author_receipt(receipt)
    except EventStoreLockError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "Authoring mutation serialization is unavailable.", retryable=True) from exc


def _lookup_author_operation(srv, *, kind: str, name: str, operation_id: str,
                             expected_target_root_id: str, expected_revision: str,
                             desired_revision: str) -> dict[str, Any]:
    try:
        with srv.settings.ui_settings_transaction():
            directory = _current_author_directory(srv, kind)
            receipt = _load_author_receipt(_author_operation_path(srv, operation_id))
            if receipt is None or receipt["kind"] != kind or receipt["name"] != name:
                raise _AuthoringFailure(
                    404, "authoring_operation_not_found",
                    "No receipt exists for that exact authoring operation.", retryable=False)
            if not _same_author_operation(
                    receipt, operation_id=operation_id, kind=kind, name=name,
                    target_root_id=expected_target_root_id,
                    expected_revision=expected_revision, desired_revision=desired_revision):
                raise _AuthoringFailure(
                    409, "authoring_operation_conflict",
                    "That operation id is bound to a different authoring payload or root identity.",
                    retryable=False)
            configured_root = _configured_author_root(directory, kind)
            if receipt["target_root_id"] != _author_target_root_id(configured_root):
                raise _AuthoringFailure(
                    409, "authoring_operation_target_changed",
                    "The configured authoring directory changed after this operation began.",
                    retryable=False)
            return _public_author_receipt(receipt)
    except EventStoreLockError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "Authoring receipt validation is unavailable.", retryable=True) from exc


def _run_legacy_author_write(srv, *, kind: str, name: str, text: str) -> dict[str, Any]:
    try:
        with _AUTHOR_THREAD_LOCK, _interprocess_lock(
                _author_operation_lock_path(srv), required=True), \
                srv.settings.ui_settings_transaction():
            directory = _current_author_directory(srv, kind)
            root = _resolved_author_root(directory, kind)
            target = root / name
            current = _author_target_revision(target)
            if current == _AUTHOR_OVERSIZED_REVISION:
                raise _AuthoringFailure(
                    409, "authoring_target_oversized",
                    "The existing file is larger than the editor's safe write bound.",
                    retryable=False)
            try:
                atomic_write_text(target, text)
            except OSError as exc:
                raise _AuthoringFailure(
                    503, "authoring_target_unavailable",
                    "The authored file could not be written.", retryable=True) from exc
            return {"ok": True, "name": name,
                    "revision": _author_target_revision(target), "legacy": True}
    except EventStoreLockError as exc:
        raise _AuthoringFailure(
            503, "authoring_lock_unavailable",
            "Authoring mutation serialization is unavailable.", retryable=True) from exc


def build_router(srv) -> APIRouter:
    router = APIRouter()
    store = srv.settings
    # App-local paid-probe registry. It intentionally does not cross run roots/processes and retains
    # only opaque request identities plus terminal public envelopes -- never Settings, credentials,
    # provider output, clients, or raw exceptions.
    llm_health_operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
    llm_health_operations_lock = threading.Lock()
    llm_health_identity_key = secrets.token_bytes(32)

    # ------------------------------------------------------------------ settings (UI defaults)
    # The engine has no settings server (ADR-18); these are UI-chosen DEFAULTS for new runs,
    # persisted at <run-root>/ui_settings.json and applied to a spawned run as LOOPLAB_* env.
    @router.get(
        "/api/settings/schema/{version}",
        response_model=SettingsUISchemaResponse,
        response_model_exclude_unset=True,
        responses={304: {"description": "The cached semantic schema revision is still current."}},
    )
    def get_settings_schema(version: int, request: Request, response: Response):
        """Revalidated display metadata for the versioned Settings/Config form contract.

        The envelope version describes the wire format. Labels and bounds are derived from the
        deployed Settings model and may change without another format bump, so this stable URL must
        consult its semantic ETag instead of being cached as immutable.
        """
        if version != SETTINGS_UI_SCHEMA_VERSION:
            raise HTTPException(404, "unknown settings UI schema version")
        headers = {
            "Cache-Control": "private, no-cache, max-age=0, must-revalidate",
            "ETag": SETTINGS_UI_SCHEMA_ETAG,
            "X-LoopLab-Schema-Version": str(SETTINGS_UI_SCHEMA_VERSION),
        }
        if _if_none_match(request.headers.get("if-none-match"), SETTINGS_UI_SCHEMA_ETAG):
            return Response(status_code=304, headers=headers)
        for name, value in headers.items():
            response.headers[name] = value
        return SETTINGS_UI_SCHEMA

    @router.get("/api/settings", response_model=SettingsSnapshotResponse)
    def get_settings():
        # Bind each returned resource snapshot to the revision checked by its next PUT. The locks
        # prevent a concurrent rename (or secret env mutation) between payload and token reads.
        # This is the only two-resource lock site; its fixed UI-then-secret order avoids lock cycles.
        with store.ui_settings_transaction(), store.secret_transaction():
            overrides = store.load_ui_settings()
            s, credential = store.settings_and_credential(overrides)
            defaults = Settings().model_dump()
            defaults.pop("llm_api_key", None)
            defaults.pop("llm_api_key_base_url", None)
            response = {
                "settings": s.masked_snapshot(),
                "overrides": overrides,
                "defaults": defaults,
                "settings_revision": store.ui_settings_revision(),
                "secret_revision": store.secret_revision(),
                "credential": credential,
            }
        return response

    @router.put(
        "/api/settings",
        response_model=SettingsUpdateResponse,
        openapi_extra=request_body_contract(SettingsUpdateRequest, LegacySettingsUpdateRequest),
    )
    async def put_settings(request: Request):
        body = await json_object(request, "settings payload")
        expected_revision = _expected_revision(body)
        incoming = body.get("settings", body)
        if not isinstance(incoming, dict):
            raise HTTPException(400, "settings must be a JSON object")
        # Keep only known, non-secret fields whose value differs from the engine default — the file
        # stays a small, readable diff rather than a full mirror of every Settings field. Diff
        # against the PROFILE-expanded defaults: the form echoes the expanded snapshot back, and
        # diffing against bare defaults would persist every profile value as an explicit override
        # (a one-way ratchet the profile selector could never undo) while dropping an explicit
        # knob that happens to equal the bare default (breaking "explicit knob wins").
        # Atomic rename prevents torn JSON but cannot protect this larger load→merge→write cycle:
        # two concurrent disjoint PUTs must observe one another instead of losing the first rename.
        # OFF the event loop. `ui_settings_transaction()` takes a threading.Lock plus
        # `_interprocess_lock(required=True)` — a blocking `fcntl.flock` with NO timeout — and then
        # does load / merge / `Settings()` validation / atomic write inline. Run inline on the ASGI
        # loop, a lock another server process holds froze every SSE stream and poll on this worker
        # until it was released. Same offload `/control` and `submit_command` already use; the JSON
        # parse and shape checks above stay on the loop because they are cheap and need `await`.
        def _apply() -> dict:
            # Settings and credential status are one UI→secret snapshot. Endpoint-only saves can
            # deactivate an old binding, and the response must say so immediately (no stale green UI).
            with store.settings_write_transaction():
                current_revision = store.ui_settings_revision()
                if expected_revision is not None and expected_revision != current_revision:
                    raise _revision_conflict("settings", expected_revision, current_revision)
                current = store.load_ui_settings()
                prev = store.resolved_settings()
                candidate = dict(current)
                for k, v in incoming.items():
                    if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS:
                        continue
                    if k == "agent_control" and isinstance(v, dict):
                        # Governance is a nested sparse PATCH too. Start from the resolved map so
                        # the first customization retains shipped defaults; sparse edits from stale tabs
                        # then merge by governed setting instead of replacing one another wholesale.
                        old_control = prev.get("agent_control")
                        merged_control = dict(old_control) if isinstance(old_control, dict) else {}
                        for setting_key, roles in v.items():
                            if roles is None:
                                merged_control.pop(setting_key, None)
                            else:
                                merged_control[setting_key] = roles
                        candidate[k] = merged_control
                    elif v is None:
                        candidate.pop(k, None)
                    else:
                        candidate[k] = v
                profile = candidate.get("profile") or "default"
                try:
                    base = Settings(profile=profile).model_dump()
                except Exception:  # noqa: BLE001 — unknown profile: fall back to bare defaults
                    base = Settings().model_dump()
                # Fields the form merely ECHOES from the previous resolved snapshot are not user edits:
                # when the profile changes, those echoes must fall away with the old profile, not stick.
                overrides = {}
                profile_changed = "profile" in incoming and profile != prev.get("profile")
                for k, v in candidate.items():
                    if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS:
                        continue
                    if k == "profile":
                        if v != Settings.model_fields["profile"].default:
                            overrides[k] = v
                        continue
                    if base.get(k) == v:
                        continue
                    if profile_changed and k in incoming and k not in current and prev.get(k) == v:
                        continue                       # unchanged echo of the old profile's expansion
                    overrides[k] = v
                try:
                    Settings(**overrides)
                except Exception as exc:  # noqa: BLE001 - reject before persisting a poison configuration
                    raise HTTPException(422, f"invalid settings: {exc}") from exc
                # PATCH-like contract: omission preserves opaque overrides; explicit null/default removes one.
                revision = store.write_ui_settings(overrides)
                resolved, credential = store.settings_and_credential(overrides)
                return {"ok": True, "settings": resolved.masked_snapshot(), "overrides": overrides,
                        "settings_revision": revision,
                        "secret_revision": store.secret_revision(), "credential": credential}
        return await anyio.to_thread.run_sync(_apply)

    @router.put(
        "/api/settings/secret",
        response_model=SecretUpdateResponse,
        openapi_extra=request_body_contract(SecretUpdateRequest),
    )
    async def put_secret(request: Request):
        """Store (or clear) a secret credential securely. The value is written owner-only to
        secrets.json (never ui_settings.json / a run snapshot), atomically bound to the current
        endpoint, and exposed only to an authorized child spawn. The response never returns it."""
        body = await json_object(request, "secret payload")
        allowed_body = {
            "key", "value", "expected_revision",
            "expected_settings_revision", "expected_secret_revision",
        }
        unknown = sorted(str(name) for name in body if name not in allowed_body)
        if unknown:
            raise HTTPException(400, "unknown secret payload field(s): " + ", ".join(unknown))
        expected_settings_revision = _expected_named_revision(body, "expected_settings_revision")
        expected_secret_revision = _expected_named_revision(body, "expected_secret_revision")
        legacy_secret_revision = _expected_revision(body)
        key = body.get("key")
        if key not in _SECRET_API_FIELDS:
            raise HTTPException(
                400, f"unknown secret {key!r} (known: {sorted(_SECRET_API_FIELDS)})")
        value = body.get("value")
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, "value must be a string (or null to clear)")
        clean_value = (value or "").strip()
        if clean_value and (
                expected_settings_revision is None or expected_secret_revision is None):
            raise HTTPException(428, detail={
                "code": "credential_revision_precondition_required",
                "message": (
                    "Saving a credential requires both the Settings and credential revisions "
                    "from the latest Settings snapshot."),
                "required": ["expected_settings_revision", "expected_secret_revision"],
            })
        # The old one-token contract remains accepted only for clear/backcompat. A nonempty write
        # must never bind a key to an endpoint snapshot the caller did not prove it reviewed.
        if not clean_value and expected_secret_revision is None:
            expected_secret_revision = legacy_secret_revision

        def _apply_secret() -> dict:
            # Fixed UI -> secret -> launch order matches PUT /settings. Endpoint, both CAS tokens,
            # pair publication and returned status are one logical transaction, and the write cannot
            # cross a child process's credential-snapshot -> Popen boundary.
            with store.settings_write_transaction():
                settings_revision = store.ui_settings_revision()
                secret_revision = store.secret_revision()
                if (expected_settings_revision is not None
                        and expected_settings_revision != settings_revision):
                    raise _revision_conflict(
                        "settings", expected_settings_revision, settings_revision)
                if (expected_secret_revision is not None
                        and expected_secret_revision != secret_revision):
                    raise _revision_conflict("secret", expected_secret_revision, secret_revision)
                endpoint = None
                if clean_value:
                    from looplab.core.llm import normalize_llm_base_url
                    try:
                        endpoint = normalize_llm_base_url(
                            store.resolve_settings(store.load_ui_settings()).llm_base_url)
                    except Exception as exc:  # noqa: BLE001 - do not reflect URL parser details
                        raise HTTPException(
                            422, "current LLM endpoint is not valid for credential binding") from exc
                new_secret_revision = store._store_secret_locked(
                    key, clean_value, binding=endpoint)
                _settings, credential = store.settings_and_credential(store.load_ui_settings())
                return {
                    "ok": True,
                    "key": key,
                    "set": bool(clean_value),
                    "settings_revision": settings_revision,
                    "secret_revision": new_secret_revision,
                    "credential": credential,
                }

        return await anyio.to_thread.run_sync(_apply_secret)

    # ------------------------------------------------------------------ task catalogue
    @router.get("/api/tasks")
    def list_tasks():
        """Discover runnable task JSON files (the `examples/` catalogue by default, plus any in the
        run-root) so the launch dialog can offer a pick-list instead of a raw path."""
        repo = Path(__file__).resolve().parents[3]   # routers/ is one level deeper than server.py was
        dirs = [repo / "examples", srv.root]
        env_dir = os.environ.get("LOOPLAB_TASKS_DIR")
        if env_dir:
            dirs.insert(0, Path(env_dir))
        seen, out = set(), []
        for d in dirs:
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict) or not ("goal" in data or "id" in data):
                    continue
                out.append({"path": rp, "name": p.name,
                            "kind": data.get("kind", "quadratic"),
                            "id": data.get("id"), "goal": data.get("goal", ""),
                            "direction": data.get("direction")})
        return {"tasks": out}

    # Late-bind the catalogue for the genesis boss (it grounds its plan on the task list), breaking
    # the route-calls-route dependency between this router and `routers/genesis.py`.
    srv.list_tasks_fn = list_tasks

    @router.get("/api/health")
    def health():
        """P1-3 zero-model liveness: the ONE /api/ route that stays open without a UI token, so a
        monitor can probe process reachability WITHOUT the X-LoopLab-Token AND without triggering a
        billable model completion (that is /api/llm/health, which stays token-gated under deny-default).
        Pure process-liveness — never touches the LLM, a run, or any sensitive state."""
        return {"ok": True, "service": "looplab"}

    def _llm_health_revisions() -> tuple[str, str]:
        # The fixed lock order matches GET /api/settings. Never hold either lock across provider I/O.
        with store.ui_settings_transaction(), store.secret_transaction():
            return store.ui_settings_revision(), store.secret_revision()

    def _llm_health_effective_identity(settings: Settings, timeout_s: float) -> str:
        """Process-keyed identity of the actual probe inputs, including URL/key, without a verifier."""
        from looplab.core.llm import (
            bound_api_key_for, client_kwargs_for, resolve_llm_target,
        )
        target = resolve_llm_target(settings)
        target_kwargs = client_kwargs_for(target, timeout=timeout_s)
        secret = bound_api_key_for(
            settings, target.base_url, api_key=target_kwargs.get("api_key"),
            api_key_base_url=target_kwargs.get("api_key_base_url"))
        header_timeout = min(float(getattr(settings, "llm_header_timeout", timeout_s)), timeout_s)
        canonical = json.dumps({
            "model": target.model,
            "base_url": str(target.base_url).rstrip("/"),
            "api_key": secret,
            "api_key_base_url": target_kwargs.get("api_key_base_url"),
            "temperature": target.temperature,
            "trust_env": bool(getattr(settings, "llm_trust_env", False)),
            "header_timeout": header_timeout,
            "wall_timeout": timeout_s,
            "stream": False,
            "reasoning": False,
            "cache": False,
            "max_tokens": _LLM_HEALTH_MAX_TOKENS,
        }, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
        return "probe-v1:" + hmac.new(
            llm_health_identity_key, canonical, hashlib.sha256).hexdigest()

    def _llm_health_prune_locked(now: float) -> None:
        for operation_id, entry in list(llm_health_operations.items()):
            completed_at = entry.get("completed_at")
            if completed_at is not None and now - completed_at >= _LLM_HEALTH_OPERATION_TTL_S:
                llm_health_operations.pop(operation_id, None)

    def _llm_health_reserve(body: LLMHealthRequest) -> tuple[str, dict[str, Any] | None]:
        identity = (body.expected_settings_revision, body.expected_secret_revision)
        now = time.monotonic()
        with llm_health_operations_lock:
            _llm_health_prune_locked(now)
            existing = llm_health_operations.get(body.operation_id)
            if existing is not None:
                llm_health_operations.move_to_end(body.operation_id)
                return (("replay", existing) if existing["identity"] == identity
                        else ("conflict", existing))
            # Reconciliation is observation-only. After TTL eviction or a process restart, absence
            # must stay absence instead of silently turning the old paid UUID into a new provider call.
            if body.replay_only:
                return "missing", None
            # One app issues at most one paid probe at a time, even when two tabs mint different UUIDs.
            if any(entry.get("completed_at") is None for entry in llm_health_operations.values()):
                return "busy", None
            while len(llm_health_operations) >= _LLM_HEALTH_OPERATION_MAX:
                victim = next((key for key, entry in llm_health_operations.items()
                               if entry.get("completed_at") is not None), None)
                if victim is None:
                    return "capacity", None
                llm_health_operations.pop(victim, None)
            entry = {
                "identity": identity,
                "done": threading.Event(),
                "outcome": None,
                "completed_at": None,
            }
            llm_health_operations[body.operation_id] = entry
            return "leader", entry

    def _llm_health_finish(operation_id: str, entry: dict[str, Any],
                           outcome: tuple[int, dict[str, Any]]) -> None:
        # `outcome` is already the public, allow-listed envelope. Keep no exception, Settings, client,
        # provider text, URL, key, or response object reachable from the replay registry.
        public_outcome = (int(outcome[0]), dict(outcome[1]))
        with llm_health_operations_lock:
            entry["outcome"] = public_outcome
            entry["completed_at"] = time.monotonic()
            if llm_health_operations.get(operation_id) is entry:
                llm_health_operations.move_to_end(operation_id)
            entry["done"].set()

    def _llm_health_render(entry: dict[str, Any]):
        entry["done"].wait()
        outcome = entry.get("outcome")
        if not isinstance(outcome, tuple) or len(outcome) != 2:
            raise HTTPException(503, detail={
                "code": "llm_health_outcome_unavailable",
                "message": "The provider-check outcome is unavailable.",
            })
        status_code, payload = outcome
        if status_code != 200:
            raise HTTPException(status_code=status_code, detail=dict(payload))
        return dict(payload)

    def _llm_health_base(body: LLMHealthRequest, revisions: tuple[str, str], *,
                         provider_attempted: bool, effective_identity: str) -> dict:
        return {
            "operation_id": body.operation_id,
            "settings_revision": revisions[0],
            "secret_revision": revisions[1],
            "provider_attempted": provider_attempted,
            "effective_identity": effective_identity,
        }

    def _llm_health_changed(body: LLMHealthRequest, revisions: tuple[str, str], *,
                            provider_attempted: bool, change_scope: str,
                            effective_identity: str | None = None,
                            current_effective_identity: str | None = None) -> tuple[int, dict]:
        after = provider_attempted
        detail = {
            "code": ("llm_configuration_changed_after_attempt" if after
                     else "llm_configuration_changed"),
            "message": ("LLM configuration changed while the provider attempt was in flight; "
                        "the paid outcome is ambiguous." if after else
                        "LLM configuration changed before the provider attempt."),
            "remediation": ("Do not start a new probe automatically; inspect the saved configuration "
                            "and explicitly choose whether to try again." if after else
                            "Reload saved Settings and start a new provider check."),
            "operation_id": body.operation_id,
            "provider_attempted": provider_attempted,
            "ambiguous": after,
            "outcome_unknown": after,
            "retryable": False,
            "change_scope": change_scope,
            "expected_settings_revision": body.expected_settings_revision,
            "expected_secret_revision": body.expected_secret_revision,
            "settings_revision": revisions[0],
            "secret_revision": revisions[1],
        }
        if effective_identity is not None:
            detail["effective_identity"] = effective_identity
        if current_effective_identity is not None:
            detail["current_effective_identity"] = current_effective_identity
        return 409, detail

    def _llm_health_unverifiable(body: LLMHealthRequest, *,
                                 provider_attempted: bool) -> tuple[int, dict]:
        return (409 if provider_attempted else 503), {
            "code": ("llm_health_outcome_unverifiable_after_attempt" if provider_attempted
                     else "llm_health_precondition_unavailable"),
            "message": ("The provider attempt may have completed, but its configuration fence "
                        "could not be verified." if provider_attempted else
                        "The saved LLM configuration fence is temporarily unavailable."),
            "remediation": ("Treat this operation as terminal and ambiguous; do not start another "
                            "probe automatically." if provider_attempted else
                            "Retry later with a new operation ID after settings storage is available."),
            "operation_id": body.operation_id,
            "provider_attempted": provider_attempted,
            "ambiguous": provider_attempted,
            "outcome_unknown": provider_attempted,
            "retryable": False,
            "expected_settings_revision": body.expected_settings_revision,
            "expected_secret_revision": body.expected_secret_revision,
        }

    def _llm_health_definitive_rejection(_exc: Exception, failure: dict) -> bool:
        # Only classifications whose semantics prove that the provider declined the request may
        # release the recovery fence. A generic/provider-specific 4xx (including proxy 4xx) can be
        # emitted after upstream work started, so its billing/outcome must remain unknown.
        return failure.get("error_kind") in {"credentials", "rate_limit"}

    def _run_llm_health(body: LLMHealthRequest,
                        attempt_state: dict[str, bool]) -> tuple[int, dict[str, Any]]:
        expected = (body.expected_settings_revision, body.expected_secret_revision)

        def _preflight_failure(exc: Exception,
                               effective_identity: str | None = None) -> tuple[int, dict[str, Any]]:
            try:
                current = _llm_health_revisions()
            except Exception:  # noqa: BLE001
                return _llm_health_unverifiable(body, provider_attempted=False)
            if current != expected:
                return _llm_health_changed(
                    body, current, provider_attempted=False, change_scope="saved",
                    effective_identity=effective_identity)
            if effective_identity is None:
                return _llm_health_unverifiable(body, provider_attempted=False)
            return 200, {
                "ok": False,
                **safe_provider_failure(exc),
                **_llm_health_base(
                    body, current, provider_attempted=False,
                    effective_identity=effective_identity),
                "outcome_unknown": False,
            }

        try:
            revisions = _llm_health_revisions()
        except Exception:  # noqa: BLE001 -- never persist/reflect lock or filesystem detail
            return _llm_health_unverifiable(body, provider_attempted=False)
        if revisions != expected:
            return _llm_health_changed(
                body, revisions, provider_attempted=False, change_scope="saved")

        try:
            timeout_s = float(os.environ.get("LOOPLAB_HEALTHCHECK_TIMEOUT", "10.0"))
            if (not math.isfinite(timeout_s)
                    or not _LLM_HEALTH_TIMEOUT_MIN_S <= timeout_s <= _LLM_HEALTH_TIMEOUT_MAX_S):
                raise ValueError("health-check timeout is outside the supported range")
            settings = srv.llm_settings()
            effective_identity = _llm_health_effective_identity(settings, timeout_s)
        except Exception as exc:  # noqa: BLE001
            return _preflight_failure(exc)

        # Preserve the settings-load CAS boundary before constructing any client object.
        try:
            revisions = _llm_health_revisions()
        except Exception:  # noqa: BLE001
            return _llm_health_unverifiable(body, provider_attempted=False)
        if revisions != expected:
            return _llm_health_changed(
                body, revisions, provider_attempted=False, change_scope="saved",
                effective_identity=effective_identity)

        try:
            # Re-resolve ambient env/.env before construction. The HMAC is process-keyed, so it
            # detects key/URL drift without exposing or storing either value.
            current_settings = srv.llm_settings()
            current_effective_identity = _llm_health_effective_identity(current_settings, timeout_s)
        except Exception as exc:  # noqa: BLE001
            return _preflight_failure(exc, effective_identity)

        try:
            revisions = _llm_health_revisions()
        except Exception:  # noqa: BLE001
            return _llm_health_unverifiable(body, provider_attempted=False)
        if revisions != expected:
            return _llm_health_changed(
                body, revisions, provider_attempted=False, change_scope="saved",
                effective_identity=effective_identity)

        if current_effective_identity != effective_identity:
            return _llm_health_changed(
                body, revisions, provider_attempted=False, change_scope="effective",
                effective_identity=effective_identity,
                current_effective_identity=current_effective_identity)

        try:
            from looplab.core.llm import make_llm_client_for

            def _probe_factory(current_settings, **target_kwargs):
                return srv.make_llm_client(
                    current_settings, **target_kwargs, max_retries=0, stream=False,
                    disable_reasoning=True, wall_timeout=timeout_s, cache=False)

            client = make_llm_client_for(
                settings, timeout=timeout_s, factory=_probe_factory)
        except Exception as exc:  # noqa: BLE001
            return _preflight_failure(exc, effective_identity)

        # This is deliberately the final operation before entering the one-call client method.
        try:
            revisions = _llm_health_revisions()
        except Exception:  # noqa: BLE001
            return _llm_health_unverifiable(body, provider_attempted=False)
        if revisions != expected:
            return _llm_health_changed(
                body, revisions, provider_attempted=False, change_scope="saved",
                effective_identity=effective_identity)

        provider_error: Exception | None = None
        attempt_state["provider_attempted"] = True
        try:
            client.probe(
                [{"role": "user", "content": "Reply with one word: ready"}],
                max_tokens=_LLM_HEALTH_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001
            provider_error = exc

        # A paid attempt is now possible. Any failure to prove the postcondition is terminal and
        # ambiguous; it must never be downgraded to an ordinary retryable provider failure.
        try:
            revisions = _llm_health_revisions()
        except Exception:  # noqa: BLE001
            return _llm_health_unverifiable(body, provider_attempted=True)
        if revisions != expected:
            return _llm_health_changed(
                body, revisions, provider_attempted=True, change_scope="saved",
                effective_identity=effective_identity)
        try:
            current_settings = srv.llm_settings()
            current_effective_identity = _llm_health_effective_identity(current_settings, timeout_s)
            confirmed_revisions = _llm_health_revisions()
        except Exception:  # noqa: BLE001
            return _llm_health_unverifiable(body, provider_attempted=True)
        if confirmed_revisions != expected:
            return _llm_health_changed(
                body, confirmed_revisions, provider_attempted=True, change_scope="saved",
                effective_identity=effective_identity)
        if current_effective_identity != effective_identity:
            return _llm_health_changed(
                body, confirmed_revisions, provider_attempted=True, change_scope="effective",
                effective_identity=effective_identity,
                current_effective_identity=current_effective_identity)

        base = _llm_health_base(
            body, confirmed_revisions, provider_attempted=True,
            effective_identity=effective_identity)
        if provider_error is not None:
            failure = safe_provider_failure(provider_error)
            # Only an explicit auth/rate-limit rejection proves the provider declined the request.
            # A 5xx, empty/no-choices HTTP 200, timeout, connection loss, or generic post-send error
            # may already have generated/billed work even though no usable response reached us.
            outcome_unknown = not _llm_health_definitive_rejection(provider_error, failure)
            if outcome_unknown:
                message = ("The provider-check outcome is unresolved; the request may have "
                           "completed or been billed.")
                failure = {**failure,
                           "code": "llm_health_provider_outcome_unknown",
                           "error": message,
                           "message": message,
                           "remediation": "Do not start another probe automatically.",
                           "retryable": False}
            return 200, {"ok": False, **failure, **base,
                         "outcome_unknown": outcome_unknown}
        return 200, {"ok": True, **base, "outcome_unknown": False}

    @router.post("/api/llm/health")
    def llm_health(body: LLMHealthRequest):
        """One revision-fenced, idempotent and output-capped provider reachability mutation."""
        reservation, entry = _llm_health_reserve(body)
        if reservation == "conflict":
            raise HTTPException(409, detail={
                "code": "llm_health_operation_conflict",
                "message": "This operation ID belongs to a different saved-configuration identity.",
                "remediation": "Use a new UUIDv4 for a different saved Settings snapshot.",
                "operation_id": body.operation_id,
                "expected_settings_revision": body.expected_settings_revision,
                "expected_secret_revision": body.expected_secret_revision,
                "provider_attempted": False,
                "outcome_unknown": False,
            })
        if reservation == "busy":
            raise HTTPException(409, detail={
                "code": "llm_health_in_progress",
                "message": "Another provider check is already in progress.",
                "remediation": "Wait for the active check to finish before starting another one.",
                "operation_id": body.operation_id,
                "expected_settings_revision": body.expected_settings_revision,
                "expected_secret_revision": body.expected_secret_revision,
                "provider_attempted": False,
                "outcome_unknown": False,
            })
        if reservation == "missing":
            raise HTTPException(410, detail={
                "code": "llm_health_replay_unavailable",
                "message": ("The prior provider-check result is no longer available; replay-only "
                            "reconciliation made no new provider call."),
                "remediation": ("Treat the prior outcome as unresolved. Start a new provider check "
                                "only through an explicit new user action and UUIDv4."),
                "operation_id": body.operation_id,
                "expected_settings_revision": body.expected_settings_revision,
                "expected_secret_revision": body.expected_secret_revision,
                "provider_attempted": False,
                "outcome_unknown": True,
                "ambiguous": True,
                "retryable": False,
                "replay_only": True,
            })
        if reservation == "capacity":
            raise HTTPException(503, detail={
                "code": "llm_health_capacity",
                "message": "Provider-check replay capacity is temporarily unavailable.",
                "operation_id": body.operation_id,
                "expected_settings_revision": body.expected_settings_revision,
                "expected_secret_revision": body.expected_secret_revision,
                "provider_attempted": False,
                "outcome_unknown": False,
            })
        assert entry is not None
        if reservation == "replay":
            return _llm_health_render(entry)

        attempt_state = {"provider_attempted": False}
        try:
            outcome = _run_llm_health(body, attempt_state)
        except Exception:  # noqa: BLE001 -- terminalize the flight; never strand duplicate waiters
            outcome = _llm_health_unverifiable(
                body, provider_attempted=attempt_state["provider_attempted"])
        _llm_health_finish(body.operation_id, entry, outcome)
        return _llm_health_render(entry)

    # ------------------------------------------------------------------ GPU monitor
    @router.get("/api/gpu")
    def gpu():
        try:
            from looplab.core.hardware import query_nvidia_smi
            from looplab.core.parse import to_float as _f
            rows = query_nvidia_smi(
                "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                timeout=4)
            if rows is None:
                return {"available": False}
            gpus = []
            for p in rows:
                if len(p) >= 6:
                    gpus.append({"name": p[0], "util": _f(p[1]), "mem_used": _f(p[2]),
                                 "mem_total": _f(p[3]), "temp": _f(p[4]), "power": _f(p[5])})
            return {"available": True, "gpus": gpus}
        except Exception:  # noqa: BLE001 - no GPU / no nvidia-smi -> soft fail
            return {"available": False}

    @router.get("/api/memory")
    def memory():
        # Cross-run memory dir holds several tiers in separate .jsonl files — SPLIT them by filename so
        # the UI can show cases / lessons / notes each with their own shape. MUST be declared BEFORE the
        # `/api/{kind}` catch-all below, else it's swallowed as an unknown kind (→ 404, the reason the
        # Memory panel was silently empty). `cases` stays populated for back-compat.
        s = srv.global_settings()
        out = {"dir": None, "cases": [], "lessons": [], "notes": []}
        if not s.memory_dir:
            return out
        md = Path(s.memory_dir)
        out["dir"] = str(md)
        receipts = {}
        # allow-list only the three UI tiers. Every tier gets an independent bounded
        # recent source window and result cap; governance/capsule ledgers are not accidental "cases".
        for tier, filename in (("cases", "cases.jsonl"), ("lessons", "lessons.jsonl"),
                               ("notes", "meta_notes.jsonl")):
            out[tier], receipts[tier] = _read_memory_tier(md / filename, tier)
        out["projection"] = "bounded_recent_tail"
        out["page"] = {"tiers": receipts,
                       "truncated": any(row["source_window_truncated"] for row in receipts.values()),
                       "unavailable": any(row["unavailable"] for row in receipts.values()),
                       "partial": any(row["source_window_truncated"] or row["skipped"]
                                      or row["unavailable"] for row in receipts.values())}
        return out

    # ------------------------------------------------------------------ authoring (files-as-truth)
    def _author_dir(kind: str) -> Optional[Path]:
        return _current_author_directory(srv, kind)

    @router.get("/api/{kind}")
    def list_author(kind: str, response: Response):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        # The returned digest is a write precondition, never a cacheable display hint.
        response.headers["Cache-Control"] = "private, no-store"
        d = _author_dir(kind)
        if d is None:
            return {"dir": None, "target_root_id": None, "files": []}
        try:
            root = _configured_author_root(d, kind)
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc) from exc
        target_root_id = _author_target_root_id(root)
        if not root.exists():
            return {"dir": str(d), "target_root_id": target_root_id, "files": []}
        if not root.is_dir():
            raise HTTPException(409, f"configured {kind} path is not a directory")
        # Bounded: `knowledge_dir` is AGENT-WRITABLE (KnowledgeWriteTools.remember), so an unbounded
        # "read every *.md whole into one response" is a self-inflicted OOM the engine itself can grow.
        # Cap the file count and each file's bytes, and disclose truncation instead of silently lying
        # about completeness. A symlinked entry is skipped for the same reason /log refuses one.
        files = []
        names = sorted(root.glob("*.md"))
        for p in names[:_AUTHOR_MAX_FILES]:
            if not _valid_author_name(p.name) or p.is_symlink() or not p.is_file():
                continue
            # knowledge_dir is AGENT-WRITABLE (see above), so a file can be deleted or renamed
            # between the glob and this open. Skip the one that vanished rather than 500-ing the
            # whole listing over a race that is normal here.
            try:
                with open(p, "rb") as fh:
                    head = fh.read(_AUTHOR_MAX_BYTES + 1)
            except OSError:
                continue
            text = head[:_AUTHOR_MAX_BYTES].decode("utf-8", errors="replace")
            truncated = len(head) > _AUTHOR_MAX_BYTES
            files.append({"name": p.name, "text": text, "truncated": truncated,
                          # A truncated prefix cannot safely authorize replacement of bytes the
                          # editor never displayed. Such rows intentionally have no writable CAS
                          # token; operation PUTs reject every invented token against "oversized".
                          "revision": None if truncated else _author_revision(head)})
        return {"dir": str(d), "target_root_id": target_root_id, "files": files,
                "truncated_files": max(0, len(names) - len(files))}

    @router.get(
        "/api/{kind}/{name}/operations/{operation_id}",
        response_model=AuthoringOperationResponse,
        responses={404: {"description": "No receipt exists for that exact operation."}},
    )
    def get_author_operation(kind: str, name: str, operation_id: str,
                             expected_target_root_id: str, expected_revision: str,
                             desired_revision: str, response: Response):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        if not _valid_author_name(name):
            raise HTTPException(400, "bad name (expected a plain <file>.md)")
        if _AUTHOR_OPERATION_RE.fullmatch(operation_id) is None:
            raise HTTPException(400, "operation_id must be a lowercase UUIDv4")
        if _AUTHOR_TARGET_ROOT_ID_RE.fullmatch(expected_target_root_id) is None:
            raise HTTPException(400, "expected_target_root_id is invalid")
        if _AUTHOR_REVISION_RE.fullmatch(expected_revision) is None:
            raise HTTPException(400, "expected_revision is invalid")
        if (_AUTHOR_REVISION_RE.fullmatch(desired_revision) is None
                or desired_revision == _AUTHOR_MISSING_REVISION):
            raise HTTPException(400, "desired_revision is invalid")
        # Observation is deliberately read-only: it neither creates the receipt directory/lock nor
        # advances a prepared write. The exact PUT is the sole recovery transition.
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return _lookup_author_operation(
                srv, kind=kind, name=name, operation_id=operation_id,
                expected_target_root_id=expected_target_root_id,
                expected_revision=expected_revision, desired_revision=desired_revision)
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc, operation_id) from exc

    @router.put(
        "/api/{kind}/{name}/operations/{operation_id}",
        response_model=AuthoringOperationResponse,
        responses={409: {"description": "Operation identity or target conflict."}},
    )
    async def write_author_operation(kind: str, name: str, operation_id: str,
                                     body: AuthoringOperationRequest, response: Response):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        if not _valid_author_name(name):
            raise HTTPException(400, "bad name (expected a plain <file>.md)")
        if _AUTHOR_OPERATION_RE.fullmatch(operation_id) is None:
            raise HTTPException(400, "operation_id must be a lowercase UUIDv4")
        try:
            text_bytes = body.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(400, "text must be valid UTF-8") from exc
        if len(text_bytes) > _AUTHOR_MAX_BYTES:
            raise HTTPException(
                400, f"file too large: {len(text_bytes)}b > {_AUTHOR_MAX_BYTES}b")
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return await anyio.to_thread.run_sync(lambda: _run_author_operation(
                srv, kind=kind, name=name, operation_id=operation_id,
                text_bytes=text_bytes, expected_revision=body.expected_revision,
                expected_target_root_id=body.expected_target_root_id))
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc, operation_id) from exc

    @router.put("/api/{kind}/{name}")
    async def write_author(kind: str, name: str, request: Request):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        d = _author_dir(kind)
        if d is None:
            raise HTTPException(400, f"no {kind} dir configured (set LOOPLAB_{kind.upper()}_DIR)")
        # Name allow-list BEFORE mkdir: the traversal guard below only confines the DIRECTORY, so a
        # bare `.env` / `x.py` would land in prompt_dir/skills_dir/knowledge_dir — write-only-invisible,
        # since `list_author` globs `*.md` and would never show it again. These dirs are hot-reloaded
        # into agent context, so only the authored markdown surface belongs here.
        if not _valid_author_name(name):
            raise HTTPException(400, "bad name (expected a plain <file>.md)")
        # BOUNDED BY THE READ CAP, not the server-wide 2 MB body cap: `list_author` only ever shows
        # the first `_AUTHOR_MAX_BYTES`, so a larger PUT was persisted whole and then displayed
        # truncated forever — while the oversized file still got hot-reloaded into agent context in
        # full. And an undecodable body raised UnicodeDecodeError into an unhandled 500; a client
        # sending non-UTF-8 made a bad REQUEST, so say so.
        body = await request.body()
        if len(body) > _AUTHOR_MAX_BYTES:
            raise HTTPException(400, f"file too large: {len(body)}b > {_AUTHOR_MAX_BYTES}b "
                                     "(the editor only displays the first that many bytes)")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, f"file must be UTF-8 text: {exc}") from exc
        # Compatibility only. New clients use the operation route above; this branch has no
        # caller-supplied source revision and therefore remains explicitly last-writer-wins. It is
        # nevertheless atomic and shares the operation lock, so it cannot split a CAS transaction.
        try:
            return await anyio.to_thread.run_sync(lambda: _run_legacy_author_write(
                srv, kind=kind, name=name, text=text))
        except _AuthoringFailure as exc:
            raise _authoring_http_failure(exc) from exc

    return router
