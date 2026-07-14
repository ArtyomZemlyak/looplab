"""Durable, idempotent lifecycle for run control commands.

The legacy ``/control`` endpoint appends an intent and leaves every caller to guess whether an
engine must be woken and whether the requested effect happened.  ``RunCommandService`` is the one
authoritative funnel for command-aware clients: it normalizes the same control payloads as the
legacy route, persists a per-run command record, appends at most one marked intent, applies the
command's engine policy, and records an observable postcondition.

Records deliberately contain only a SHA-256 digest of ``Idempotency-Key``.  One atomic JSON file per
command avoids a shared read/modify/write index and survives UI/server restarts.  The event carries
``_command_id`` so recovery can prove an intent was appended before retrying it.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from looplab.core.atomicio import atomic_write_text
from looplab.core.models import Event, Idea
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.comment_projection import (
    COMMENT_ID_RE, COMMENT_MAX_PER_NODE_GENERATION, COMMENT_MAX_PER_RUN, COMMENT_MAX_VERSION,
    normalize_comment_text)
from looplab.events.eventstore import (
    EventStore, EventStoreConcurrencyError, EventStoreLockError, iter_jsonl)
from looplab.events.replay import fold
from looplab.events.types import (
    EV_ANNOTATION, EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_DEEP_RESEARCH,
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT, EV_HYPOTHESIS_ADDED,
    EV_HYPOTHESIS_UPDATED, EV_INJECT_NODE, EV_NODE_ABORT, EV_NODE_RESET, EV_PAUSE, EV_PROMOTE,
    EV_RESUME, EV_RUN_ABORT, EV_RUN_REOPENED, EV_SET_STRATEGY, EV_SPEC_APPROVED)
from looplab.serve.command_observation import CommandObservation, CommandObservationIndex
from looplab.serve.engine_proc import _engine_alive, _engine_liveness, _spawn_engine
from looplab.serve.protocol import COLLABORATION_EVENTS, CONTROL_EVENTS
from looplab.trust.redact import redact_secrets


class EnginePolicy(str, Enum):
    NO_SPAWN = "no_spawn"
    ENSURE_RUNNING = "ensure_running"
    ENSURE_DRIVER_PRESERVE_STOP = "ensure_driver_preserve_stop"


@dataclass(frozen=True)
class ControlSpec:
    event_type: str
    engine_policy: EnginePolicy
    postcondition: str


def _spec(event_type: str, policy: EnginePolicy, postcondition: str) -> ControlSpec:
    return ControlSpec(event_type, policy, postcondition)


# The single policy registry for every appendable control event.  Keep the equality assertion: a new
# CONTROL_EVENTS member must make an explicit engine/postcondition choice instead of silently falling
# into an unsafe default.
CONTROL_SPECS: dict[str, ControlSpec] = {
    EV_RUN_ABORT: _spec(EV_RUN_ABORT, EnginePolicy.ENSURE_DRIVER_PRESERVE_STOP, "finished_and_stopped"),
    EV_PAUSE: _spec(EV_PAUSE, EnginePolicy.NO_SPAWN, "paused_and_stopped"),
    EV_RESUME: _spec(EV_RESUME, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_RUN_REOPENED: _spec(EV_RUN_REOPENED, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_NODE_ABORT: _spec(EV_NODE_ABORT, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_NODE_RESET: _spec(EV_NODE_RESET, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_BUDGET_EXTEND: _spec(EV_BUDGET_EXTEND, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_HINT: _spec(EV_HINT, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_SET_STRATEGY: _spec(EV_SET_STRATEGY, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_FORCE_CONFIRM: _spec(EV_FORCE_CONFIRM, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_FORCE_ABLATE: _spec(EV_FORCE_ABLATE, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_FORK: _spec(EV_FORK, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_INJECT_NODE: _spec(EV_INJECT_NODE, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_DEEP_RESEARCH: _spec(EV_DEEP_RESEARCH, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_APPROVAL_GRANTED: _spec(EV_APPROVAL_GRANTED, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_SPEC_APPROVED: _spec(EV_SPEC_APPROVED, EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_ANNOTATION: _spec(EV_ANNOTATION, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_COMMENT_CREATED: _spec(EV_COMMENT_CREATED, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_COMMENT_EDITED: _spec(EV_COMMENT_EDITED, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_COMMENT_RESOLUTION_CHANGED: _spec(
        EV_COMMENT_RESOLUTION_CHANGED, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_PROMOTE: _spec(EV_PROMOTE, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_HYPOTHESIS_ADDED: _spec(EV_HYPOTHESIS_ADDED, EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_HYPOTHESIS_UPDATED: _spec(EV_HYPOTHESIS_UPDATED, EnginePolicy.NO_SPAWN, "folded_intent"),
}
assert set(CONTROL_SPECS) == set(CONTROL_EVENTS), "every control event needs an explicit ControlSpec"

# HTTP control payloads are strict contracts, not arbitrary event bags. Unknown keys are dangerous:
# replay ignores many of them, so a caller could persist `{secret: ...}` and receive false success.
CONTROL_DATA_FIELDS: dict[str, frozenset[str]] = {
    EV_RUN_ABORT: frozenset({"reason"}),
    EV_PAUSE: frozenset(),
    EV_RESUME: frozenset(),
    EV_RUN_REOPENED: frozenset(),
    EV_NODE_ABORT: frozenset({"node_id", "generation", "reason"}),
    EV_NODE_RESET: frozenset({"node_id", "generation", "from_stage"}),
    EV_BUDGET_EXTEND: frozenset(
        {"add_nodes", "max_seconds", "max_eval_seconds", "timeout", "max_parallel"}),
    EV_HINT: frozenset({"text", "replace"}),
    EV_SET_STRATEGY: frozenset({"strategy"}),
    EV_FORCE_CONFIRM: frozenset({"node_id", "generation"}),
    EV_FORCE_ABLATE: frozenset({"node_id", "generation"}),
    EV_FORK: frozenset({"from_node_id", "generation"}),
    EV_INJECT_NODE: frozenset({
        "idea", "parent_id", "parent_ids", "parent_generations", "code", "files", "deleted", "origin",
        "source_run", "source_node"}),
    EV_DEEP_RESEARCH: frozenset(),
    EV_APPROVAL_GRANTED: frozenset({"node_id", "generation"}),
    EV_SPEC_APPROVED: frozenset(),
    EV_ANNOTATION: frozenset({"node_id", "text"}),
    EV_COMMENT_CREATED: frozenset({"node_id", "node_generation", "text"}),
    EV_COMMENT_EDITED: frozenset(
        {"comment_id", "node_id", "node_generation", "expected_version", "text"}),
    EV_COMMENT_RESOLUTION_CHANGED: frozenset(
        {"comment_id", "node_id", "node_generation", "expected_version", "resolved"}),
    EV_PROMOTE: frozenset({"node_id", "generation", "alias"}),
    EV_HYPOTHESIS_ADDED: frozenset({"id", "statement", "source"}),
    EV_HYPOTHESIS_UPDATED: frozenset({"id", "status"}),
}
assert set(CONTROL_DATA_FIELDS) == set(CONTROL_SPECS), "every control event needs a data allowlist"

_LIFECYCLE_CONTROL_TARGETS = {
    EV_NODE_ABORT: "node_id",
    EV_NODE_RESET: "node_id",
    EV_APPROVAL_GRANTED: "node_id",
    EV_FORCE_CONFIRM: "node_id",
    EV_FORCE_ABLATE: "node_id",
    EV_FORK: "from_node_id",
    EV_PROMOTE: "node_id",
}
_ABSENT = object()


TERMINAL_STATUSES = frozenset({"succeeded", "noop", "failed", "rejected", "timed_out"})
_RETRY_GUARDED_EVENTS = frozenset(CONTROL_EVENTS)
_COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")
_RUN_GENERATION_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def run_generation_token(events) -> str:
    """Return the stable lowercase token for one non-empty event-log generation.

    An in-place reset archives the entire log and a replacement engine writes a new first event.
    Basing the token on that durable event keeps it stable as the same run grows, while making the
    old and replacement logs distinct without a mutable sidecar that could drift from events.jsonl.
    Empty/startup logs deliberately have no token: accepting a mutation before there is durable
    generation identity would re-open the exact reset race this precondition closes.
    """
    iterator = iter(events)
    try:
        try:
            first = next(iterator, None)
        except OSError:
            # A concurrent delete/replace or transient filesystem read failure is not a trustworthy
            # generation. Match EventStore.read_all's fail-closed empty-prefix behavior.
            return ""
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    if first is None:
        return ""
    if isinstance(first, dict):
        try:
            first = Event(**first)
        except Exception:  # noqa: BLE001 - match EventStore's fail-closed invalid-record boundary
            return ""
    seq = first.seq
    timestamp = first.ts
    event_type = first.type
    data = first.data or {}
    raw = json.dumps({
        "seq": seq, "ts": timestamp, "type": event_type,
        "run_id": data.get("run_id"),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_expected_generation(value: object) -> str:
    """Normalize a wire generation token without coercing or trimming ambiguous input."""
    if not isinstance(value, str) or _RUN_GENERATION_RE.fullmatch(value) is None:
        raise HTTPException(400, {
            "code": "invalid_run_generation",
            "message": "expected_generation must be an exact 64-character hexadecimal string.",
            "remediation": "Refresh GET /state and submit its generation with this new command.",
        })
    return value.lower()


def _process_alive(pid: Optional[int]) -> Optional[bool]:
    """Return True/False only when process liveness is known; None means fail-closed unknown.

    Spawn leases use this after their observation deadline. A timeout is not evidence that a cold
    detached child died: clearing its lease could launch a second engine before the first imports
    enough code to expose ``engine.lock``. ``psutil`` gives the best zombie handling when installed;
    ``kill(pid, 0)`` is the dependency-free fallback. Permission/platform ambiguity stays unknown.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        import psutil  # optional proc extra
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
            return bool(proc.is_running())
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, psutil.Error):
            return None
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as exc:
        # Windows reports a missing PID as ERROR_INVALID_PARAMETER (87); POSIX uses ESRCH.
        if exc.errno == errno.ESRCH or getattr(exc, "winerror", None) == 87:
            return False
        return None


def _lock_identity(path: Path) -> str:
    """Conservative per-run lock identity on common case-insensitive desktop filesystems."""
    identity = str(path.resolve())
    if os.name == "nt":
        return os.path.normcase(identity)
    if sys.platform == "darwin":
        # Default APFS/HFS+ is case-insensitive and normalization-insensitive. A case-sensitive macOS
        # volume is safely over-serialized; it must never get two locks for one default-volume run.
        return unicodedata.normalize("NFD", identity).casefold()
    return identity


_PROCESS_IDENTITY_SCHEMES = frozenset({"proc-start", "psutil", "windows-filetime"})
_LEGACY_PROCESS_IDENTITY_SCHEME = "<legacy>"


def _process_identity_scheme(identity: object) -> Optional[str]:
    """Return a comparable source scheme, preserving pre-tag legacy identities."""
    if not isinstance(identity, str) or not identity:
        return None
    scheme, separator, token = identity.partition(":")
    if not separator:
        return _LEGACY_PROCESS_IDENTITY_SCHEME
    if scheme in _PROCESS_IDENTITY_SCHEMES and token:
        return scheme
    # Unknown/invalid tags may belong to a newer writer. Treat them as incomparable rather than
    # guessing that two different encodings prove PID reuse.
    return None


def _process_identity_proves_reuse(stored: object, current: object) -> bool:
    """Whether two non-equal identities conclusively describe different PID generations."""
    if not isinstance(stored, str) or not isinstance(current, str):
        return False
    if not stored or not current or stored == current:
        return False
    stored_scheme = _process_identity_scheme(stored)
    current_scheme = _process_identity_scheme(current)
    # Same-source tokens are comparable. This also retains the old behavior when both claims came
    # from pre-tag LoopLab versions. A tagged/legacy or cross-source mismatch is only ambiguity.
    return stored_scheme is not None and stored_scheme == current_scheme


def _process_identity(pid: Optional[int]) -> Optional[str]:
    """Source-tagged creation identity used to distinguish a live child from PID reuse."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        import psutil
        proc = psutil.Process(pid)
        created = proc.create_time()
        # Creation time is the stable PID-generation token. Do not mix cmdline into the hash:
        # transient AccessDenied could make the same live child compare as a recycled PID.
        raw = json.dumps({"pid": pid, "created": created}, sort_keys=True).encode("utf-8")
        return f"psutil:{hashlib.sha256(raw).hexdigest()}"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - absent/exited/inaccessible process => identity unknown
        pass  # native Windows or /proc may still provide the same creation token
    if os.name == "nt":
        # ``looplab[ui]`` deliberately keeps psutil optional.  Windows has no ``/proc`` fallback,
        # but the process creation FILETIME is the same PID-generation token psutil exposes.  Read
        # it directly so a restarted UI can distinguish its dead worker from a recycled live PID
        # instead of quarantining the run forever merely because the optional ``proc`` extra is
        # absent. AccessDenied remains unknown/fail-closed and has the explicit recovery route below.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                        handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                created_ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                raw = json.dumps(
                    {"pid": pid, "created_filetime": created_ticks}, sort_keys=True
                ).encode("utf-8")
                return f"windows-filetime:{hashlib.sha256(raw).hexdigest()}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    if os.name != "nt":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            start_ticks = stat.rsplit(")", 1)[1].split()[19]
            digest = hashlib.sha256(f"{pid}:{start_ticks}".encode("ascii")).hexdigest()
            return f"proc-start:{digest}"
        except (OSError, IndexError, UnicodeError, ValueError):
            pass
    return None


def task_file_for(rd: Path) -> Optional[str]:
    """Resolve the immutable run snapshot, with a safe existing-file legacy fallback."""
    snapshot = rd / "task.snapshot.json"
    if snapshot.is_file():
        return str(snapshot)
    meta = rd / "ui_meta.json"
    if meta.is_file():
        try:
            row = json.loads(meta.read_text(encoding="utf-8"))
            raw = row.get("task_file") if isinstance(row, dict) else None
            if raw and Path(raw).is_file():
                return str(raw)
        except (OSError, ValueError, TypeError):
            pass
    return None


def _normalize_finalize_data(data: dict) -> dict:
    unknown = set(data) - {"reason"}
    if unknown:
        raise HTTPException(400, f"run_abort has unknown field(s): {', '.join(sorted(unknown))}")
    if "reason" in data and data.get("reason") is None:
        raise HTTPException(400, "run_abort.reason must not be null")
    reason = data.get("reason", "finalized")
    if not isinstance(reason, str):
        raise HTTPException(400, "run_abort.reason must be a string")
    reason = reason.strip()
    if not reason or len(reason) > 256:
        raise HTTPException(400, "run_abort.reason must be non-empty and at most 256 characters")
    return {"reason": reason}


def normalize_control(srv, rd: Path, event_type: str, data) -> dict:
    """Validate/normalize one control payload for both /control and /commands.

    This is the old route's node-reset and cross-run-import logic extracted verbatim enough that the
    compatibility endpoint and command service cannot drift into accepting different commands.
    """
    if event_type not in CONTROL_SPECS:
        raise HTTPException(400, f"unknown control event: {event_type!r}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(400, "control data must be a JSON object")
    data = dict(data)
    unknown = set(data) - CONTROL_DATA_FIELDS[event_type]
    if unknown:
        raise HTTPException(
            400, f"{event_type} has unknown field(s): {', '.join(sorted(unknown))}")

    if event_type == EV_RUN_ABORT:
        data = _normalize_finalize_data(data)

    state = None

    def _state():
        nonlocal state
        if state is None:
            state = srv.state(rd)
        return state

    def _strict_integer(value, name: str) -> int:
        if isinstance(value, bool):
            raise HTTPException(400, f"{name} must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value.strip())
        raise HTTPException(400, f"{name} must be an integer")

    def _integer(name: str, *, required: bool = True) -> Optional[int]:
        value = data.get(name)
        if value is None and not required:
            return None
        return _strict_integer(value, name)

    def _node(name: str, *, required: bool = True) -> Optional[int]:
        value = _integer(name, required=required)
        if value is not None and value not in _state().nodes:
            raise HTTPException(404, f"no node #{value} in this run")
        return value

    def _text(name: str, *, required: bool = True, limit: int = 20_000) -> Optional[str]:
        value = data.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise HTTPException(400, f"{name} must be a string")
        value = value.strip()
        if required and not value:
            raise HTTPException(400, f"{name} must be non-empty")
        if len(value) > limit:
            raise HTTPException(400, f"{name} must be at most {limit} characters")
        return value

    def _comment_id(value: object) -> str:
        if not isinstance(value, str) or COMMENT_ID_RE.fullmatch(value) is None:
            raise HTTPException(400, {
                "code": "invalid_comment_id",
                "message": "comment_id must be cmt_ followed by exactly 32 lowercase hex characters",
                "remediation": "refresh the collaboration panel and retry the exact visible comment",
            })
        return value

    def _comment_text(value: object) -> str:
        try:
            return normalize_comment_text(value)
        except ValueError as exc:
            raise HTTPException(400, {
                "code": "invalid_comment_text",
                "message": str(exc),
                "remediation": "enter non-empty text no larger than 8192 UTF-8 bytes",
            }) from exc

    def _comment_version(value: object) -> int:
        version = _strict_integer(value, "expected_version")
        if version < 1:
            raise HTTPException(400, "expected_version must be positive")
        return version

    if event_type == EV_COMMENT_CREATED:
        current = _state()
        node_id = _node("node_id")
        node_generation = _strict_integer(data.get("node_generation"), "node_generation")
        if node_generation < 0:
            raise HTTPException(400, "node_generation must be non-negative")
        node = current.nodes[node_id]
        if node.attempt != node_generation:
            raise HTTPException(409, {
                "code": "node_generation_changed",
                "message": (f"experiment #{node_id} is generation {node.attempt}, not "
                            f"{node_generation}"),
                "remediation": "refresh the run before commenting on this experiment lifecycle",
            })
        # Count only MODERN comments: legacy EV_ANNOTATION notes cannot be compacted in an append-only
        # log, so counting them here would permanently 409 modern comments on a heavily-annotated run.
        # Mirrors comment_projection.apply_comment_event so validation and fold never diverge.
        modern_count = sum(1 for item in current.comments.values() if not item.legacy)
        if modern_count >= COMMENT_MAX_PER_RUN:
            raise HTTPException(409, {
                "code": "comment_run_limit_reached",
                "message": f"this run already has {COMMENT_MAX_PER_RUN} projected comments",
                "remediation": "archive or compact comment history before creating more",
            })
        per_subject = sum(
            1 for item in current.comments.values()
            if (not item.legacy and item.node_id == node_id
                and item.node_generation == node_generation))
        if per_subject >= COMMENT_MAX_PER_NODE_GENERATION:
            raise HTTPException(409, {
                "code": "comment_subject_limit_reached",
                "message": (f"experiment #{node_id} generation {node_generation} already has "
                            f"{COMMENT_MAX_PER_NODE_GENERATION} comments"),
                "remediation": "resolve or consolidate the existing discussion",
            })
        comment_id = ""
        for _ in range(128):
            candidate = "cmt_" + secrets.token_hex(16)
            if candidate not in current.comments:
                comment_id = candidate
                break
        if not comment_id:
            raise HTTPException(503, {
                "code": "comment_id_unavailable",
                "message": "the server could not allocate a unique comment id",
                "remediation": "retry with the same command idempotency key",
                "retryable": True,
            })
        data = {
            "comment_id": comment_id,
            "node_id": node_id,
            "node_generation": node_generation,
            "text": _comment_text(data.get("text")),
            "actor_kind": ("deployment_owner" if getattr(srv, "owner_auth_enabled", False)
                           else "local_operator"),
            "version": 1,
        }
    elif event_type in {EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED}:
        current = _state()
        raw_comment_id = data.get("comment_id")
        # Legacy annotations are projected under a synthetic lookup-only id. Accept that exact
        # shape solely so the caller gets the intentional read-only result below; it is never
        # admitted into a modern collaboration event (modern ids remain strictly validated).
        if (isinstance(raw_comment_id, str)
                and re.fullmatch(r"legacy_(?:0|[1-9]\d*)", raw_comment_id)):
            comment_id = raw_comment_id
        else:
            comment_id = _comment_id(raw_comment_id)
        comment = current.comments.get(comment_id)
        if comment is None:
            raise HTTPException(404, {
                "code": "comment_not_found",
                "message": "the comment does not exist in this run generation",
                "remediation": "refresh the collaboration panel",
            })
        if not comment.editable or comment.legacy:
            raise HTTPException(409, {
                "code": "legacy_comment_read_only",
                "message": "legacy annotations have no verifiable actor or lifecycle and are read-only",
                "remediation": "create a new attributed comment instead",
            })
        if comment.version >= COMMENT_MAX_VERSION:
            raise HTTPException(409, {
                "code": "comment_version_limit_reached",
                "message": f"comment {comment_id} reached its {COMMENT_MAX_VERSION}-revision limit",
                "remediation": "resolve it and create a concise follow-up comment",
            })
        supplied_node_id = _strict_integer(data.get("node_id"), "node_id")
        supplied_generation = _strict_integer(
            data.get("node_generation"), "node_generation")
        if (supplied_node_id != comment.node_id
                or supplied_generation != comment.node_generation):
            raise HTTPException(409, {
                "code": "comment_subject_changed",
                "message": "the submitted node lifecycle does not own this comment",
                "remediation": "refresh the collaboration panel before editing this comment",
            })
        expected_version = _comment_version(data.get("expected_version"))
        if comment.version != expected_version:
            raise HTTPException(409, {
                "code": "comment_version_changed",
                "message": (f"comment {comment_id} is version {comment.version}, not "
                            f"{expected_version}"),
                "current_version": comment.version,
                "remediation": "refresh the comment and re-apply the edit to its current version",
            })
        normalized = {
            "comment_id": comment_id,
            "node_id": comment.node_id,
            "node_generation": comment.node_generation,
            "base_version": expected_version,
            "version": expected_version + 1,
            "actor_kind": ("deployment_owner" if getattr(srv, "owner_auth_enabled", False)
                           else "local_operator"),
        }
        if event_type == EV_COMMENT_EDITED:
            text = _comment_text(data.get("text"))
            if text == comment.text:
                raise HTTPException(409, {
                    "code": "comment_unchanged",
                    "message": "the edited text is identical to the current comment",
                    "remediation": "no update is needed",
                })
            normalized["text"] = text
        else:
            resolved = data.get("resolved")
            if not isinstance(resolved, bool):
                raise HTTPException(400, "resolved must be a boolean")
            if resolved == comment.resolved:
                raise HTTPException(409, {
                    "code": "comment_resolution_unchanged",
                    "message": "the comment already has that resolution state",
                    "remediation": "refresh the collaboration panel",
                })
            normalized["resolved"] = resolved
        data = normalized

    if event_type == EV_NODE_RESET:
        raw_stage = data.get("from_stage", "eval")
        if not isinstance(raw_stage, str):
            raise HTTPException(400, "from_stage must be a string")
        stage = raw_stage.strip()
        if not stage or len(stage) > 64:
            raise HTTPException(400, "from_stage must be a non-empty stage name")
        data["from_stage"] = stage

    if event_type == EV_SPEC_APPROVED:
        current = _state()
        if (current.proposed_spec is None or not current.spec_approval_requested
                or current.spec_confirmed):
            raise HTTPException(409, {
                "code": "ratification_not_requested",
                "message": "the run is not awaiting eval-spec ratification",
                "remediation": "refresh the run and ratify only its active spec request",
            })

    target_key = _LIFECYCLE_CONTROL_TARGETS.get(event_type)
    if target_key is not None:
        current = _state()
        if event_type == EV_APPROVAL_GRANTED and not current.awaiting_approval:
            raise HTTPException(409, {
                "code": "approval_not_requested",
                "message": "the run is not awaiting result approval",
                "remediation": "refresh the run and approve only its active result request",
            })
        raw_nid = data.get(target_key)
        if event_type == EV_APPROVAL_GRANTED and raw_nid is None:
            # A bare/default approval means "the exact pending request", never "whatever node is
            # best now". The best can change, and reset can reuse a node id with another attempt.
            # Modern callers pass both fields explicitly; this fallback preserves convenience only
            # when the fold still has authoritative subject + lifecycle identity.
            nid = current.approval_subject
            pending_generation = current.approval_generation
            if (not current.awaiting_approval or isinstance(nid, bool)
                    or not isinstance(nid, int) or nid < 0
                    or isinstance(pending_generation, bool)
                    or not isinstance(pending_generation, int) or pending_generation < 0):
                raise HTTPException(409, {
                    "code": "approval_target_unavailable",
                    "message": "the pending approval target cannot be verified",
                    "remediation": "refresh the run and inspect Events before approving",
                })
            data["generation"] = pending_generation
        else:
            nid = _strict_integer(raw_nid, target_key)
            if nid < 0:
                raise HTTPException(400, f"{target_key} must be non-negative")
        node = current.nodes.get(nid)
        if node is None:
            raise HTTPException(404, f"no node #{nid} in this run")
        if node.tombstoned:
            raise HTTPException(409, f"node #{nid} is tombstoned and cannot be controlled")
        if nid in current.aborted_nodes and event_type not in (EV_NODE_ABORT, EV_NODE_RESET):
            raise HTTPException(409, f"node #{nid} is aborted; reset it before {event_type}")
        raw_generation = data.get("generation", _ABSENT)
        if raw_generation is _ABSENT:
            if node.attempt != 0:
                raise HTTPException(
                    409, f"stale {event_type}: generation is required "
                         f"(current generation is {node.attempt})")
            generation = 0
        else:
            generation = _strict_integer(raw_generation, "generation")
            if generation < 0:
                raise HTTPException(400, "generation must be non-negative")
        if generation != node.attempt:
            raise HTTPException(
                409, f"stale {event_type}: node #{nid} is generation "
                     f"{node.attempt}, not {generation}")
        data[target_key] = nid
        data["generation"] = generation

    if event_type == EV_INJECT_NODE and data.get("source_run") and data.get("source_node") is not None:
        sr = str(data.pop("source_run"))
        if not sr or len(sr) > 255:
            raise HTTPException(400, "source_run must be a non-empty run id")
        raw_source_node = data.pop("source_node")
        sn = _strict_integer(raw_source_node, "source_node")
        source_rd = srv.run_dir(sr)
        command_service = getattr(srv, "commands", None)
        if command_service is not None and callable(getattr(command_service, "validate_paths", None)):
            source_rd = command_service.validate_paths(source_rd)
        sst = srv.state(source_rd)
        snode = sst.nodes.get(sn)
        if snode is None:
            raise HTTPException(404, f"no experiment #{sn} in run {sr}")
        if snode.tombstoned:
            raise HTTPException(409, f"source experiment #{sn} in run {sr} is tombstoned")
        if sn in sst.aborted_nodes:
            raise HTTPException(409, f"source experiment #{sn} in run {sr} is aborted")
        sidea = snode.idea.model_dump(mode="json")
        note = f"imported from run {sr} #{sn}"
        base = (sidea.get("rationale") or "").strip()
        sidea["rationale"] = f"{base} | {note}" if base else note
        data["idea"] = sidea
        data["code"] = snode.code or None
        data["files"] = dict(snode.files)
        data["deleted"] = list(snode.deleted)
        data["origin"] = {"run_id": sr, "node_id": sn, "metric": snode.robust_metric}

    # The event fold is intentionally tolerant of historical hand-authored logs.  HTTP mutation is a
    # stronger trust boundary: reject payloads that would otherwise become permanent replay poison or
    # silently do nothing while the command lifecycle reports success.
    if event_type == EV_RUN_ABORT and data.get("reason") is not None:
        data["reason"] = _text("reason", limit=256)
    elif event_type == EV_NODE_ABORT:
        data["node_id"] = _node("node_id")
        if data.get("reason") is not None:
            data["reason"] = _text("reason", required=False, limit=1000)
    elif event_type in {EV_FORCE_CONFIRM, EV_FORCE_ABLATE, EV_ANNOTATION, EV_PROMOTE}:
        data["node_id"] = _node("node_id")
        if event_type == EV_ANNOTATION:
            data["text"] = _text("text")
        if event_type == EV_PROMOTE and data.get("alias") is not None:
            data["alias"] = _text("alias", limit=128)
    elif event_type == EV_FORK:
        data["from_node_id"] = _node("from_node_id")
    elif event_type == EV_BUDGET_EXTEND:
        allowed = ("add_nodes", "max_seconds", "max_eval_seconds", "timeout", "max_parallel")
        if not any(data.get(name) is not None for name in allowed):
            raise HTTPException(400, "budget_extend needs at least one budget field")
        if data.get("add_nodes") is not None:
            value = _integer("add_nodes")
            # Upper bound too: a huge extension (or a 400-digit int) is not a valid budget and would
            # let a single control command balloon the run. Ceiling mirrors Settings.max_nodes.
            if value <= 0 or value > 1_000_000:
                raise HTTPException(400, "add_nodes must be between 1 and 1000000")
            data["add_nodes"] = value
        if data.get("max_parallel") is not None:
            value = _integer("max_parallel")
            # Ceiling mirrors Settings.max_parallel: an unbounded value fans out that many sandboxes.
            if value <= 0 or value > 1024:
                raise HTTPException(400, "max_parallel must be between 1 and 1024")
            data["max_parallel"] = value
        for name in ("max_seconds", "max_eval_seconds", "timeout"):
            if data.get(name) is None:
                continue
            value = data[name]
            if isinstance(value, bool):
                raise HTTPException(400, f"{name} must be a finite positive number")
            try:
                value = float(value)
            except (TypeError, ValueError, OverflowError):
                raise HTTPException(400, f"{name} must be a finite positive number")
            if not math.isfinite(value) or value <= 0:
                raise HTTPException(400, f"{name} must be a finite positive number")
            data[name] = value
    elif event_type == EV_HINT:
        data["text"] = _text("text")
        if data.get("replace") is not None and not isinstance(data["replace"], bool):
            raise HTTPException(400, "replace must be a boolean")
    elif event_type == EV_SET_STRATEGY:
        strategy = data.get("strategy")
        if not isinstance(strategy, dict) or not strategy:
            raise HTTPException(400, "strategy must be a non-empty JSON object")
        unknown_strategy = set(strategy) - {"policy", "policy_params", "fidelity"}
        if unknown_strategy:
            raise HTTPException(
                400, f"strategy has unknown field(s): {', '.join(sorted(unknown_strategy))}")
        from looplab.search.policy import available_policies
        clean_strategy = {}
        policy = strategy.get("policy")
        if policy is not None:
            if not isinstance(policy, str) or policy not in available_policies():
                raise HTTPException(400, "strategy.policy must name an available policy")
            clean_strategy["policy"] = policy
        fidelity = strategy.get("fidelity")
        if fidelity is not None:
            if fidelity not in {"smoke", "full", "adaptive"}:
                raise HTTPException(400, "strategy.fidelity must be smoke, full, or adaptive")
            clean_strategy["fidelity"] = fidelity
        params = strategy.get("policy_params")
        if params is not None:
            if not isinstance(params, dict) or not params:
                raise HTTPException(400, "strategy.policy_params must be a non-empty JSON object")
            if policy is None:
                raise HTTPException(400, "strategy.policy_params requires an explicit policy")
            allowed_params = ({"c"} if policy == "mcts" else
                              {"eta", "rung_nodes"} if policy in {"asha", "bohb"} else set())
            unknown_params = set(params) - allowed_params
            if unknown_params:
                raise HTTPException(400, f"strategy.policy_params not supported for {policy}: "
                                             f"{', '.join(sorted(unknown_params))}")
            clean_params = {}
            if "c" in params:
                value = params["c"]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(float(value)) or float(value) < 0):
                    raise HTTPException(400, "strategy.policy_params.c must be finite and non-negative")
                clean_params["c"] = float(value)
            for name in ("eta", "rung_nodes"):
                if name not in params:
                    continue
                value = _strict_integer(params[name], f"strategy.policy_params.{name}")
                if (name == "eta" and value < 2) or (name == "rung_nodes" and value < 0):
                    raise HTTPException(400, f"strategy.policy_params.{name} is out of range")
                clean_params[name] = value
            clean_strategy["policy_params"] = clean_params
        if not clean_strategy:
            raise HTTPException(400, "strategy must change policy, policy_params, or fidelity")
        data["strategy"] = clean_strategy
    elif event_type == EV_INJECT_NODE:
        allowed_inject = {
            "idea", "parent_id", "parent_ids", "parent_generations",
            "code", "files", "deleted", "origin",
        }
        unknown_inject = set(data) - allowed_inject
        if unknown_inject:
            raise HTTPException(
                400, f"inject_node has unknown field(s): {', '.join(sorted(unknown_inject))}")
        if data.get("parent_id") is not None and data.get("parent_ids") is not None:
            raise HTTPException(400, "inject_node accepts parent_id or parent_ids, not both")
        idea = data.get("idea")
        if not isinstance(idea, dict) or not idea:
            raise HTTPException(400, "idea must be a non-empty JSON object")
        unknown_idea = set(idea) - set(Idea.model_fields)
        if unknown_idea:
            raise HTTPException(400, f"idea has unknown field(s): {', '.join(sorted(unknown_idea))}")
        operator = idea.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise HTTPException(400, "idea.operator must be a non-empty string")
        try:
            normalized_idea = Idea.model_validate(idea)
        except ValidationError as exc:
            issues = [f"{'.'.join(map(str, row.get('loc') or ('idea',)))}: {row.get('msg')}"
                      for row in exc.errors(include_url=False)[:5]]
            raise HTTPException(400, f"idea is invalid: {'; '.join(issues)}") from exc
        data["idea"] = normalized_idea.model_dump(mode="json")
        if data.get("parent_id") is not None:
            data["parent_id"] = _node("parent_id")
        if data.get("parent_ids") is not None:
            parents = data["parent_ids"]
            if not isinstance(parents, list) or not parents or len(parents) > 64:
                raise HTTPException(400, "parent_ids must be a non-empty list of at most 64 node ids")
            normalized_parents = []
            for value in parents:
                value = _strict_integer(value, "parent_ids entries")
                if value not in _state().nodes:
                    raise HTTPException(404, f"no node #{value} in this run")
                normalized_parents.append(value)
            data["parent_ids"] = normalized_parents
        parents = ([data["parent_id"]] if data.get("parent_id") is not None
                   else list(data.get("parent_ids") or []))
        if len(set(parents)) != len(parents):
            raise HTTPException(400, "parent ids must be unique")
        raw_snapshot = data.get("parent_generations", _ABSENT)
        if raw_snapshot is not _ABSENT and not isinstance(raw_snapshot, dict):
            raise HTTPException(400, "parent_generations must be an object")
        if raw_snapshot is _ABSENT:
            if any(_state().nodes[pid].attempt != 0 for pid in parents):
                raise HTTPException(409, "parent generation is required after node reset")
            raw_snapshot = {str(pid): 0 for pid in parents}
        if len(raw_snapshot) != len(parents):
            raise HTTPException(400, "parent generation snapshot does not match parents")
        normalized_snapshot: dict[str, int] = {}
        for pid in parents:
            parent = _state().nodes[pid]
            if parent.tombstoned:
                raise HTTPException(409, f"parent #{pid} is tombstoned")
            if pid in _state().aborted_nodes:
                raise HTTPException(409, f"parent #{pid} is aborted")
            raw_generation = raw_snapshot.get(str(pid), raw_snapshot.get(pid, _ABSENT))
            if raw_generation is _ABSENT:
                raise HTTPException(400, f"missing generation for parent #{pid}")
            generation = _strict_integer(raw_generation, "parent generation")
            if generation < 0:
                raise HTTPException(400, "parent generation must be non-negative")
            if generation != parent.attempt:
                raise HTTPException(
                    409, f"stale parent #{pid}: current generation is {parent.attempt}")
            normalized_snapshot[str(pid)] = generation
        data["parent_generations"] = normalized_snapshot
        if data.get("code") is not None and not isinstance(data["code"], str):
            raise HTTPException(400, "code must be a string or null")
        files = data.get("files")
        if files is None:
            files = {}
        if not isinstance(files, dict):
            raise HTTPException(400, "files must be an object mapping relative paths to strings")

        def _relative_file_name(value, field: str) -> str:
            if (not isinstance(value, str) or not value or len(value) > 512
                    or any(ord(ch) < 32 for ch in value)):
                raise HTTPException(400, f"{field} entries must be non-empty relative path strings")
            portable = value.replace("\\", "/")
            parsed = PurePosixPath(portable)
            raw_parts = portable.split("/")
            reserved = {"CON", "PRN", "AUX", "NUL",
                        *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))}
            if (not parsed.parts or parsed.is_absolute() or ":" in portable
                    or any(part in {"", ".", ".."} for part in raw_parts)
                    or any(part.endswith((".", " ")) for part in raw_parts)
                    or any(part.split(".", 1)[0].upper() in reserved for part in raw_parts)):
                raise HTTPException(400, f"{field} entries must stay within the node workspace")
            return portable

        normalized_files = {}
        for name, content in files.items():
            name = _relative_file_name(name, "files")
            if not isinstance(content, str):
                raise HTTPException(400, "files values must be strings")
            normalized_files[name] = content
        data["files"] = normalized_files
        deleted = data.get("deleted")
        if deleted is None:
            deleted = []
        if not isinstance(deleted, list):
            raise HTTPException(400, "deleted must be a list of relative path strings")
        data["deleted"] = [_relative_file_name(name, "deleted") for name in deleted]
        origin = data.get("origin")
        if origin is not None and not isinstance(origin, dict):
            raise HTTPException(400, "origin must be a JSON object or null")
    elif event_type == EV_HYPOTHESIS_ADDED:
        data["statement"] = _text("statement")
        if data.get("id") is not None:
            data["id"] = _text("id", limit=256)
        if data.get("source") is not None:
            data["source"] = _text("source", limit=128)
    elif event_type == EV_HYPOTHESIS_UPDATED:
        data["id"] = _text("id", limit=256)
        status = _text("status", limit=64).lower()
        if status not in {"open", "abandoned", "deleted"}:
            raise HTTPException(400, "hypothesis status must be open, abandoned, or deleted")
        data["status"] = status

    try:
        # Encode INSIDE the guard: json.dumps(ensure_ascii=False) accepts a lone surrogate (valid
        # JSON \uD800) but str.encode("utf-8") then raises UnicodeEncodeError — a ValueError subclass.
        # Keeping the encode out here surfaced it as a 500; every sibling validation error is a 400.
        encoded_bytes = json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(400, f"control data must be finite, encodable JSON: {exc}") from exc
    if len(encoded_bytes) > 1_048_576:
        raise HTTPException(413, "control data is too large (maximum 1 MiB)")
    return data


def _error(code: str, message: str, remediation: str = "", *, retryable: bool = False) -> dict:
    return {"code": code, "message": redact_secrets(str(message)), "retryable": bool(retryable),
            "remediation": redact_secrets(str(remediation))}


class RunCommandService:
    def __init__(self, srv, *, engine_alive: Callable[[Path], bool] = _engine_alive,
                 engine_liveness: Optional[Callable[[Path], Optional[bool]]] = None,
                 spawn_engine: Callable[..., Optional[int]] = _spawn_engine,
                 process_alive: Callable[[Optional[int]], Optional[bool]] = _process_alive,
                 process_identity: Callable[[Optional[int]], Optional[str]] = _process_identity,
                 startup_timeout: float = 3.0, command_timeout: float = 120.0,
                 poll_interval: float = 0.05,
                 max_observation_timeout: Optional[float] = None,
                 lock_acquire_timeout: float = 60.0):
        self.srv = srv
        self.engine_alive = engine_alive
        # Existing tests/integrations inject the historical bool probe. Treat those values as exact;
        # production uses the tri-state probe so unsupported/inaccessible ownership stays unknown.
        self.engine_liveness = (engine_liveness if engine_liveness is not None else
                                (_engine_liveness if engine_alive is _engine_alive else engine_alive))
        self.spawn_engine = spawn_engine
        self.process_alive = process_alive
        self.process_identity = process_identity
        self.startup_timeout = max(0.05, float(startup_timeout))
        self.command_timeout = max(self.startup_timeout, float(command_timeout))
        self.max_observation_timeout = max(
            self.command_timeout,
            float(max_observation_timeout) if max_observation_timeout is not None
            else max(300.0, self.command_timeout * 10),
        )
        self.poll_interval = max(0.01, float(poll_interval))
        self.lock_acquire_timeout = max(0.05, float(lock_acquire_timeout))
        self._local_lock = threading.RLock()
        self._run_locks: dict[str, threading.RLock] = {}
        self._command_observations = CommandObservationIndex(max_indexed_runs=8)

    def _engine_state(self, rd: Path) -> Optional[bool]:
        try:
            value = self.engine_liveness(rd)
        except OSError:
            return None
        if value is True:
            return True
        if value is False:
            return False
        return None

    @staticmethod
    def _engine_unknown_error(operation: str, *, retryable: bool = False) -> dict:
        return _error(
            "engine_liveness_unknown",
            f"cannot {operation} because engine ownership is unknown",
            ("inspect engine.lock and storage locking, then retry this command only after liveness "
             "is verifiable" if retryable else
             "inspect engine.lock and storage locking, then submit a new command with a new "
             "idempotency key after liveness is verifiable"),
            retryable=retryable,
        )

    def _lock_directory(self) -> Path:
        root = self.srv.root.resolve()
        directory = root / ".command-locks"
        try:
            if directory.is_symlink() or (directory.exists() and directory.resolve().parent != root):
                raise HTTPException(409, "run .command-locks must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or directory.resolve().parent != root:
                raise HTTPException(409, "run .command-locks changed during validation")
        except OSError as exc:
            raise HTTPException(409, f"run command-lock path cannot be validated: {exc}") from exc
        return directory

    def _sequence_path(self, rd: Path) -> Path:
        # Case-insensitive desktop filesystems must not give ``Foo`` and ``foo`` two OS locks/spawn
        # claims before either directory exists; `_lock_identity` normalizes Windows and default macOS.
        identity = _lock_identity(rd)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        path = self._lock_directory() / f"{digest}.lock"
        if path.is_symlink():
            raise HTTPException(409, "run command lock must not be a symlink")
        return path

    def _spawn_claim_path(self, rd: Path) -> Path:
        path = self._sequence_path(rd).with_suffix(".spawn.json")
        if path.is_symlink():
            raise HTTPException(409, "run spawn claim must not be a symlink")
        return path

    def _start_record_path(self, rd: Path) -> Path:
        """Root-sidecar path for the durable start operation occupying ``rd``.

        A start record must exist before the run directory does and must survive a partial
        materialization, so it cannot live underneath ``rd``. Deriving it from the sequencer path
        gives it exactly the same case/Unicode identity as the lock and spawn claim.
        """
        path = self._sequence_path(rd).with_suffix(".start.json")
        if path.is_symlink():
            raise HTTPException(409, "run start record must not be a symlink")
        return path

    def load_start_record(self, rd: Path) -> Optional[dict]:
        """Load one start sidecar, failing closed on unreadable or malformed ownership evidence."""
        path = self._start_record_path(rd)
        if not path.exists():
            return None
        record = self._load(path)
        # Atomic writers publish a complete JSON object. Once the path exists, an unreadable value
        # or a record without its exact operation identity is therefore unresolved evidence, never
        # permission for a caller to reserve the run or invoke Popen again.
        if record is None or not isinstance(record.get("id"), str) or not record.get("id"):
            raise HTTPException(503, {
                "code": "start_record_unavailable",
                "message": "The durable run-start record is unreadable or malformed.",
                "remediation": "Inspect the .command-locks start sidecar; do not start another engine.",
            })
        return record

    def save_start_record(self, rd: Path, record: dict) -> None:
        """Atomically publish a complete start record while the caller holds ``sequence(rd)``."""
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) \
                or not record.get("id"):
            raise ValueError("start record requires a non-empty string id")
        self._save(self._start_record_path(rd), record)

    def retire_start_record(self, rd: Path, start_id: str) -> bool:
        """Retire only the sidecar whose stored id exactly matches ``start_id``.

        The caller must hold ``sequence(rd)`` when retirement is part of delete/replacement. A
        mismatched id and malformed evidence are deliberately not cleared.
        """
        record = self.load_start_record(rd)
        if record is None or not isinstance(start_id, str) or record.get("id") != start_id:
            return False
        path = self._start_record_path(rd)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise HTTPException(503, f"could not retire run start record: {exc}") from exc
        return True

    def run_generation(self, rd: Path) -> str:
        """Stable identity of the event-log generation currently occupying a run id.

        Generation identity depends only on the first durable event. Read just that record so callers
        can validate the identity while holding the run sequencer without turning every poll into an
        O(events) critical section. ``iter_jsonl`` preserves the event store's torn/corrupt-first-line
        semantics; ``run_generation_token`` also validates that dictionary through ``Event`` so a
        complete JSON object with an invalid event schema remains generation-less, as in ``read_all``.
        """
        return run_generation_token(iter_jsonl(self._events_path(rd)))

    @contextmanager
    def run_activity(self, rd: Path, kind: str, *, generation: str):
        """Lease a run generation for server-side work that can append while reset is possible."""
        token = secrets.token_hex(16)
        path = self._directory(rd) / f".activity_{token}.json"
        with self.sequence(rd):
            if self.run_generation(rd) != generation:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "message": "The run was reset or replaced before this background work started.",
                    "remediation": "Refresh the run and submit the request against its current generation.",
                })
            now = time.time()
            owner = {"kind": str(kind)[:80], "pid": os.getpid(), "created_at": now}
            try:
                identity = self.process_identity(os.getpid())
            except Exception:  # noqa: BLE001
                identity = None
            if identity:
                owner["process_identity"] = identity
            self._save(path, owner)
        try:
            yield
        finally:
            try:
                with self.sequence(rd):
                    path.unlink(missing_ok=True)
            except (HTTPException, OSError):
                pass

    def _claim_child_definitely_gone(self, row: dict) -> bool:
        pid = row.get("pid")
        try:
            pid_state = self.process_alive(pid)
        except Exception:  # noqa: BLE001
            pid_state = None
        if pid_state is False:
            return True
        stored_identity = row.get("process_identity")
        if stored_identity:
            try:
                current_identity = self.process_identity(pid)
            except Exception:  # noqa: BLE001
                current_identity = None
            # A mismatch proves reuse only when both tokens share a comparable source encoding.
            if _process_identity_proves_reuse(stored_identity, current_identity):
                return True
        return False

    def _claim_child_exactly_alive(self, row: dict) -> bool:
        """Whether a spawn claim names the exact currently-live PID generation."""
        pid = row.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            if self.process_alive(pid) is not True:
                return False
        except Exception:  # noqa: BLE001 - an inaccessible process is ambiguous, not known-live
            return False
        stored_identity = row.get("process_identity")
        if not isinstance(stored_identity, str) or not stored_identity:
            return False
        try:
            return self.process_identity(pid) == stored_identity
        except Exception:  # noqa: BLE001 - identity lookup failure stays fail-closed/uncertain
            return False

    def _execution_owner_definitely_gone(self, path: Path) -> bool:
        """Return true only when a stale execution claim cannot still have a live owner.

        Age alone is not ownership evidence: a suspended worker can miss every heartbeat and later
        resume.  Reclaiming its file in that state would permit two workers to write terminal command
        state.  New claims therefore carry the server process creation identity; legacy bare-PID
        claims remain readable, and malformed/ambiguous claims fail closed.
        """
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return False
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            pid = parsed.get("pid")
            stored_identity = parsed.get("process_identity")
        else:
            try:
                pid = int(raw)
            except (TypeError, ValueError, OverflowError):
                return False
            stored_identity = None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            pid_state = self.process_alive(pid)
        except Exception:  # noqa: BLE001 - ambiguous ownership must fail closed
            pid_state = None
        if pid_state is False:
            return True
        if isinstance(stored_identity, str) and stored_identity:
            try:
                current_identity = self.process_identity(pid)
            except Exception:  # noqa: BLE001 - ambiguous ownership must fail closed
                current_identity = None
            if _process_identity_proves_reuse(stored_identity, current_identity):
                return True
        return False

    def _execution_owner_exactly_alive(self, path: Path) -> bool:
        """True only when the claim names the exact live process generation that created it."""
        row = self._load(path)
        if not row:
            return False
        pid = row.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            if self.process_alive(pid) is not True:
                return False
        except Exception:  # noqa: BLE001 - unknown is not exact-live proof
            return False
        stored_identity = row.get("process_identity")
        if isinstance(stored_identity, str) and stored_identity:
            try:
                return self.process_identity(pid) == stored_identity
            except Exception:  # noqa: BLE001
                return False
        # Even on a platform where creation identity is unavailable, never let an operator clear a
        # claim owned by this very server process: its worker/activity context may still be running.
        return pid == os.getpid()

    def resolve_active_claims(self, rd: Path, confirmation: str = "") -> dict:
        """Retire orphaned execution/activity claims without ever clearing a proven live owner.

        Atomic hard-link publication below removes the normal empty-file crash window.  This route
        remains the guarded escape hatch for pre-upgrade, malformed, inaccessible-owner, or
        hard-link-fallback claims.  It is intentionally explicit because ambiguity is not evidence
        that a suspended worker died.
        """
        phrase = "I verified no LoopLab command or run activity is active"
        root = self.srv.root.resolve()
        canonical = rd.resolve()
        if canonical == root or canonical.parent != root or rd.is_symlink():
            raise HTTPException(400, "active-claim run must be a canonical direct child")
        with self.sequence(canonical):
            directory = self._directory(canonical)
            claims = [
                *directory.glob(".cmd_*.executing"),
                *directory.glob(".activity_*.json"),
            ] if directory.exists() else []
            if not claims:
                return {"ok": True, "resolved": False, "count": 0,
                        "reason": "no_active_claims"}

            unresolved: list[Path] = []
            retired = 0
            for claim in claims:
                if claim.is_symlink():
                    raise HTTPException(409, {
                        "code": "active_claim_symlink",
                        "message": "An active-claim path is a symbolic link and cannot be inspected safely.",
                        "remediation": "Inspect and remove the link locally; the API will not follow it.",
                    })
                try:
                    if self._execution_owner_definitely_gone(claim):
                        claim.unlink()
                        retired += 1
                        continue
                except OSError:
                    pass
                if self._execution_owner_exactly_alive(claim):
                    raise HTTPException(409, {
                        "code": "active_claim_owner_alive",
                        "message": "The exact process generation owning a command/activity claim is alive.",
                        "remediation": "Wait for it to finish or stop that process; never clear its live claim.",
                    })
                unresolved.append(claim)

            if not unresolved:
                return {"ok": True, "resolved": bool(retired), "count": retired,
                        "reason": "owners_definitively_gone"}
            now = time.time()
            minimum_age = max(5.0, self.startup_timeout * 2 + 1)
            for claim in unresolved:
                try:
                    created_at = float((self._load(claim) or {}).get("created_at")
                                       or claim.stat().st_mtime)
                except (OSError, TypeError, ValueError, OverflowError):
                    created_at = now
                if now - created_at < minimum_age:
                    raise HTTPException(409, {
                        "code": "active_claim_uncertain",
                        "message": "An unknown command/activity claim is still inside its safety window.",
                        "remediation": "Wait, inspect the process table, then retry explicit resolution.",
                    })
            if confirmation != phrase:
                raise HTTPException(409, {
                    "code": "active_claim_confirmation_required",
                    "message": "Claim ownership is unknown; automatic death proof is impossible.",
                    "remediation": f"After inspection, repeat with confirmation exactly: {phrase}",
                })

            # Revalidate immediately before unlinking. If any exact owner appeared/becomes provable,
            # leave every remaining claim intact rather than partially overriding live ownership.
            if any(self._execution_owner_exactly_alive(claim) for claim in unresolved):
                raise HTTPException(409, "an active claim owner became live during resolution")
            for claim in unresolved:
                try:
                    claim.unlink()
                    retired += 1
                except OSError as exc:
                    raise HTTPException(503, f"could not resolve active claim: {exc}") from exc
            return {"ok": True, "resolved": True, "count": retired,
                    "reason": "operator_verified_unknown_claims"}

    def _recent_spawn_claim(self, rd: Path) -> bool:
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if path.exists() and (row is None or not isinstance(row.get("command_id"), str)
                              or not row.get("command_id")):
            return True  # malformed ownership evidence is unresolved, never permission to Popen
        if not row:
            return False
        # engine.lock is the startup postcondition for the lease itself. Once observed, the Popen
        # race is over and even an external/reset claim can be retired immediately.
        liveness = self._engine_state(rd)
        if liveness is True:
            try:
                path.unlink()
            except OSError:
                pass
            # Block THIS decision even though the lease can now be retired. A caller may have probed
            # liveness as false just before this probe observed the child acquire engine.lock; false
            # here would be interpreted as permission to Popen a duplicate (TOCTOU).
            return True
        if liveness is None:
            return True  # unknown ownership is an expiry-free anti-Popen fence
        quarantined = bool(row.get("quarantined"))
        if quarantined:
            if not self._claim_child_definitely_gone(row):
                return True
            # Definitive child death makes a retry/new driver safe again.
            try:
                path.unlink()
            except OSError:
                pass
            return False
        try:
            expires_at = float(row.get("expires_at"))
        except (TypeError, ValueError, OverflowError):
            try:
                expires_at = float(row.get("created_at")) + max(
                    self.command_timeout, self.startup_timeout * 2 + 1)
            except (TypeError, ValueError, OverflowError):
                expires_at = 0
        # A recorded child that has already exited is conclusive even inside the cold-start lease.
        # Waiting for the full observation timeout would make a pre-lock startup crash invisible and
        # block an otherwise safe retry for up to twenty minutes.
        if self._claim_child_definitely_gone(row):
            try:
                path.unlink()
            except OSError:
                pass
            return False
        if time.time() <= expires_at:
            return True
        # The observation deadline expiring is NOT proof that a detached child died. Promote the
        # lease to an expiry-free quarantine and release it only after engine.lock appears (handled
        # above) or the recorded PID is definitively gone. A missing PID is the crash window between
        # Popen and persisting its result and therefore remains unknown/fail-closed.
        row["quarantined"] = True
        row["quarantined_at"] = time.time()
        row["expires_at"] = None
        self._save(path, row)
        if not self._claim_child_definitely_gone(row):
            return True
        try:
            path.unlink()
        except OSError:
            pass
        return False

    def _record_spawn_claim(self, rd: Path, command_id: str, pid: Optional[int]) -> None:
        now = time.time()
        row = {"command_id": command_id, "created_at": now,
               "expires_at": now + self.max_observation_timeout,
               "pid": pid}
        if pid is not None:
            try:
                identity = self.process_identity(pid)
            except Exception:  # noqa: BLE001
                identity = None
            if identity:
                row["process_identity"] = identity
        self._save(self._spawn_claim_path(rd), row)

    def _quarantine_spawn_claim(self, rd: Path, command_id: str,
                                pid: Optional[int]) -> bool:
        """Keep an uncertain Popen owner until lock evidence or definitive PID death.

        Returns whether the claim is still unsafe after refreshing its process liveness.
        """
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if path.exists() and (row is None or not isinstance(row.get("command_id"), str)
                              or not row.get("command_id")):
            return True
        if row and str(row.get("command_id") or "") != command_id:
            return self._recent_spawn_claim(rd)
        now = time.time()
        row = dict(row or {"command_id": command_id, "created_at": now})
        if pid is not None:
            row["pid"] = pid
            if not row.get("process_identity"):
                try:
                    identity = self.process_identity(pid)
                except Exception:  # noqa: BLE001
                    identity = None
                if identity:
                    row["process_identity"] = identity
        row["quarantined"] = True
        row["quarantined_at"] = now
        row["expires_at"] = None
        self._save(path, row)
        return self._recent_spawn_claim(rd)

    def _clear_spawn_claim(self, rd: Path, command_id: str) -> None:
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if path.exists() and (row is None or not isinstance(row.get("command_id"), str)
                              or not row.get("command_id")):
            return
        if row and str(row.get("command_id") or "") != command_id:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def spawn_inflight(self, rd: Path) -> bool:
        """True while a Popen is unresolved/quarantined, or on the decision that observes its lock."""
        return self._recent_spawn_claim(rd)

    def record_external_spawn(self, rd: Path, owner: str, pid: Optional[int]) -> None:
        """Register a legacy/reset Popen performed while the caller holds ``sequence(rd)``."""
        self._record_spawn_claim(rd, f"external:{owner}", pid)

    def begin_external_spawn(self, rd: Path, owner: str) -> None:
        """Install the crash-safe lease immediately before a legacy/reset Popen."""
        self._record_spawn_claim(rd, f"external:{owner}", None)

    def cancel_external_spawn(self, rd: Path, owner: str) -> None:
        self._clear_spawn_claim(rd, f"external:{owner}")

    def observe_external_spawn(self, rd: Path, owner: str) -> str:
        """Observe the spawn claim correlated to ``owner`` without ever starting a process.

        The bounded result vocabulary is intentionally evidence-oriented:

        * ``absent``: no claim and no engine lock are visible;
        * ``live``: the engine lock is visible, including after its claim was already retired;
        * ``pending_known``: a matching claim names the exact live PID generation, pre-lock;
        * ``uncertain``: matching/malformed evidence cannot prove either liveness or death;
        * ``dead_or_cleared``: the matching claim was definitively dead and was retired;
        * ``mismatched``: the extant claim belongs to another external owner.

        This delegates expiry, quarantine, engine-lock retirement, PID-death and PID-reuse handling
        to ``_recent_spawn_claim``. It never exposes the stored PID creation identity. Callers that
        make a subsequent ownership decision should hold ``sequence(rd)`` across both operations.
        """
        expected = f"external:{owner}"
        path = self._spawn_claim_path(rd)
        row = self._load(path)
        if not path.exists():
            liveness = self._engine_state(rd)
            return "live" if liveness is True else "absent" if liveness is False else "uncertain"
        if row is None or not isinstance(row.get("command_id"), str) \
                or not row.get("command_id"):
            # Preserve the existing fail-closed semantics (and any future quarantine bookkeeping).
            self._recent_spawn_claim(rd)
            return "uncertain"
        if row.get("command_id") != expected:
            # Still let the canonical observer retire a definitively dead/lock-observed claim, but
            # never alias another owner's operation to the supplied owner in this decision.
            self._recent_spawn_claim(rd)
            return "mismatched"

        active = self._recent_spawn_claim(rd)
        if not active:
            return "dead_or_cleared"

        # For a valid matching row, `_recent_spawn_claim` can return true with no remaining path only
        # on the decision that observed engine.lock and retired the lease. Preserve that positive
        # observation even if the lock changes immediately after the probe.
        if not path.exists():
            return "live"
        current = self._load(path)
        if current is None or not isinstance(current.get("command_id"), str) \
                or not current.get("command_id"):
            return "uncertain"
        if current.get("command_id") != expected:
            return "mismatched"
        liveness = self._engine_state(rd)
        if liveness is True:
            # The first `_recent_spawn_claim` probe may have raced just before lock acquisition. Run
            # it once more so the existing claim-retirement behavior remains centralized.
            self._recent_spawn_claim(rd)
            return "live"
        if liveness is None:
            return "uncertain"
        if self._claim_child_exactly_alive(current):
            return "pending_known"
        return "uncertain"

    def resolve_spawn_claim(self, rd: Path, confirmation: str = "") -> dict:
        """Safely retire a quarantined/unknown claim, with an explicit operator escape hatch.

        Known live children are never force-cleared. For an unreadable/identity-unknown claim the
        operator must provide an exact confirmation after independently checking the process table.
        """
        phrase = "I verified no LoopLab engine process is running"
        root = self.srv.root.resolve()
        canonical = rd.resolve()
        if canonical == root or canonical.parent != root or rd.is_symlink():
            raise HTTPException(400, "spawn-claim run must be a canonical direct child")
        with self.sequence(canonical):
            path = self._spawn_claim_path(canonical)
            if not path.exists():
                return {"ok": True, "resolved": False, "reason": "no_spawn_claim"}
            liveness = self._engine_state(canonical)
            if liveness is True:
                try:
                    path.unlink()
                except OSError as exc:
                    raise HTTPException(503, f"could not retire observed-live spawn claim: {exc}") from exc
                return {"ok": True, "resolved": True, "reason": "engine_lock_observed"}
            if liveness is None:
                raise HTTPException(409, self._engine_unknown_error("resolve the engine spawn claim"))

            row = self._load(path)
            if row and isinstance(row.get("command_id"), str):
                if self._claim_child_definitely_gone(row):
                    try:
                        path.unlink()
                    except OSError as exc:
                        raise HTTPException(503, f"could not retire dead-child spawn claim: {exc}") from exc
                    return {"ok": True, "resolved": True, "reason": "child_definitively_gone"}
                if self._claim_child_exactly_alive(row):
                    raise HTTPException(409, {
                        "code": "engine_start_uncertain",
                        "message": "The exact claimed child process is still alive.",
                        "remediation": "Inspect the process; never clear a live LoopLab child claim.",
                    })

            try:
                created_at = float((row or {}).get("quarantined_at")
                                   or (row or {}).get("created_at") or path.stat().st_mtime)
            except (OSError, TypeError, ValueError, OverflowError):
                created_at = time.time()
            minimum_age = max(5.0, self.startup_timeout * 2 + 1)
            if time.time() - created_at < minimum_age:
                raise HTTPException(409, {
                    "code": "engine_start_uncertain",
                    "message": "The unknown spawn claim is still inside its cold-start safety window.",
                    "remediation": "Wait, inspect the process table, then retry explicit resolution.",
                })
            if confirmation != phrase:
                raise HTTPException(409, {
                    "code": "spawn_claim_confirmation_required",
                    "message": "Process identity is unavailable; automatic child-death proof is impossible.",
                    "remediation": f"After inspection, repeat with confirmation exactly: {phrase}",
                })
            liveness = self._engine_state(canonical)  # final check before destructive unlink
            if liveness is not False:
                if liveness is None:
                    raise HTTPException(
                        409, self._engine_unknown_error("resolve the engine spawn claim"))
                raise HTTPException(409, "engine became live while resolving its spawn claim")
            try:
                path.unlink()
            except OSError as exc:
                raise HTTPException(503, f"could not resolve spawn claim: {exc}") from exc
            return {"ok": True, "resolved": True, "reason": "operator_verified_unknown_claim"}

    @contextmanager
    def sequence(self, rd: Path):
        """Serialize one run's decision→intent→spawn boundary across threads/processes.

        The OS lock is cross-process on ordinary Windows/POSIX filesystems. Contention uses bounded
        non-blocking retries; unsupported locking or acquisition timeout fails closed, never entering
        thread-only while another process may own the run. Lock files live outside the run directory
        so delete/reset can hold the guard while moving or removing the run itself.
        """
        key = _lock_identity(rd)
        with self._local_lock:
            local = self._run_locks.setdefault(key, threading.RLock())
        deadline = time.monotonic() + self.lock_acquire_timeout
        if not local.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise HTTPException(503, "timed out waiting for the in-process run command sequencer")
        try:
            lock_path = self._sequence_path(rd)
            handle = open(lock_path, "a+")
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write("\0")
                        handle.flush()
                    while True:
                        handle.seek(0)
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError as exc:
                            contention = exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                            if not contention:
                                raise HTTPException(
                                    503, f"run command locking is unsupported: {exc}") from exc
                            if time.monotonic() >= deadline:
                                raise HTTPException(
                                    503, "timed out waiting for the run command sequencer") from exc
                            time.sleep(min(0.05, self.poll_interval))
                else:
                    import fcntl
                    while True:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            locked = True
                            break
                        except OSError as exc:
                            contention = isinstance(exc, BlockingIOError) or exc.errno in {
                                errno.EACCES, errno.EAGAIN}
                            if not contention:
                                raise HTTPException(
                                    503, f"run command locking is unsupported: {exc}") from exc
                            if time.monotonic() >= deadline:
                                raise HTTPException(
                                    503, "timed out waiting for the run command sequencer") from exc
                            time.sleep(min(0.05, self.poll_interval))
                yield
            finally:
                if locked:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
        finally:
            local.release()

    def validate_paths(self, rd: Path) -> Path:
        """Return the canonical direct-child run, refusing writable sidecar symlinks.

        ``AppState.run_dir`` protects ordinary traversal, but an ``events.jsonl`` or ``.commands``
        symlink could otherwise turn an authenticated command into a write outside the run.  These
        are service-owned files, so unlike ``ui_meta.task_file`` there is no compatibility reason to
        permit indirection.
        """
        root = self.srv.root.resolve()
        canonical = rd.resolve()
        if canonical == root or canonical.parent != root:
            raise HTTPException(404, "no such run")
        events = canonical / "events.jsonl"
        if not events.exists():
            raise HTTPException(404, "no such run")
        try:
            if events.is_symlink() or events.resolve().parent != canonical:
                raise HTTPException(409, "run events.jsonl must not be a symlink")
        except OSError as exc:
            raise HTTPException(409, f"run event path cannot be validated: {exc}") from exc
        directory = canonical / ".commands"
        try:
            if directory.is_symlink() or (directory.exists() and directory.resolve().parent != canonical):
                raise HTTPException(409, "run .commands must not be a symlink")
        except OSError as exc:
            raise HTTPException(409, f"run command path cannot be validated: {exc}") from exc
        return canonical

    def _directory(self, rd: Path) -> Path:
        return self.validate_paths(rd) / ".commands"

    def _events_path(self, rd: Path) -> Path:
        return self.validate_paths(rd) / "events.jsonl"

    def _path(self, rd: Path, command_id: str) -> Path:
        if not _COMMAND_ID_RE.fullmatch(command_id):
            raise HTTPException(404, "no such command")
        path = self._directory(rd) / f"{command_id}.json"
        if path.is_symlink():
            raise HTTPException(409, "run command record must not be a symlink")
        return path

    def _exec_path(self, rd: Path, command_id: str) -> Path:
        path = self._directory(rd) / f".{command_id}.executing"
        if path.is_symlink():
            raise HTTPException(409, "run execution claim must not be a symlink")
        return path

    @staticmethod
    def _load(path: Path) -> Optional[dict]:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return row if isinstance(row, dict) else None

    @staticmethod
    def _save(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, indent=2, sort_keys=True, allow_nan=False)
        # Windows can deny ``os.replace`` for the few milliseconds another thread/process has the
        # destination open for a GET/read. The unique temp was already cleaned by atomic_write_text;
        # retry only the platform's transient sharing/access violations. Without this, observation
        # traffic can turn an otherwise-correct command into `command_worker_failed` even though its
        # marked intent is durable. Other errors remain immediate/fail-visible.
        for attempt in range(20):
            try:
                atomic_write_text(path, payload)
                return
            except PermissionError as exc:
                if (getattr(exc, "winerror", None) not in (5, 32)
                        and getattr(exc, "errno", None) not in (errno.EACCES, errno.EBUSY)):
                    raise
                if attempt == 19:
                    raise
                time.sleep(min(0.05, 0.002 * (attempt + 1)))

    @staticmethod
    def _public(record: dict) -> dict:
        hidden = {"data", "idempotency_key_digest", "payload_digest", "semantic_payload_digest",
                  "attached_semantic_payload_digest", "spawn_claim_released"}
        return {key: value for key, value in record.items() if key not in hidden}

    def _active_command_ids(self, rd: Path) -> list[str]:
        directory = self._directory(rd)
        if not directory.exists():
            return []
        active = []
        for path in directory.glob("cmd_*.json"):
            if path.is_symlink():
                active.append(path.stem)
                continue
            record = self._load(path)
            # A malformed durable record is fail-closed: destructive mutation must not erase the only
            # evidence of a command whose state cannot be determined.
            if record is None or record.get("status") not in TERMINAL_STATUSES:
                active.append(path.stem)
        for claim in directory.glob(".cmd_*.executing"):
            if claim.is_symlink():
                active.append(claim.name[1:-len(".executing")])
                continue
            try:
                # Positive owner death is conclusive immediately; age protects only ambiguous/live
                # owners from heartbeat pauses, not a PID the OS says no longer exists.
                owner_gone = self._execution_owner_definitely_gone(claim)
                if owner_gone:
                    claim.unlink()
                    continue
            except OSError:
                pass
            cid = claim.name[1:-len(".executing")]
            if cid not in active:
                active.append(cid)
        for claim in directory.glob(".activity_*.json"):
            if claim.is_symlink():
                active.append(claim.stem)
                continue
            try:
                if self._execution_owner_definitely_gone(claim):
                    claim.unlink()
                    continue
            except OSError:
                pass
            active.append(claim.stem)
        return sorted(active)

    def _unresolved_equivalent(self, rd: Path, event_type: str,
                               semantic_payload_digest: str) -> tuple[Optional[Path], Optional[dict]]:
        if event_type not in _RETRY_GUARDED_EVENTS:
            return None, None
        directory = self._directory(rd)
        if not directory.exists():
            return None, None
        candidates = []
        for path in directory.glob("cmd_*.json"):
            if path.is_symlink():
                raise HTTPException(409, "run command record must not be a symlink")
            record = self._load(path)
            if not record or record.get("event_type") != event_type:
                continue
            record_semantic = record.get("semantic_payload_digest")
            if not record_semantic:
                try:
                    _raw, record_semantic = self._payload(
                        event_type, dict(record.get("data") or {}))
                except HTTPException:
                    record_semantic = None
            if record_semantic != semantic_payload_digest:
                continue
            status = record.get("status")
            if status not in {"accepted", "executing", "failed", "timed_out"}:
                continue
            # accepted/executing is already one reserved logical command even in the tiny
            # reserve→append window. Failed/timed-out only block a new key if their intent became
            # durable; a pre-append validation/spawn failure is safe to correct under a new payload.
            if status in {"accepted", "executing"} or record.get("event_seq") is not None \
                    or self._find_intent(rd, str(record.get("id") or ""), record):
                candidates.append((float(record.get("created_at") or 0), path.name, path, record))
        if candidates:
            _created, _name, path, record = min(candidates, key=lambda item: (item[0], item[1]))
            return path, record
        return None, None

    def _finalize_incomplete(self, rd: Path, state=None) -> bool:
        """A finalize remains pending until its terminal projections are durably complete."""
        events = self._events(rd)
        if incomplete_finalize_scope(events) is not None:
            return True
        state = state or self.srv.state(rd)
        return bool(state.finalization_pending() or (state.stop_requested and (
            not state.finished or str(state.stop_reason or "").lower() == "error")))

    def _pending_finalize_intent(
            self, rd: Path, observation: Optional[CommandObservation] = None):
        """Return the latest canonical external/legacy run_abort and its semantic digest."""
        observation = observation or self._observe(rd)
        event = observation.latest_run_abort
        if event is None:
            return None, None
        data = dict(event.data or {})
        data.pop("_command_id", None)
        try:
            normalized = _normalize_finalize_data(data)
            _raw, digest = self._payload(EV_RUN_ABORT, normalized)
        except HTTPException:
            return event, None
        return event, digest

    def _attached_finalize_intact(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> bool:
        expected_seq = record.get("attached_event_seq")
        expected_digest = record.get("attached_semantic_payload_digest")
        if expected_seq is None or not expected_digest:
            return False
        observation = observation or self._observe(rd)
        latest, digest = self._pending_finalize_intent(rd, observation)
        if latest is None or latest.seq != expected_seq or digest != expected_digest:
            return False
        # The historical row may still exist after an external resume/superseding stop. Attachment
        # represents the effective pending finalize, not mere event ancestry.
        expected_reason = str((record.get("data") or {}).get("reason") or "")
        return str(observation.state().stop_requested or "") == expected_reason

    def _pending_finalize_record(self, rd: Path, semantic_payload_digest: Optional[str] = None
                                 ) -> tuple[Optional[Path], Optional[dict]]:
        """Find the durable finalize a reload/new browser key should observe, not duplicate."""
        finalize_incomplete = self._finalize_incomplete(rd)
        directory = self._directory(rd)
        if not directory.exists():
            return None, None
        candidates = []
        for path in directory.glob("cmd_*.json"):
            if path.is_symlink():
                raise HTTPException(409, "run command record must not be a symlink")
            record = self._load(path)
            if not record or record.get("event_type") != EV_RUN_ABORT:
                continue
            if (semantic_payload_digest is not None
                    and record.get("semantic_payload_digest", record.get("payload_digest"))
                    != semantic_payload_digest):
                continue
            if record.get("status") not in {"accepted", "executing", "failed", "timed_out"}:
                continue
            status = record.get("status")
            # accepted/executing is already the authoritative logical finalize even before its
            # worker appends. Failed/timed-out must have a durable/attached stop intent.
            if status in {"failed", "timed_out"}:
                if not finalize_incomplete:
                    continue
                if not record.get("attached") and record.get("event_seq") is None \
                        and self._find_intent(
                            rd, str(record.get("id") or ""), record) is None:
                    continue
            candidates.append((float(record.get("updated_at") or 0), path, record))
        if not candidates:
            return None, None
        _updated, path, record = max(candidates, key=lambda item: item[0])
        return path, record

    def _active_record(self, rd: Path) -> tuple[Optional[Path], Optional[dict]]:
        """Return the earliest reserved nonterminal command, including the pre-append window."""
        directory = self._directory(rd)
        if not directory.exists():
            return None, None
        candidates = []
        for path in directory.glob("cmd_*.json"):
            if path.is_symlink():
                raise HTTPException(409, "run command record must not be a symlink")
            record = self._load(path)
            if record is None:
                # A malformed/half-reserved record is an active unknown, so submission fails closed.
                record = {"id": path.stem, "status": "executing", "created_at": 0}
            if record.get("status") not in {"accepted", "executing"}:
                continue
            candidates.append((float(record.get("created_at") or 0), path.name, path, record))
        if not candidates:
            return None, None
        _created, _name, path, record = min(candidates, key=lambda item: (item[0], item[1]))
        return path, record

    def _unresolved_terminal_record(self, rd: Path) -> tuple[Optional[Path], Optional[dict]]:
        """Return the earliest retryable terminal command with an intact durable intent.

        ``failed``/``timed_out`` is only an observation result, not proof that an additive intent
        did not land.  The durable command may still reconcile from a late exact ack, or it may need
        an explicit same-id retry to drive the already-appended budget/fork/inject event.  A legacy
        caller has neither identity, so allowing it to append or spawn here would bypass that
        recovery boundary.

        Reconcile each candidate while the caller holds ``sequence(rd)``.  This deliberately does
        *not* block on rejected/pre-append/non-retryable failures, changed/missing intents, or a
        command whose late postcondition is now proven: those are safe terminal history, not a
        permanent compatibility lock.
        """
        directory = self._directory(rd)
        if not directory.exists():
            return None, None
        candidates = []
        for path in directory.glob("cmd_*.json"):
            if path.is_symlink():
                raise HTTPException(409, "run command record must not be a symlink")
            record = self._load(path)
            # Malformed and nonterminal records are handled fail-closed by _active_record.
            if record is None or record.get("status") not in {"failed", "timed_out"}:
                continue
            record = self._reconcile_observation(rd, path, record)
            if record.get("status") not in {"failed", "timed_out"}:
                continue
            if not bool((record.get("error") or {}).get("retryable")):
                continue
            command_id = str(record.get("id") or path.stem)
            if record.get("attached"):
                durable_intent = self._attached_finalize_intact(rd, record)
            else:
                durable_intent = self._find_intent(rd, command_id, record) is not None
            if not durable_intent:
                continue
            candidates.append((float(record.get("created_at") or 0), path.name, path, record))
        if not candidates:
            return None, None
        _created, _name, path, record = min(candidates, key=lambda item: (item[0], item[1]))
        return path, record

    def reject_if_active(self, rd: Path, operation: str, *,
                         allow_incomplete_finalize: bool = False) -> None:
        """Fail closed when a legacy mutation would overtake a durable command intent.

        Caller must hold ``sequence(rd)`` so the check and its own append/spawn are one ordering
        boundary.
        """
        pending_finalize = self._finalize_incomplete(rd)
        if pending_finalize and not allow_incomplete_finalize:
            raise HTTPException(409, {
                "code": "finalize_in_progress",
                "message": f"Cannot {operation} while terminal projections are incomplete.",
                "remediation": "Resume the finalization driver; do not append a legacy mutation.",
            })
        _path, active = self._active_record(rd)
        if active is not None:
            command_id = str(active.get("id") or "")
            raise HTTPException(409, {
                "code": "command_in_progress",
                "existing_command_id": command_id,
                "current_status": active.get("status"),
                "message": f"Cannot {operation} while another run command is in progress.",
                "remediation": f"GET /commands/{command_id} to a terminal status first.",
            })
        unresolved_path, unresolved = self._unresolved_terminal_record(rd)
        if unresolved is not None:
            command_id = str(unresolved.get("id") or (
                unresolved_path.stem if unresolved_path is not None else ""))
            raise HTTPException(409, {
                "code": "command_retry_required",
                "existing_command_id": command_id,
                "current_status": unresolved.get("status"),
                "message": (
                    f"Cannot {operation} while an earlier run command intent is unresolved."),
                "remediation": (
                    f"GET /commands/{command_id}; wait for late reconciliation or POST "
                    f"/commands/{command_id}/retry. Do not use a legacy mutation to bypass it."),
            })
        if self._recent_spawn_claim(rd):
            raise HTTPException(409, {
                "code": "engine_start_uncertain",
                "message": f"Cannot {operation} while an engine start is unresolved.",
                "remediation": "Wait for engine_running or definitive child exit; do not start another driver.",
            })

    @contextmanager
    def destructive_guard(self, rd: Path, operation: str):
        """Exclude submissions/workers while reset/delete performs an irreversible mutation."""
        # A paid UI call whose usage append failed deliberately retains a live activity claim and its
        # exact same-ID ledger. Give it one non-paid flush opportunity BEFORE taking ``sequence``:
        # successful flush closes the retained run_activity context, whose cleanup itself acquires
        # this sequencer. Calling the hook inside the block would deadlock. A persistent outage stays
        # fail-closed and the provider is never called by this accounting-only hook.
        flush_pending_cost = getattr(self.srv, "flush_pending_run_costs", None)
        if callable(flush_pending_cost):
            try:
                flushed = flush_pending_cost(rd)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - destructive mutation must fail closed
                raise HTTPException(
                    503, f"cannot {operation}: pending run-cost recovery failed") from exc
            if flushed is False:
                raise HTTPException(
                    409,
                    f"cannot {operation}: a paid call is waiting for durable run-cost accounting")
        with self.sequence(rd):
            active = self._active_command_ids(rd)
            if active:
                sample = ", ".join(active[:3])
                raise HTTPException(
                    409, f"cannot {operation}: run has active command(s) {sample}; wait for a terminal status")
            if self._recent_spawn_claim(rd):
                raise HTTPException(
                    409, f"cannot {operation}: an engine start is still in progress; wait for its lock/status")
            if self._finalize_incomplete(rd):
                raise HTTPException(
                    409, f"cannot {operation}: terminal projections are incomplete; resume finalization first")
            # Re-check the canonical path while holding the sequencer.  A run symlink swapped after
            # the route's initial run_dir() lookup must not redirect a destructive operation.
            canonical = self.srv.run_dir(rd.name)
            if canonical != rd.resolve():
                raise HTTPException(409, f"cannot {operation}: run path changed during validation")
            yield canonical

    @staticmethod
    def _payload(event_type: str, data: dict) -> tuple[bytes, str]:
        try:
            raw = json.dumps({"type": event_type, "data": data}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"command payload is not valid JSON: {exc}") from exc
        return raw, hashlib.sha256(raw).hexdigest()

    def _read_existing(self, path: Path) -> Optional[dict]:
        # New writers publish the complete record with atomic replacement while holding the run
        # sequencer. Heal a pre-upgrade empty O_EXCL reservation once it is old enough that its owner
        # cannot still hold this same sequencer; no intent/worker could start before the record save.
        if not path.exists():
            return None
        for _ in range(20):
            row = self._load(path)
            if row is not None:
                return row
            time.sleep(0.01)
        try:
            if path.stat().st_size == 0 and time.time() - path.stat().st_mtime > 1.0:
                path.unlink()
                return None
        except OSError:
            pass
        raise HTTPException(503, "command record is temporarily unavailable")

    def _check_duplicate(self, record: dict, key_digest: str, payload_digest: str) -> dict:
        if record.get("idempotency_key_digest") != key_digest:
            raise HTTPException(409, "idempotency command-id collision")
        if record.get("payload_digest") != payload_digest:
            raise HTTPException(409, "Idempotency-Key was already used with a different command payload")
        return record

    def _record_generation_match(self, rd: Path, record: dict) -> tuple[Optional[bool], str]:
        """Return True/False for a comparable record token, None for unbound legacy evidence."""
        stored = record.get("run_generation")
        if not isinstance(stored, str) or _RUN_GENERATION_RE.fullmatch(stored) is None:
            return None, self.run_generation(rd)
        current = self.run_generation(rd)
        return stored.lower() == current, current

    @staticmethod
    def _generation_changed_error(record: dict, current: str) -> dict:
        error = _error(
            "run_generation_changed",
            "The command belongs to an event-log generation that no longer occupies this run id.",
            "Observe this record only; refresh the run and form a new command for its current generation.",
            retryable=False,
        )
        error["expected_generation"] = record.get("run_generation")
        error["current_generation"] = current or None
        return error

    @staticmethod
    def _generation_unavailable_error(current: str) -> dict:
        error = _error(
            "run_generation_unavailable",
            "The legacy command record has no trustworthy event-log generation binding.",
            "Observe this record only; refresh the run and form a new generation-bound command.",
            retryable=False,
        )
        error["current_generation"] = current or None
        return error

    def _record_generation_error(self, record: dict, match: Optional[bool], current: str) -> dict:
        if match is False:
            return self._generation_changed_error(record, current)
        return self._generation_unavailable_error(current)

    def _terminal(self, path: Path, record: dict, status: str, *, error: Optional[dict] = None) -> dict:
        record = dict(record)
        record["status"] = status
        record["updated_at"] = time.time()
        if error is not None:
            record["error"] = error
        elif status in {"succeeded", "noop"}:
            record["error"] = None
        self._save(path, record)
        return record

    def _succeeded(self, rd: Path, path: Path, record: dict) -> dict:
        # Exact ack / terminal postcondition proves the spawned process passed its startup window.
        # Release only this command's lease so an immediate next command/finalize-resume is not held
        # behind a stale Popen claim; external/reset and other-command leases remain untouched.
        self._clear_spawn_claim(rd, str(record.get("id") or ""))
        return self._terminal(path, record, "succeeded")

    def _reconcile_observation(
            self, rd: Path, path: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> dict:
        """Promote a failed/timed-out record if its durable postcondition arrived later.

        GET is observation-only: it never appends or spawns.  A same-key POST may explicitly retry
        the existing command below; it reuses the marked intent and therefore cannot double-apply an
        additive budget/fork/inject request.
        """
        observation = observation or self._observe(rd)
        marked_invalid = (not record.get("attached") and record.get("event_seq") is not None
                          and self._find_intent(
                              rd, str(record.get("id") or ""), record, observation) is None)
        attached_invalid = bool(record.get("attached")
                                and not self._attached_finalize_intact(rd, record, observation))
        if marked_invalid or attached_invalid:
            return self._terminal(path, record, "failed", error=_error(
                "command_intent_missing",
                "the durable command record points to an intent that is missing or changed",
                "do not retry automatically; inspect/repair the event log and command record",
                retryable=False))
        status = record.get("status")
        if status not in {"failed", "timed_out"}:
            return record
        spec = CONTROL_SPECS.get(str(record.get("event_type") or ""))
        if spec is None:
            return record
        if self._postcondition(rd, record, observation):
            updated = dict(record)
            updated["reconciled_from"] = status
            return self._succeeded(rd, path, updated)
        if ((record.get("error") or {}).get("code") == "engine_start_uncertain"
                and not self._recent_spawn_claim(rd)):
            # GET remains observation-only: it does not restart anything. It merely makes the same
            # command explicitly retryable once lock evidence or definitive PID death removes the
            # duplicate-Popen hazard.
            updated = dict(record)
            updated["error"] = _error(
                "postcondition_timeout",
                f"command intent was recorded but {record.get('postcondition')} was not observed in time",
                "POST this command id's /retry endpoint; the prior engine start is no longer unresolved",
                retryable=True)
            updated["updated_at"] = time.time()
            self._save(path, updated)
            return updated
        return record

    def _safe_retry(self, rd: Path, path: Path, record: dict) -> dict:
        """Re-arm the SAME command id/key; never mint or append a second logical intent."""
        observation = self._observe(rd)
        record = self._reconcile_observation(rd, path, record, observation)
        if record.get("status") not in {"failed", "timed_out"}:
            return record
        updated = dict(record)
        updated["status"] = "accepted"
        updated["error"] = None
        updated["updated_at"] = time.time()
        updated["deadline_at"] = time.time() + self.command_timeout
        updated["absolute_deadline_at"] = time.time() + self.max_observation_timeout
        updated["observe_after_seq"] = observation.latest_seq
        updated["retry_count"] = int(updated.get("retry_count", 0)) + 1
        # A prior spawn no longer proves this retry has a driver.  Recovery must observe fresh domain
        # progress or claim a new spawn under the per-run sequencer.
        updated["spawned_by_command"] = False
        updated.pop("spawn_claim_released", None)
        updated.pop("engine_pid", None)
        updated.pop("startup_slow", None)
        updated.pop("waiting_for_spawn", None)
        self._save(path, updated)
        return updated

    @staticmethod
    def _comment_precondition(state, event_type: str, data: dict) -> Optional[dict]:
        """Recheck the exact semantic subject immediately before a collaboration append."""
        if event_type == EV_COMMENT_CREATED:
            node_id = data.get("node_id")
            generation = data.get("node_generation")
            node = state.nodes.get(node_id)
            if node is None or node.attempt != generation:
                current = getattr(node, "attempt", None)
                error = _error(
                    "node_generation_changed",
                    f"the comment target is no longer experiment #{node_id} generation {generation}",
                    "refresh the run and create a new comment against the current lifecycle")
                error["current_generation"] = current
                return error
            if data.get("comment_id") in state.comments:
                return _error(
                    "comment_id_conflict", "the allocated comment id is already present",
                    "submit a new command with a new idempotency key")
            # Count only MODERN comments (legacy EV_ANNOTATION notes are uncompactable in an append-only
            # log): this append-time recheck must match normalize_control's intake cap AND the fold in
            # comment_projection.apply_comment_event, or a heavily-annotated run accepts a comment at
            # intake then silently drops it here — the exact bug the modern-count cap fixes.
            modern_count = sum(1 for item in state.comments.values() if not item.legacy)
            if modern_count >= COMMENT_MAX_PER_RUN:
                return _error(
                    "comment_run_limit_reached",
                    f"this run already has {COMMENT_MAX_PER_RUN} projected comments",
                    "archive or compact comment history before creating more comments")
            per_subject = sum(
                1 for item in state.comments.values()
                if (not item.legacy and item.node_id == node_id
                    and item.node_generation == generation))
            if per_subject >= COMMENT_MAX_PER_NODE_GENERATION:
                return _error(
                    "comment_subject_limit_reached",
                    (f"experiment #{node_id} generation {generation} already has "
                     f"{COMMENT_MAX_PER_NODE_GENERATION} comments"),
                    "resolve or consolidate the existing discussion")
            return None
        comment_id = data.get("comment_id")
        comment = state.comments.get(comment_id)
        if comment is None or not comment.editable:
            return _error(
                "comment_not_found", "the editable comment no longer exists",
                "refresh the collaboration panel")
        if (comment.node_id != data.get("node_id")
                or comment.node_generation != data.get("node_generation")):
            return _error(
                "comment_subject_changed", "the comment subject identity no longer matches",
                "inspect the event history before retrying")
        expected = data.get("base_version")
        if comment.version != expected:
            error = _error(
                "comment_version_changed",
                f"comment {comment_id} is version {comment.version}, not {expected}",
                "refresh the comment and re-apply the edit to its current version")
            error["current_version"] = comment.version
            return error
        if comment.version >= COMMENT_MAX_VERSION or data.get("version") > COMMENT_MAX_VERSION:
            return _error(
                "comment_version_limit_reached",
                f"comment {comment_id} reached its {COMMENT_MAX_VERSION}-revision limit",
                "resolve it and create a concise follow-up comment")
        return None

    def _append_collaboration_intent(self, rd: Path, record: dict, event_data: dict
                                     ) -> tuple[Optional[Event], int, Optional[dict]]:
        """Strict-lock, bounded-CAS append for a versioned comment mutation.

        Engine/domain events may advance the shared log between our read and append.  Retry those
        unrelated tail races after refolding; reject only when the run/node/comment identity moved.
        """
        store = EventStore(self._events_path(rd))
        baseline = -1
        for _ in range(8):
            events = store.read_all()
            current_generation = run_generation_token(events)
            if current_generation != record.get("run_generation"):
                error = self._generation_changed_error(record, current_generation)
                return None, (events[-1].seq if events else -1), error
            state = fold(events)
            error = self._comment_precondition(state, str(record.get("event_type") or ""), event_data)
            if error is not None:
                return None, (events[-1].seq if events else -1), error
            baseline = events[-1].seq if events else -1
            try:
                intent = store.append(
                    str(record["event_type"]), event_data,
                    expected_last_seq=baseline, require_lock=True)
                return intent, baseline, None
            except EventStoreConcurrencyError:
                continue
            except EventStoreLockError as exc:
                return None, baseline, _error(
                    "event_lock_unavailable", str(exc),
                    "restore cross-process file locking, then retry this exact command id",
                    retryable=True)
        return None, baseline, _error(
            "comment_concurrency_busy",
            "the event log kept changing while the comment version was being verified",
            "retry this exact command id after the run produces less event traffic",
            retryable=True)

    def _decision(self, rd: Path, event_type: str) -> tuple[str, Optional[dict]]:
        # Comments never wake or steer the engine.  Requiring a readable engine.lock here would make
        # collaboration unavailable precisely when storage ownership diagnostics are degraded, even
        # though the strict event append lock below is the only ownership guarantee this write needs.
        if event_type in COLLABORATION_EVENTS:
            return "append", None
        state = self.srv.state(rd)
        liveness = self._engine_state(rd)
        if liveness is None:
            return "reject", self._engine_unknown_error(f"apply {event_type}")
        alive = liveness is True
        pending_finalize = self._finalize_incomplete(rd, state)

        if event_type == EV_RUN_ABORT:
            if state.finished and alive:
                return "reject", _error(
                    "engine_finishing", "the engine is still completing its terminal write-out",
                    "retry after engine_running becomes false", retryable=True)
            if pending_finalize:
                return "attach", None
            if state.finished and str(state.stop_reason or "").lower() != "error":
                return "noop", None
            return "append", None
        if event_type == EV_PAUSE:
            if pending_finalize:
                return "reject", _error(
                    "finalize_in_progress", "cannot stop a run while finalization is pending",
                    "wait for finalization to finish; its command record remains observable",
                    retryable=True)
            if state.finished or (state.paused and not alive):
                return "noop", None
            return "append", None
        if event_type in {EV_RESUME, EV_RUN_REOPENED}:
            if pending_finalize:
                return "reject", _error(
                    "finalize_in_progress", "cannot resume while finalization is pending",
                    "wait for finalization to finish, then submit a new resume command",
                    retryable=True)
            if state.finished and alive:
                return "reject", _error(
                    "engine_finishing", "the engine is still completing its terminal write-out",
                    "retry after engine_running becomes false", retryable=True)
            if alive and not state.paused and not state.finished:
                return "noop", None
            return "append", None
        if event_type == EV_APPROVAL_GRANTED:
            if not state.awaiting_approval:
                return "reject", _error(
                    "approval_not_requested", "the run is not awaiting result approval",
                    "approve only while the run phase is approval")
        if event_type == EV_SPEC_APPROVED:
            if not state.spec_approval_requested or state.spec_confirmed:
                return "reject", _error(
                    "ratification_not_requested", "the run is not awaiting eval-spec ratification",
                    "ratify only while the run phase is spec_approval")
        spec = CONTROL_SPECS[event_type]
        if pending_finalize and spec.engine_policy is not EnginePolicy.NO_SPAWN:
            return "reject", _error(
                "finalize_in_progress", f"cannot apply {event_type} while finalization is pending",
                "wait for finalization to finish before submitting engine-driving work",
                retryable=True)
        if state.finished and alive and spec.engine_policy is EnginePolicy.ENSURE_RUNNING:
            return "reject", _error(
                "engine_finishing", "the engine is still completing its terminal write-out",
                "retry after engine_running becomes false", retryable=True)
        return "append", None

    def submit(self, rd: Path, idempotency_key: str, event_type: str, data,
               *, expected_generation: object = None) -> dict:
        key = str(idempotency_key or "")
        if not key or len(key) > 512:
            raise HTTPException(400, "Idempotency-Key is required and must be at most 512 characters")
        if not isinstance(event_type, str):
            raise HTTPException(400, "command type must be a string")
        raw_data = {} if data is None else data
        if not isinstance(raw_data, dict):
            raise HTTPException(400, "command data must be a JSON object")
        _raw, payload_digest = self._payload(event_type, raw_data)
        # The precondition itself is a strict wire contract even for an idempotent replay. A valid
        # stale token may resolve an existing same-key record below, but missing/malformed input must
        # never be silently accepted just because a record happens to exist.
        expected = _normalize_expected_generation(expected_generation)
        key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        command_id = "cmd_" + key_digest[:32]
        path = self._path(rd, command_id)

        should_start = False
        synchronous = False
        with self.sequence(rd):
            existing = self._read_existing(path)
            if existing is not None:
                record = self._check_duplicate(existing, key_digest, payload_digest)
                generation_match, current_generation = self._record_generation_match(rd, record)
                if generation_match is not True:
                    if record.get("status") not in TERMINAL_STATUSES:
                        # An externally replaced log or crash-recovery edge can leave an old
                        # nonterminal record even though normal reset guards exclude it. Make it
                        # observable but inert; GET/same-key POST must never wake a worker in B.
                        record = self._terminal(
                            path, record, "failed",
                            error=self._record_generation_error(
                                record, generation_match, current_generation))
                    # A terminal A record remains byte-for-byte semantic history. Do not reconcile
                    # its intent/postcondition against B and rewrite a successful lost-response
                    # replay as command_intent_missing.
                else:
                    record = self._reconcile_observation(rd, path, record)
            else:
                # Idempotency lookup intentionally wins over this precondition: after an in-place
                # reset, replaying a lost response with the SAME key/payload must resolve the old
                # durable record, never reject it or apply it to the replacement generation. Only a
                # genuinely brand-new record is bound to the generation observed by its caller.
                current_generation = self.run_generation(rd)
                if not current_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_unavailable",
                        "message": "The run has no durable generation identity yet.",
                        "remediation": (
                            "Wait for run_started, refresh GET /state, and submit a new command with "
                            "the returned generation."),
                    })
                if expected != current_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_changed",
                        "expected_generation": expected,
                        "current_generation": current_generation,
                        "message": "The run was reset or replaced after this command was formed.",
                        "remediation": (
                            "Refresh the run, review its current state, and form a new command with "
                            "a new idempotency key and current generation."),
                    })
                normalized_candidate = None
                semantic_candidate = None
                normalization_error = None
                gate_field = ({EV_APPROVAL_GRANTED: "approval_request_seq",
                               EV_SPEC_APPROVED: "spec_approval_request_seq"}.get(event_type))
                gate_before = (getattr(self.srv.state(rd), gate_field, None)
                               if gate_field is not None else None)
                try:
                    normalized_candidate = normalize_control(
                        self.srv, rd, event_type, raw_data)
                    if gate_field is not None:
                        gate_after = getattr(self.srv.state(rd), gate_field, None)
                        if (not isinstance(gate_before, int) or isinstance(gate_before, bool)
                                or gate_before < 0 or gate_after != gate_before):
                            raise HTTPException(409, {
                                "code": "approval_state_changed",
                                "message": "the approval request changed while the command was admitted",
                                "remediation": "refresh the run and submit a new approval command",
                                "retryable": False,
                            })
                    _semantic_raw, semantic_candidate = self._payload(
                        event_type, normalized_candidate)
                except HTTPException as exc:
                    normalization_error = exc
                # Reload recovery is special for FINALIZE: the browser may have lost its generated
                # key/command id, while the durable stop intent is still pending. Return the existing
                # record so it can resume polling; never mint an alias record, event, or second driver.
                reattach_path = reattach = None
                if event_type == EV_RUN_ABORT and semantic_candidate is not None:
                    reattach_path, reattach = self._pending_finalize_record(
                        rd, semantic_candidate)
                    if reattach is None:
                        _other_path, other_finalize = self._pending_finalize_record(rd)
                        if other_finalize is not None:
                            existing_id = str(other_finalize.get("id") or "")
                            raise HTTPException(409, {
                                "code": "finalize_payload_conflict",
                                "existing_command_id": existing_id,
                                "message": "A finalize with different normalized data is unresolved.",
                                "remediation": f"GET /commands/{existing_id}; do not alias another reason.",
                            })
                if reattach is not None and reattach_path is not None:
                    path = reattach_path
                    record = self._reconcile_observation(rd, path, reattach)
                else:
                    if event_type not in COLLABORATION_EVENTS and self._recent_spawn_claim(rd):
                        raise HTTPException(409, {
                            "code": "engine_start_uncertain",
                            "message": "An earlier engine start has not exposed its lock or exited.",
                            "remediation": (
                                "Wait for engine_running or definitive child exit; do not submit "
                                "another state-changing command."),
                        })
                    # A fresh key must not double-apply any unresolved control intent. Unlike
                    # finalize recovery above, the caller must name and explicitly retry the original
                    # failed/timed-out command; a silent alias would hide which intent is authoritative.
                    equivalent_path = equivalent = None
                    if semantic_candidate is not None:
                        equivalent_path, equivalent = self._unresolved_equivalent(
                            rd, event_type, semantic_candidate)
                    if equivalent is not None and equivalent_path is not None:
                        existing_id = str(equivalent.get("id") or "")
                        raise HTTPException(
                            409, {
                                "code": "retry_existing_command",
                                "existing_command_id": existing_id,
                                "message": "An unresolved identical control intent already exists.",
                                "remediation": (
                                    f"GET /commands/{existing_id}; if it is retryable failed/timed_out, "
                                    f"POST /commands/{existing_id}/retry."),
                            })
                    _active_path, active = self._active_record(rd)
                    if active is not None:
                        existing_id = str(active.get("id") or "")
                        raise HTTPException(409, {
                            "code": "command_in_progress",
                            "existing_command_id": existing_id,
                            "current_status": active.get("status"),
                            "message": "Another state-changing run command is still in progress.",
                            "remediation": (
                                f"GET /commands/{existing_id} to a terminal status before submitting "
                                "the next command."),
                        })
                    now = time.time()
                    record = {
                        "id": command_id,
                        "status": "accepted",
                        "event_type": event_type,
                        "error": None,
                        "data": {},
                        "idempotency_key_digest": key_digest,
                        "payload_digest": payload_digest,
                        "run_generation": current_generation,
                        "created_at": now,
                        "updated_at": now,
                        "deadline_at": now + self.command_timeout,
                        "absolute_deadline_at": now + self.max_observation_timeout,
                        "driver_was_alive": (None if event_type in COLLABORATION_EVENTS
                                             else self._engine_state(rd)),
                    }
                    try:
                        if normalization_error is not None:
                            raise normalization_error
                        normalized = dict(normalized_candidate or {})
                        record["data"] = normalized
                        if gate_field is not None:
                            record["approval_gate_field"] = gate_field
                            record["approval_gate_seq"] = gate_before
                        _semantic_raw, record["semantic_payload_digest"] = self._payload(
                            event_type, normalized)
                        record["engine_policy"] = CONTROL_SPECS[event_type].engine_policy.value
                        record["postcondition"] = CONTROL_SPECS[event_type].postcondition
                        decision, err = self._decision(rd, event_type)
                        if decision == "reject":
                            record["status"] = "rejected"
                            record["error"] = err
                        elif decision == "noop":
                            record["status"] = "noop"
                        elif decision == "attach":
                            pending_event, pending_digest = self._pending_finalize_intent(rd)
                            if pending_event is None or not pending_digest:
                                record["status"] = "rejected"
                                record["error"] = _error(
                                    "command_intent_missing",
                                    "the pending external finalize intent is missing or malformed",
                                    "inspect/repair the event log; do not infer completion",
                                    retryable=False)
                            elif pending_digest != record["semantic_payload_digest"]:
                                record["status"] = "rejected"
                                record["error"] = _error(
                                    "finalize_payload_conflict",
                                    "a pending external finalize has different normalized data",
                                    "observe the existing finalize; do not alias another reason",
                                    retryable=False)
                            else:
                                record["attached"] = True
                                record["attached_event_seq"] = pending_event.seq
                                record["attached_semantic_payload_digest"] = pending_digest
                    except HTTPException as exc:
                        record["status"] = "rejected"
                        detail = exc.detail
                        if (isinstance(detail, dict) and detail.get("code")
                                and detail.get("message")):
                            safe_error = _error(
                                str(detail["code"]), str(detail["message"]),
                                str(detail.get("remediation") or ""),
                                retryable=bool(detail.get("retryable", False)))
                            # Expose only the bounded numeric CAS value needed to recover from a
                            # stale comment edit. Arbitrary exception-detail fields stay excluded.
                            current_version = detail.get("current_version")
                            if (isinstance(current_version, int)
                                    and not isinstance(current_version, bool)
                                    and 1 <= current_version <= COMMENT_MAX_VERSION):
                                safe_error["current_version"] = current_version
                            record["error"] = safe_error
                        else:
                            record["error"] = _error(
                                "invalid_command" if exc.status_code < 404 else "command_target_not_found",
                                str(detail),
                                "correct the command payload and submit it with a new idempotency key")

                    # The cross-process sequencer already excludes competing creators. Atomic replace
                    # publishes either no record or the complete record, never an immortal empty
                    # reservation after a process crash.
                    self._save(path, record)

            should_start = record.get("status") not in TERMINAL_STATUSES
            if should_start:
                spec = CONTROL_SPECS[str(record["event_type"])]
                synchronous = spec.engine_policy is EnginePolicy.NO_SPAWN and record["event_type"] != EV_PAUSE

        if should_start:
            if synchronous and self._claim_execution(rd, str(record["id"])):
                self._execute(rd, path, record, claimed=True)
            else:
                self._start_worker(rd, path, record)
        result = self._public(self._load(path) or record)
        # A collaboration append promises strict cross-process serialization. Surface the missing
        # guarantee as HTTP 503 (while retaining the durable command id for explicit same-intent
        # recovery) instead of returning an ordinary failed 200 record.
        if (event_type in COLLABORATION_EVENTS and result.get("status") == "failed"
                and (result.get("error") or {}).get("code") == "event_lock_unavailable"):
            detail = dict(result["error"])
            detail["command_id"] = result.get("id")
            raise HTTPException(503, detail)
        return result

    def retry(self, rd: Path, command_id: str) -> dict:
        path = self._path(rd, command_id)
        with self.sequence(rd):
            record = self._read_existing(path)
            if record is None:
                raise HTTPException(404, "no such command")
            generation_match, current_generation = self._record_generation_match(rd, record)
            if generation_match is not True:
                detail = self._record_generation_error(record, generation_match, current_generation)
                detail.update({
                    "existing_command_id": command_id,
                    "current_status": record.get("status"),
                })
                raise HTTPException(409, detail)
            record = self._reconcile_observation(rd, path, record)
            if record.get("status") == "succeeded":
                return self._public(record)
            if (record.get("event_type") not in COLLABORATION_EVENTS
                    and self._recent_spawn_claim(rd)):
                raise HTTPException(409, {
                    "code": "engine_start_uncertain",
                    "existing_command_id": command_id,
                    "current_status": record.get("status"),
                    "message": "The prior detached engine may still be starting without a live lock.",
                    "remediation": "Wait for engine_running or definitive child exit; retry must not Popen yet.",
                })
            if (record.get("status") not in {"failed", "timed_out"}
                    or not bool((record.get("error") or {}).get("retryable"))):
                raise HTTPException(409, {
                    "code": "command_not_retryable",
                    "existing_command_id": command_id,
                    "current_status": record.get("status"),
                    "message": "Only retryable failed/timed_out commands can be retried.",
                    "remediation": f"GET /commands/{command_id} and observe its current status.",
                })
            _active_path, active = self._active_record(rd)
            if active is not None and str(active.get("id") or "") != command_id:
                active_id = str(active.get("id") or "")
                raise HTTPException(409, {
                    "code": "command_in_progress",
                    "existing_command_id": active_id,
                    "current_status": active.get("status"),
                    "message": "Another state-changing run command is still in progress.",
                    "remediation": (
                        f"GET /commands/{active_id} to a terminal status before retrying {command_id}."),
                })
            record = self._safe_retry(rd, path, record)
        if record.get("event_type") in COLLABORATION_EVENTS \
                and self._claim_execution(rd, str(record["id"])):
            self._execute(rd, path, record, claimed=True)
        else:
            self._start_worker(rd, path, record)
        result = self._public(self._load(path) or record)
        if (record.get("event_type") in COLLABORATION_EVENTS and result.get("status") == "failed"
                and (result.get("error") or {}).get("code") == "event_lock_unavailable"):
            detail = dict(result["error"])
            detail["command_id"] = result.get("id")
            raise HTTPException(503, detail)
        return result

    def get(self, rd: Path, command_id: str) -> dict:
        path = self._path(rd, command_id)
        with self.sequence(rd):
            record = self._read_existing(path)
            if record is None:
                raise HTTPException(404, "no such command")
            generation_match, current_generation = self._record_generation_match(rd, record)
            if generation_match is not True:
                if record.get("status") not in TERMINAL_STATUSES:
                    record = self._terminal(
                        path, record, "failed",
                        error=self._record_generation_error(
                            record, generation_match, current_generation))
                # Terminal cross-generation/legacy records are observation-only history.
            else:
                record = self._reconcile_observation(rd, path, record)
        if record.get("status") not in TERMINAL_STATUSES:
            self._start_worker(rd, path, record)
            record = self._load(path) or record
        return self._public(record)

    def _claim_execution(self, rd: Path, command_id: str) -> bool:
        lock = self._exec_path(rd, command_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        owner = {"pid": os.getpid(), "created_at": time.time()}
        try:
            identity = self.process_identity(os.getpid())
        except Exception:  # noqa: BLE001 - identity is an optional hardening token
            identity = None
        if identity:
            owner["process_identity"] = identity

        def publish() -> bool:
            # Publish a fully-written inode with one exclusive hard-link CAS. A hard kill can leave
            # an unreferenced temp, but never an empty/partial authoritative `.executing` claim.
            temp = lock.with_name(f".{lock.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
            try:
                self._save(temp, owner)
                try:
                    os.link(temp, lock)
                    return True
                except FileExistsError:
                    return False
                except OSError:
                    if lock.exists():
                        return False
                    # Some network/FAT filesystems cannot hard-link. Preserve functionality with a
                    # short O_EXCL write; any kill inside this fallback is recoverable through the
                    # explicit active-claim resolver rather than becoming a permanent deadlock.
                    try:
                        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    except FileExistsError:
                        return False
                    try:
                        raw = json.dumps(owner, sort_keys=True).encode("utf-8")
                        written = 0
                        while written < len(raw):
                            written += os.write(fd, raw[written:])
                        try:
                            os.fsync(fd)
                        except OSError:
                            pass
                    except BaseException:
                        try:
                            lock.unlink()
                        except OSError:
                            pass
                        raise
                    finally:
                        os.close(fd)
                    return True
            finally:
                try:
                    temp.unlink()
                except OSError:
                    pass

        if not publish():
            # A missed heartbeat can mean suspension, not death. Reclaim only after the full command
            # deadline and positive evidence that the owning process exited or its PID was reused.
            try:
                owner_gone = self._execution_owner_definitely_gone(lock)
                if (time.time() - lock.stat().st_mtime <= self.command_timeout + 30
                        and not owner_gone):
                    return False
                if not owner_gone:
                    return False
                lock.unlink()
                if not publish():
                    return False
            except (OSError, FileExistsError):
                return False
        return True

    def _release_execution(self, rd: Path, command_id: str) -> None:
        try:
            self._exec_path(rd, command_id).unlink()
        except OSError:
            pass

    def _heartbeat_execution(self, rd: Path, command_id: str) -> None:
        try:
            os.utime(self._exec_path(rd, command_id), None)
        except OSError:
            pass

    def _start_worker(self, rd: Path, path: Path, record: dict) -> None:
        command_id = str(record.get("id") or "")
        if not self._claim_execution(rd, command_id):
            return
        thread = threading.Thread(
            target=self._execute, args=(rd, path, record), kwargs={"claimed": True},
            daemon=True, name=f"looplab-{command_id[:20]}")
        try:
            thread.start()
        except BaseException:
            # A live-owner claim with no worker would otherwise block recovery until PID reuse/death.
            self._release_execution(rd, command_id)
            raise

    def _events(self, rd: Path):
        return EventStore(self._events_path(rd)).read_all()

    def _observe(self, rd: Path) -> CommandObservation:
        return self._command_observations.observe(self._events_path(rd))

    def _find_intent(
            self, rd: Path, command_id: str, record: Optional[dict] = None,
            observation: Optional[CommandObservation] = None):
        """Return the one exact marked intent, not merely any event carrying the marker.

        The record's sequence, event type, and normalized semantic payload are all part of durable
        command identity. Log repair/rewrite that preserves only ``_command_id`` must never satisfy a
        folded-intent postcondition or make a stale command_ack look causal.
        """
        observation = observation or self._observe(rd)
        event = observation.marked_intent(command_id)
        if event is None:
            return None
        if record is None:
            return event
        expected_seq = record.get("event_seq")
        if expected_seq is not None and event.seq != expected_seq:
            return None
        event_type = str(record.get("event_type") or "")
        if event.type != event_type:
            return None
        actual_data = dict(event.data or {})
        actual_data.pop("_command_id", None)
        expected_digest = record.get("semantic_payload_digest")
        if expected_digest:
            try:
                _raw, actual_digest = self._payload(event_type, actual_data)
            except HTTPException:
                return None
            if actual_digest != expected_digest:
                return None
        elif actual_data != (record.get("data") or {}):
            return None
        return event

    def _domain_progress(
            self, rd: Path, after_seq: int,
            observation: Optional[CommandObservation] = None) -> bool:
        observation = observation or self._observe(rd)
        return observation.has_domain_progress(after_seq)

    @staticmethod
    def _observe_after(record: dict) -> int:
        return int(record.get(
            "observe_after_seq", record.get("event_seq", record.get("baseline_seq", -1))))

    def _domain_failure(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> Optional[dict]:
        after = self._observe_after(record)
        observation = observation or self._observe(rd)
        event = observation.domain_failure_after(after)
        if event is not None:
            detail = str((event.data or {}).get("error") or "engine exited with an error")
            return _error(
                "engine_failed", detail[:500],
                "correct the run error, then POST this command id's /retry endpoint",
                retryable=True)
        return None

    def _spawn(self, rd: Path) -> Optional[int]:
        task_file = task_file_for(rd)
        if not task_file:
            raise RuntimeError("run has no task.snapshot.json or usable ui_meta.json")
        # The CLI's resume path is stop-aware: it preserves a pending run_abort and appends EV_RESUME
        # only for ordinary paused/finished continuation.  Never append run_reopened here.
        return self.spawn_engine(["resume", str(rd), "--task-file", str(task_file)], run_dir=rd)

    def _postcondition(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> bool:
        observation = observation or self._observe(rd)
        kind = record.get("postcondition")
        if (record.get("attached")
                and not self._attached_finalize_intact(rd, record, observation)):
            return False
        if (not record.get("attached") and record.get("event_seq") is not None
                and self._find_intent(
                    rd, str(record.get("id") or ""), record, observation) is None):
            return False
        if kind == "folded_intent":
            intent = self._find_intent(rd, str(record["id"]), record, observation)
            if intent is None:
                return False
            observation.state()  # prove the complete log, including the marked intent, still folds
            return True
        if kind == "paused_and_stopped":
            state = observation.state()
            return bool(state.paused and self._engine_state(rd) is False)
        if kind == "finished_and_stopped":
            state = observation.state()
            if (not state.finished or self._engine_state(rd) is not False
                    or str(state.stop_reason or "").lower() == "error"):
                return False
            if not state.stop_requested:
                return False
            # New-format engines publish this only after cost/reflection/read-model/trace/tree are
            # complete. A legacy terminal event has no explicit scope and stays backward compatible.
            if (observation.incomplete_finalize_scope() is not None
                    or state.finalization_pending()):
                return False
            # An attached record observes an external/legacy finalize rather than owning a marked
            # intent. Once replay says that same stop is non-error finished and the driver released
            # its lock, the effect is satisfied even if completion raced command-record creation.
            if record.get("attached"):
                return True
            # Do not let an old natural/error finish satisfy a newly attached finalize.  On retry,
            # ``observe_after_seq`` advances past the failed attempt, so success requires a fresh,
            # non-error run_finished causally after that boundary.
            after = self._observe_after(record)
            if observation.has_non_error_finish_after(after):
                return True
            # Decision→append race: natural completion can land after the preflight baseline but just
            # before this command's run_abort intent. It is still the terminal attempt this finalize
            # observed; requiring another finish would reopen/extend an already completed run.
            try:
                baseline = int(record.get("baseline_seq", -1))
            except (TypeError, ValueError, OverflowError):
                baseline = -1
            return (record.get("event_type") == EV_RUN_ABORT
                    and observation.has_non_error_finish_after(baseline))
        if kind == "engine_ack":
            command_id = str(record.get("id") or "")
            event_seq = record.get("event_seq")
            return observation.has_ack(command_id, event_seq)
        return False

    def _driver_or_progress(
            self, rd: Path, record: dict,
            observation: Optional[CommandObservation] = None) -> bool:
        if record.get("spawned_by_command") and self._engine_state(rd) is True:
            return True
        return self._postcondition(rd, record, observation)

    def _execute(self, rd: Path, path: Path, initial: dict, *, claimed: bool) -> None:
        record = self._load(path) or dict(initial)
        command_id = str(record.get("id") or "")
        if record.get("absolute_deadline_at") is None:
            record["absolute_deadline_at"] = time.time() + self.max_observation_timeout
        sequence_ctx = None
        sequence_held = False
        try:
            if record.get("status") in TERMINAL_STATUSES:
                return
            sequence_ctx = self.sequence(rd)
            sequence_ctx.__enter__()
            sequence_held = True
            # Another process may have completed/rejected it while this worker waited for the run.
            record = self._load(path) or record
            if record.get("status") in TERMINAL_STATUSES:
                return
            event_type = str(record.get("event_type") or "")
            spec = CONTROL_SPECS.get(event_type)
            if spec is None:
                self._terminal(path, record, "rejected", error=_error(
                    "invalid_command", f"unknown control event: {event_type!r}"))
                return

            observation = self._observe(rd)
            intent = self._find_intent(rd, command_id, record, observation)
            decision_baseline = None
            if (record.get("attached")
                    and not self._attached_finalize_intact(rd, record, observation)):
                self._terminal(path, record, "failed", error=_error(
                    "command_intent_missing",
                    "the attached external finalize intent is missing or changed",
                    "do not retry automatically; inspect/repair the event log and command record",
                    retryable=False))
                return
            recorded_event_seq = record.get("event_seq")
            if recorded_event_seq is not None and (
                    intent is None or intent.seq != recorded_event_seq):
                self._terminal(path, record, "failed", error=_error(
                    "command_intent_missing",
                    "the durable command record points to a marked intent that is missing or changed",
                    "do not retry automatically; inspect/repair the event log and command record",
                    retryable=False))
                return
            # Once this command's marked intent is durable, never re-run state preflight during
            # recovery.  Its own fold may have cleared an approval gate or satisfied pause; treating
            # that changed state as a fresh submission would incorrectly turn a succeeded command
            # into rejected/noop after a server restart.
            if record.get("attached"):
                # Attachment is already a durable intent identity even though it deliberately has no
                # command marker. Never re-run fresh-state admission and accidentally append a second
                # finalize if another external event superseded it after record creation.
                decision = "already_attached"
            elif intent is None:
                gate_field = record.get("approval_gate_field")
                if gate_field in {"approval_request_seq", "spec_approval_request_seq"}:
                    admitted_gate = record.get("approval_gate_seq")
                    current_gate = getattr(self.srv.state(rd), gate_field, None)
                    if current_gate != admitted_gate:
                        self._terminal(path, record, "rejected", error=_error(
                            "approval_state_changed",
                            "the approval request changed before the intent could be recorded",
                            "refresh the run and submit a new approval command", retryable=False))
                        return
                # Capture causality BEFORE folding state for the decision. In particular, an engine
                # can complete an externally-appended finalize after `_decision` observes pending but
                # before this worker continues; that run_finished must remain *after* the attach
                # baseline so it satisfies the command instead of being hidden inside the baseline.
                before_decision = self._events(rd)
                decision_baseline = before_decision[-1].seq if before_decision else -1
                decision, err = self._decision(rd, event_type)
                if decision == "reject":
                    self._terminal(path, record, "rejected", error=err)
                    return
                if decision == "noop":
                    self._terminal(path, record, "noop")
                    return
            else:
                decision = "already_appended"
            if decision == "append" and intent is None:
                record["baseline_seq"] = decision_baseline
                event_data = dict(record.get("data") or {})
                event_data["_command_id"] = command_id
                if event_type in COLLABORATION_EVENTS:
                    intent, decision_baseline, append_error = self._append_collaboration_intent(
                        rd, record, event_data)
                    if append_error is not None:
                        status = "failed" if append_error.get("retryable") else "rejected"
                        self._terminal(path, record, status, error=append_error)
                        return
                else:
                    store = EventStore(self._events_path(rd))
                if event_type in {EV_APPROVAL_GRANTED, EV_SPEC_APPROVED}:
                    # Approval is valid only against the exact decision snapshot. The per-run command
                    # sequencer does not exclude the engine/CLI, so an external grant/reset can land
                    # after `_decision`; append with CAS and re-evaluate instead of double-approving.
                    try:
                        intent = store.append(
                            event_type, event_data, expected_last_seq=decision_baseline)
                    except EventStoreConcurrencyError:
                        self._terminal(path, record, "rejected", error=_error(
                            "approval_state_changed",
                            "the approval state changed before the intent could be recorded",
                            "refresh the run and submit a new approval command", retryable=False))
                        return
                elif event_type not in COLLABORATION_EVENTS:
                    intent = store.append(event_type, event_data)
                record["baseline_seq"] = decision_baseline
            if intent is not None:
                record["event_seq"] = intent.seq
            elif "baseline_seq" not in record:
                if decision_baseline is None:
                    before = self._events(rd)
                    decision_baseline = before[-1].seq if before else -1
                record["baseline_seq"] = decision_baseline
            record["status"] = "executing"
            record["updated_at"] = time.time()
            self._save(path, record)

            observation = self._observe(rd)
            if self._postcondition(rd, record, observation):
                self._succeeded(rd, path, record)
                return
            domain_error = (self._domain_failure(rd, record, observation)
                            if spec.engine_policy is not EnginePolicy.NO_SPAWN else None)
            if domain_error is not None:
                self._clear_spawn_claim(rd, command_id)
                self._terminal(path, record, "failed", error=domain_error)
                return

            liveness = self._engine_state(rd)
            if spec.engine_policy is not EnginePolicy.NO_SPAWN and liveness is None:
                self._terminal(
                    path, record, "failed",
                    error=self._engine_unknown_error(
                        f"start a driver for {event_type}", retryable=True))
                return
            if spec.engine_policy is not EnginePolicy.NO_SPAWN and liveness is False:
                spawned_now = False
                if self._recent_spawn_claim(rd):
                    record["waiting_for_spawn"] = True
                    record["deadline_at"] = max(
                        float(record["deadline_at"]), time.time() + self.startup_timeout * 2 + 1)
                    self._save(path, record)
                else:
                    # Write the lease *before* Popen. If the server dies after process creation but
                    # before it can persist the PID, another server still waits for engine.lock
                    # instead of launching a second engine into the same run.
                    self._record_spawn_claim(rd, command_id, None)
                    try:
                        pid = self._spawn(rd)
                    except Exception as exc:  # noqa: BLE001 - Popen/task failures become records
                        self._clear_spawn_claim(rd, command_id)
                        self._terminal(path, record, "failed", error=_error(
                            "spawn_failed", f"could not start the run engine: {exc}",
                            "fix the cause, then POST this command id's /retry endpoint (same intent)",
                            retryable=True))
                        return
                    spawned_now = True
                    record["spawned_by_command"] = True
                    record["waiting_for_spawn"] = False
                    if pid is not None:
                        record["engine_pid"] = pid
                    record["updated_at"] = time.time()
                    self._record_spawn_claim(rd, command_id, pid)
                    self._save(path, record)

                if spawned_now:
                    startup_deadline = min(
                        float(record["deadline_at"]), time.time() + self.startup_timeout)
                    while time.time() < startup_deadline:
                        observation = self._observe(rd)
                        if self._postcondition(rd, record, observation):
                            if spec.engine_policy is EnginePolicy.ENSURE_RUNNING:
                                self._succeeded(rd, path, record)
                                return
                            break
                        domain_error = self._domain_failure(rd, record, observation)
                        if domain_error is not None:
                            self._clear_spawn_claim(rd, command_id)
                            self._terminal(path, record, "failed", error=domain_error)
                            return
                        # Lock is startup evidence only. ENSURE_RUNNING stays executing until the
                        # exact command_ack arrives; finalize waits for finished + dead.
                        if self._engine_state(rd) is True:
                            self._clear_spawn_claim(rd, command_id)
                            record["spawn_claim_released"] = True
                            break
                        time.sleep(self.poll_interval)
                    else:
                        # The detached PID may still be alive in a cold import before engine.lock.
                        # Keep the pre-Popen lease and let the bounded command monitor wait until its
                        # absolute deadline; never declare failure + clear the only anti-double-spawn
                        # evidence merely because the short UX startup window elapsed.
                        record["startup_slow"] = True
                        record["deadline_at"] = float(record.get(
                            "absolute_deadline_at", record["deadline_at"]))
                        record["updated_at"] = time.time()
                        self._save(path, record)

            sequence_ctx.__exit__(None, None, None)
            sequence_held = False

            observation = self._observe(rd)
            if (spec.engine_policy is EnginePolicy.ENSURE_RUNNING
                    and self._postcondition(rd, record, observation)):
                self._succeeded(rd, path, record)
                return

            if record.get("last_progress_seq") is None:
                last_progress_seq = observation.latest_seq
            else:
                last_progress_seq = int(record.get("last_progress_seq", -1))
            while True:
                self._heartbeat_execution(rd, command_id)
                observation = self._observe(rd)
                if self._postcondition(rd, record, observation):
                    self._succeeded(rd, path, record)
                    return
                domain_error = (self._domain_failure(rd, record, observation)
                                if spec.engine_policy is not EnginePolicy.NO_SPAWN else None)
                if domain_error is not None:
                    self._clear_spawn_claim(rd, command_id)
                    self._terminal(path, record, "failed", error=domain_error)
                    return

                now = time.time()
                latest_seq = observation.latest_seq
                liveness = self._engine_state(rd)
                alive = liveness is True
                if (alive and record.get("spawned_by_command")
                        and not record.get("spawn_claim_released")):
                    self._clear_spawn_claim(rd, command_id)
                    record["spawn_claim_released"] = True
                # Pause/finalize may legitimately wait through one long evaluation or wrap-up. A
                # live lock or fresh event progress slides their observation deadline; an actually
                # stalled/dead driver still reaches a terminal timeout.
                if ((record.get("postcondition") in {"paused_and_stopped", "finished_and_stopped"}
                     or spec.engine_policy is not EnginePolicy.NO_SPAWN)
                        and (alive or latest_seq > last_progress_seq)):
                    last_progress_seq = latest_seq
                    record["last_progress_seq"] = latest_seq
                    # Never shrink a longer Popen→engine.lock lease installed above. Fresh progress
                    # extends a normal observation deadline, while an in-flight spawn keeps its full
                    # startup window even if this is the monitor's first pass.
                    record["deadline_at"] = max(
                        float(record.get("deadline_at") or 0), now + self.command_timeout)
                    record["deadline_at"] = min(
                        float(record.get("absolute_deadline_at") or record["deadline_at"]),
                        float(record["deadline_at"]))
                    record["updated_at"] = now
                    self._save(path, record)

                if spec.engine_policy is not EnginePolicy.NO_SPAWN and liveness is None:
                    # Preserve any extant spawn claim: an inaccessible lock may belong to the child
                    # we launched, so terminalize observably but never clear/retry into a duplicate.
                    self._terminal(
                        path, record, "failed",
                        error=self._engine_unknown_error(
                            f"continue driving {event_type}", retryable=True))
                    return

                # Check the bounded absolute deadline before considering another Popen. A slow child
                # owns its lease through this boundary; expiry must terminalize the command first,
                # not launch a second child in the same monitor iteration.
                absolute_deadline = float(record.get(
                    "absolute_deadline_at", record.get("deadline_at") or now))
                if now >= min(float(record["deadline_at"]), absolute_deadline):
                    break

                # A pre-existing engine can die before acknowledging this intent. Re-ensure exactly
                # one driver under the same per-run sequencer; the spawn-inflight lease closes the
                # Popen→engine.lock window for other command workers/processes.
                if spec.engine_policy is not EnginePolicy.NO_SPAWN and not alive:
                    with self.sequence(rd):
                        retry_observation = self._observe(rd)
                        if self._postcondition(rd, record, retry_observation):
                            self._succeeded(rd, path, record)
                            return
                        retry_liveness = self._engine_state(rd)
                        if retry_liveness is None:
                            self._terminal(
                                path, record, "failed",
                                error=self._engine_unknown_error(
                                    f"restart a driver for {event_type}", retryable=True))
                            return
                        if retry_liveness is False and not self._recent_spawn_claim(rd):
                            self._record_spawn_claim(rd, command_id, None)
                            try:
                                pid = self._spawn(rd)
                            except Exception as exc:  # noqa: BLE001
                                self._clear_spawn_claim(rd, command_id)
                                self._terminal(path, record, "failed", error=_error(
                                    "spawn_failed", f"could not restart the run engine: {exc}",
                                    "fix the cause, then POST this command id's /retry endpoint",
                                    retryable=True))
                                return
                            record["spawned_by_command"] = True
                            record["engine_pid"] = pid
                            record["updated_at"] = time.time()
                            self._record_spawn_claim(rd, command_id, pid)
                            self._save(path, record)
                            startup_deadline = min(
                                float(record.get("absolute_deadline_at") or time.time()),
                                time.time() + self.startup_timeout)
                            while time.time() < startup_deadline:
                                self._heartbeat_execution(rd, command_id)
                                startup_observation = self._observe(rd)
                                if (self._postcondition(rd, record, startup_observation)
                                        or self._engine_state(rd) is True):
                                    self._clear_spawn_claim(rd, command_id)
                                    record["spawn_claim_released"] = True
                                    break
                                time.sleep(self.poll_interval)

                time.sleep(self.poll_interval)
            uncertain_start = False
            if (record.get("spawned_by_command")
                    and not record.get("spawn_claim_released")):
                if self._engine_state(rd) is True:
                    self._clear_spawn_claim(rd, command_id)
                    record["spawn_claim_released"] = True
                else:
                    uncertain_start = self._quarantine_spawn_claim(
                        rd, command_id, record.get("engine_pid"))
            else:
                self._clear_spawn_claim(rd, command_id)
            if uncertain_start:
                self._terminal(path, record, "timed_out", error=_error(
                    "engine_start_uncertain",
                    "the detached engine has not exposed engine.lock and is not known to have exited",
                    "wait and GET this command; do not retry or launch another driver while quarantined",
                    retryable=False))
            else:
                self._terminal(path, record, "timed_out", error=_error(
                    "postcondition_timeout",
                    f"command intent was recorded but {record.get('postcondition')} was not observed in time",
                    "GET may reconcile late completion; otherwise POST this command id's /retry endpoint",
                    retryable=True))
        except Exception as exc:  # noqa: BLE001 - worker failures must become observable records
            try:
                self._terminal(path, record, "failed", error=_error(
                    "command_worker_failed", str(exc),
                    "correct the cause, then POST this command id's /retry endpoint",
                    retryable=True))
            except Exception:
                pass
        finally:
            if sequence_held and sequence_ctx is not None:
                sequence_ctx.__exit__(None, None, None)
            if claimed:
                self._release_execution(rd, command_id)
