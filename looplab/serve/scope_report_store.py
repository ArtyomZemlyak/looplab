"""The durable scope-report STORE: paths, receipts, leases, fences and record validation.

Extracted from `routers/reports.py` (doc 25 SR-12), where ~1 400 lines of it sat ahead of
`build_router`. None of it is HTTP: it validates a reports directory, confines a path inside it,
reads and migrates a record, takes an action lease, writes a receipt, and answers whether a stored
record is action-confirmed. `routers/genesis.py` needed exactly one of those functions
(`_prior_learnings_index`) and could only reach it by importing a SIBLING ROUTER's privates, which is
what stopped the route modules from being independent leaves.

`routers/reports.py` re-exports every name through a star import, so `reports.<name>` keeps resolving
for its own `build_router` and for the tests that spell it that way.

MONKEYPATCH SEAMS LIVE HERE NOW. A star import BINDS BY VALUE, so patching `reports.<name>` no longer
reaches the lookup these functions perform — the tests that patch `strict_atomic_write_text`,
`capture_scope_source`, `_read_scope_action_lease_marker` and `_PRIOR_REPORT_PARSE_MAX_BYTES` were
re-pointed at this module in the same change. That is the whole risk of this extraction: a patch left
on the old module keeps passing while testing nothing (the failure mode CT-09 documents for
`verify.py`), so a name added here that a test patches must be patched HERE.
"""
from __future__ import annotations

import hashlib
import errno
import json
import math
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any


from looplab.core.atomicio import atomic_write_text, strict_atomic_write_text
from looplab.core.comparison import (finite_measurement)
from looplab.events.eventstore import EventStoreLockError, _interprocess_lock
from looplab.serve.scope_report import (MAX_SCOPE_REPORT_RUNS)
from looplab.serve.scope_sources import (MAX_SCOPE_EVENT_BYTES, MAX_SCOPE_TOTAL_EVENT_BYTES)
from looplab.core.redact import redact_persisted_text

_SCOPE_TYPES = frozenset({"project", "task", "supertask"})
_SCOPE_STORAGE_ERROR = {
    "code": "scope_report_storage_conflict",
    "error_kind": "storage",
    "ambiguous": True,
    "message": "Durable scope-report storage could not be safely confirmed.",
    "error": "The action ledger, required filesystem sync, or cross-process lock is unavailable.",
    "remediation": (
        "Keep the same action UUID, restore durable filesystem/locking support, and reconcile again."
    ),
}
_SCOPE_PUBLICATION_UNCONFIRMED = {
    "code": "scope_report_publication_unconfirmed",
    "error_kind": "indeterminate",
    "ambiguous": True,
    "error": "The visible paid report has no matching confirmed durable action terminal.",
    "remediation": "Reconcile the exact action UUID; do not treat this report as authoritative.",
}
_SCOPE_INPUTS_CHANGED = {
    "code": "scope_report_inputs_changed",
    "error_kind": "conflict",
    "error": "Scope runs changed while the report was being generated. The previous report was kept.",
    "remediation": "Retry generation from the current scope snapshot.",
}
_SCOPE_SOURCE_TOO_LARGE = {
    "code": "scope_report_source_too_large",
    "error_kind": "capacity",
    "message": "The scope's event evidence exceeds the bounded cross-run report limit.",
    "error": "Scope event evidence is too large for one bounded report snapshot.",
    "max_run_bytes": MAX_SCOPE_EVENT_BYTES,
    "max_scope_bytes": MAX_SCOPE_TOTAL_EVENT_BYTES,
    "remediation": "Generate a narrower scope report or compact oversized run history.",
}
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVER_VERDICT_AUTHORITY = "server-derived-v3"
_SERVER_CONTENT_SCHEMA = 5
_SCOPE_STORE_THREAD_LOCK = threading.Lock()
_PRIOR_REPORT_MAX_FILES = 256
_PRIOR_REPORT_MAX_RECORDS = 20
_PRIOR_REPORT_MAX_NEXT_DIRECTIONS = 2
_PRIOR_REPORT_MAX_BYTES = 8 * 1024
_PRIOR_REPORT_PARSE_MAX_BYTES = 16 * 1024 * 1024
_SCOPE_REPORT_RECORD_MAX_BYTES = 512 * 1024
_SCOPE_ACTION_RECORD_MAX_BYTES = 16 * 1024
_SCOPE_ACTION_FENCE_MAX_BYTES = 8 * 1024
_SCOPE_ACTION_FENCE_SCHEMA = 1
_SCOPE_ACTION_LEASE_MARKER_MAX_BYTES = 8 * 1024
_SCOPE_ACTION_LEASE_MARKER_SCHEMA = 1
_SCOPE_REVISION_CACHE_MAX = 256
_SCOPE_REVISION_CACHE_TTL_S = 60.0
_SCOPE_CONTEXT_SCHEMA = 2
_SCOPE_ACTION_SCHEMA = 1
_SCOPE_ACTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SCOPE_JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SCOPE_ACTION_REQUIRED = {
    "code": "scope_report_idempotency_required",
    "error_kind": "precondition",
    "error": "A UUIDv4 Idempotency-Key is required for paid scope-report generation.",
    "remediation": "Retry this exact action with one stable UUIDv4 Idempotency-Key.",
}
_SCOPE_ACTION_CONFLICT = {
    "code": "scope_report_action_conflict",
    "error_kind": "conflict",
    "error": "This scope-report action identity belongs to a different scope or receipt.",
    "remediation": "Reuse an action UUID only for the exact scope where it was created.",
}
_SCOPE_ACTION_ACTIVE = {
    "code": "scope_report_action_in_progress",
    "error_kind": "conflict",
    "error": "Another paid scope-report action is still unresolved for this scope.",
    "remediation": "Reconcile or explicitly abandon that action before starting a new one.",
}
_SCOPE_ACTION_INDETERMINATE = {
    "code": "scope_report_action_indeterminate",
    "error_kind": "indeterminate",
    "error": "The paid action has no live exact worker and no confirmed durable terminal.",
    "remediation": (
        "Check this action again, or explicitly abandon it before creating a new action UUID."
    ),
}
_SCOPE_ACTION_ABANDONED = {
    "code": "scope_report_action_abandoned",
    "error_kind": "abandoned",
    "error": "The unresolved paid action was explicitly abandoned.",
    "remediation": "Create a new UUID only for an intentional new generation attempt.",
}


class _ScopeReportStorageConflict(RuntimeError):
    """An existing scope-report path cannot be proven to belong to the requested scope."""


class _ScopeReportActionConflict(_ScopeReportStorageConflict):
    """A client action UUID names a corrupt receipt or an action owned by another scope."""


def _scope_identity(scope_type: str, scope_id: str) -> dict[str, str]:
    return {"type": str(scope_type), "id": str(scope_id)}


def _is_link_or_reparse(entry: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(entry.st_mode) or bool(reparse_flag and attributes & reparse_flag)


def _validated_reports_dir(reports_dir: Path, *, create: bool = False) -> Path:
    """Return the lexical report directory only when it remains inside its canonical parent.

    ``Path.resolve()`` must never establish the authority boundary here: resolving a hostile
    ``reports`` symlink/junction would bless its external target as the store. The application root
    is canonicalized at startup, so its direct lexical child is the only valid report directory.
    """
    base = Path(os.path.abspath(os.fspath(reports_dir)))
    try:
        if base.parent.resolve(strict=True) != base.parent:
            raise _ScopeReportStorageConflict("scope report parent is not canonical")
        if create:
            base.mkdir(exist_ok=True)
        entry = base.lstat()
    except FileNotFoundError:
        if create:
            raise _ScopeReportStorageConflict("scope report directory disappeared")
        return base
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict("scope report directory could not be validated") from exc
    if (not stat.S_ISDIR(entry.st_mode) or _is_link_or_reparse(entry)):
        raise _ScopeReportStorageConflict("scope report directory is not a trusted directory")
    try:
        if base.resolve(strict=True) != base:
            raise _ScopeReportStorageConflict("scope report directory escaped its parent")
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict("scope report directory could not be resolved") from exc
    return base


def _confined_report_path(reports_dir: Path, filename: str) -> Path:
    base = _validated_reports_dir(reports_dir)
    candidate = base / filename
    try:
        entry = candidate.lstat()
    except FileNotFoundError:
        return candidate
    except OSError as exc:
        raise _ScopeReportStorageConflict("scope report path could not be inspected") from exc
    if not stat.S_ISREG(entry.st_mode) or _is_link_or_reparse(entry):
        raise _ScopeReportStorageConflict("scope report path is not a trusted regular file")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _ScopeReportStorageConflict("scope report path escaped its store")
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict("scope report path could not be resolved") from exc
    return candidate


@contextmanager
def _scope_store_lock(reports_dir: Path):
    """Serialize migration/publication outside the replaceable report directory itself."""
    base = _validated_reports_dir(reports_dir)
    lock_path = base.parent / ".scope-reports.lock"
    try:
        entry = lock_path.lstat()
    except FileNotFoundError:
        entry = None
    except OSError as exc:
        raise _ScopeReportStorageConflict("scope report lock could not be inspected") from exc
    if entry is not None and (not stat.S_ISREG(entry.st_mode) or _is_link_or_reparse(entry)):
        raise _ScopeReportStorageConflict("scope report lock is not a trusted regular file")
    try:
        with _SCOPE_STORE_THREAD_LOCK, _interprocess_lock(lock_path, required=True):
            _validated_reports_dir(reports_dir)
            yield
    except EventStoreLockError as exc:
        raise _ScopeReportStorageConflict("scope report lock is unavailable") from exc


def _scope_report_path(reports_dir: Path, scope_type: str, scope_id: str) -> Path:
    """Map one exact scope identity to a confined, collision-resistant report path.

    The readable prefix is diagnostic only. The full SHA-256 suffix owns uniqueness, so lossy
    sanitization and truncation can never alias two different scope ids. Resolving the candidate
    also rejects a pre-existing symlink that would redirect reads outside (or elsewhere inside)
    the report store.
    """
    identity_bytes = json.dumps(
        _scope_identity(scope_type, scope_id), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity_bytes).hexdigest()
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", f"{scope_type}-{scope_id}")[:48]
    return _confined_report_path(
        reports_dir, f"{readable or 'scope'}-{digest}.json")


def _confined_scope_root_path(reports_dir: Path, filename: str) -> Path:
    """Confine durable action authority to the stable run root, outside ``reports/`` swaps."""
    root = _validated_reports_dir(reports_dir).parent
    candidate = root / filename
    try:
        entry = candidate.lstat()
    except FileNotFoundError:
        return candidate
    except OSError as exc:
        raise _ScopeReportStorageConflict(
            "scope report action root path could not be inspected") from exc
    if not stat.S_ISREG(entry.st_mode) or _is_link_or_reparse(entry):
        raise _ScopeReportStorageConflict(
            "scope report action root path is not a trusted regular file")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _ScopeReportStorageConflict(
                "scope report action root path escaped its store")
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict(
            "scope report action root path could not be resolved") from exc
    return candidate


def _scope_action_path(reports_dir: Path, action_id: str) -> Path:
    """Map a global client action UUID to one permanent root-level receipt path."""
    digest = hashlib.sha256(action_id.encode("ascii")).hexdigest()
    return _confined_scope_root_path(
        reports_dir, f".scope-action-{digest}.receipt")


def _scope_identity_hash(scope_type: str, scope_id: str) -> str:
    encoded = json.dumps(
        _scope_identity(scope_type, scope_id), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_scope_report_path(reports_dir: Path, scope_type: str, scope_id: str) -> Path:
    """The pre-hash filename, used only for exact-identity upgrade reads."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{scope_type}-{scope_id}")[:120]
    return _confined_report_path(reports_dir, safe + ".json")


def _stat_identity(entry: os.stat_result) -> tuple[int, ...]:
    return (
        int(entry.st_mode), int(entry.st_dev), int(entry.st_ino),
        int(entry.st_mtime_ns), int(entry.st_size),
        int(getattr(entry, "st_file_attributes", 0) or 0),
    )


_SCOPE_ACTION_LEASES_LOCK = threading.Lock()
_SCOPE_ACTION_LEASES: set[str] = set()


def _scope_action_lease_path(reports_dir: Path, action_id: str) -> Path:
    """Return a stable lock path outside the replaceable reports directory."""
    digest = hashlib.sha256(action_id.encode("ascii")).hexdigest()
    return _confined_scope_root_path(
        reports_dir, f".scope-action-{digest}.live.lock")


def _scope_action_scope_lease_path(
        reports_dir: Path, scope_type: str, scope_id: str) -> Path:
    """Return the permanent OS-lease marker for one exact scope identity."""
    return _confined_scope_root_path(
        reports_dir,
        f".scope-action-scope-{_scope_identity_hash(scope_type, scope_id)}.live.lock",
    )


def _action_lease_marker(
        scope_type: str, scope_id: str, action_id: str) -> dict[str, Any]:
    return {
        "schema": _SCOPE_ACTION_LEASE_MARKER_SCHEMA,
        "kind": "action",
        "scope_identity": _scope_identity(scope_type, scope_id),
        "action_id": action_id,
        "phase": "claimed",
    }


def _scope_lease_marker(scope_type: str, scope_id: str) -> dict[str, Any]:
    return {
        "schema": _SCOPE_ACTION_LEASE_MARKER_SCHEMA,
        "kind": "scope",
        "scope_identity": _scope_identity(scope_type, scope_id),
        "phase": "authority",
    }


def _read_lease_marker(path: Path) -> dict[str, Any] | None:
    marker = _read_json_record(path, max_bytes=_SCOPE_ACTION_LEASE_MARKER_MAX_BYTES)
    if marker is not None and not isinstance(marker, dict):
        raise _ScopeReportStorageConflict(
            "scope report action lease marker is corrupt")
    return marker


def _ensure_lease_marker(path: Path, expected: dict[str, Any]) -> None:
    """Strictly create one immutable, scope-bound lease marker before paid work."""
    current = _read_lease_marker(path)
    if current is not None:
        if current != expected:
            raise _ScopeReportActionConflict(
                "scope report action lease marker belongs to another identity")
        # A strict replace may become visible just before its parent-directory sync fails. A live
        # lock proves the original inode must not be replaced; an unlocked equal marker is instead
        # re-published strictly so a restart never blesses an unconfirmed scope/UUID binding.
        if _lease_path_is_live(path):
            return
    encoded = json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _SCOPE_ACTION_LEASE_MARKER_MAX_BYTES:
        raise _ScopeReportStorageConflict(
            "scope report action lease marker exceeds its byte limit")
    try:
        strict_atomic_write_text(path, encoded)
        verified = _read_lease_marker(path)
    except (_ScopeReportActionConflict, _ScopeReportStorageConflict):
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict(
            "scope report action lease marker could not be durably published") from exc
    if verified != expected:
        raise _ScopeReportStorageConflict(
            "scope report action lease marker changed during publication")


def _read_scope_action_lease_marker(
        reports_dir: Path, action_id: str) -> dict[str, Any] | None:
    marker = _read_lease_marker(_scope_action_lease_path(reports_dir, action_id))
    if marker is None:
        return None
    if (set(marker) != {"schema", "kind", "scope_identity", "action_id", "phase"}
            or marker.get("schema") != _SCOPE_ACTION_LEASE_MARKER_SCHEMA
            or marker.get("kind") != "action"
            or marker.get("action_id") != action_id
            or marker.get("phase") != "claimed"
            or not isinstance(marker.get("scope_identity"), dict)):
        raise _ScopeReportStorageConflict(
            "scope report action lease marker is conflicting or corrupt")
    return marker


def _read_scope_lease_marker(
        reports_dir: Path, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    marker = _read_lease_marker(
        _scope_action_scope_lease_path(reports_dir, scope_type, scope_id))
    expected = _scope_lease_marker(scope_type, scope_id)
    if marker is not None and marker != expected:
        raise _ScopeReportStorageConflict(
            "scope report scope lease marker is conflicting or corrupt")
    return marker


def _open_scope_action_lease(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _is_link_or_reparse(opened):
            raise _ScopeReportStorageConflict(
                "scope report action lease changed before it was opened")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        after = path.lstat()
        if (_is_link_or_reparse(after)
                or _stat_identity(after) != _stat_identity(os.fstat(descriptor))):
            raise _ScopeReportStorageConflict(
                "scope report action lease changed while it was opened")
        return descriptor
    except BaseException:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _try_lock_scope_action_descriptor(descriptor: int) -> bool:
    try:
        # Windows byte locks deny reads of the locked region. Keep the lock byte beyond the bounded
        # immutable JSON marker so sibling processes can validate scope/action ownership while work
        # is live; locking past EOF does not extend the file.
        os.lseek(descriptor, _SCOPE_ACTION_LEASE_MARKER_MAX_BYTES + 1, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if (exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                or getattr(exc, "winerror", None) in {33, 36, 158}):
            return False
        raise _ScopeReportStorageConflict(
            "scope report action lease lock is unavailable") from exc


def _unlock_scope_action_descriptor(descriptor: int) -> None:
    try:
        os.lseek(descriptor, _SCOPE_ACTION_LEASE_MARKER_MAX_BYTES + 1, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        # Closing the descriptor below is the authoritative OS-level release backstop.
        pass


class _ScopeActionLease:
    def __init__(self, key: str, descriptor: int):
        self._key = key
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        try:
            _unlock_scope_action_descriptor(descriptor)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            finally:
                with _SCOPE_ACTION_LEASES_LOCK:
                    _SCOPE_ACTION_LEASES.discard(self._key)


_RETAINED_SCOPE_ACTION_LEASES: dict[
    str, tuple[_ScopeActionLease, _ScopeActionLease]
] = {}


def _retained_scope_action_key(reports_dir: Path, action_id: str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(
        _scope_action_lease_path(reports_dir, action_id))))


def _retain_scope_action_leases(
        reports_dir: Path, action_id: str, action_lease: _ScopeActionLease,
        scope_lease: _ScopeActionLease) -> None:
    """Keep both live handles as an in-process quarantine after ambiguous strict writes."""
    key = _retained_scope_action_key(reports_dir, action_id)
    with _SCOPE_ACTION_LEASES_LOCK:
        existing = _RETAINED_SCOPE_ACTION_LEASES.get(key)
        if existing is not None and existing != (action_lease, scope_lease):
            raise _ScopeReportStorageConflict(
                "scope report action has conflicting retained leases")
        _RETAINED_SCOPE_ACTION_LEASES[key] = (action_lease, scope_lease)


def _scope_action_leases_are_retained(reports_dir: Path, action_id: str) -> bool:
    key = _retained_scope_action_key(reports_dir, action_id)
    with _SCOPE_ACTION_LEASES_LOCK:
        return key in _RETAINED_SCOPE_ACTION_LEASES


def _release_retained_scope_action_leases(
        reports_dir: Path, action_id: str) -> None:
    key = _retained_scope_action_key(reports_dir, action_id)
    with _SCOPE_ACTION_LEASES_LOCK:
        leases = _RETAINED_SCOPE_ACTION_LEASES.pop(key, None)
    if leases is None:
        return
    action_lease, scope_lease = leases
    # Release the per-scope gate first. Until the action gate is released, exact reconciliation still
    # sees the old UUID as live and no different UUID can race through the hand-off.
    scope_lease.release()
    action_lease.release()


def _acquire_lease_path(path: Path) -> _ScopeActionLease | None:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _SCOPE_ACTION_LEASES_LOCK:
        if key in _SCOPE_ACTION_LEASES:
            return None
    try:
        descriptor = _open_scope_action_lease(path, create=False)
        if not _try_lock_scope_action_descriptor(descriptor):
            os.close(descriptor)
            return None
    except _ScopeReportStorageConflict:
        raise
    except OSError as exc:
        raise _ScopeReportStorageConflict(
            "scope report action lease could not be acquired") from exc
    with _SCOPE_ACTION_LEASES_LOCK:
        # The store lock serializes same-process acquisition, while this registry makes liveness
        # explicit even on platforms whose byte locks are process-scoped rather than handle-scoped.
        if key in _SCOPE_ACTION_LEASES:
            _unlock_scope_action_descriptor(descriptor)
            os.close(descriptor)
            return None
        _SCOPE_ACTION_LEASES.add(key)
    return _ScopeActionLease(key, descriptor)


def _lease_path_is_live(path: Path) -> bool:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _SCOPE_ACTION_LEASES_LOCK:
        if key in _SCOPE_ACTION_LEASES:
            return True
    try:
        descriptor = _open_scope_action_lease(path, create=False)
    except FileNotFoundError as exc:
        # A claimed action created this path before its receipt and the server never deletes it.
        # Missing therefore means external corruption (and, on POSIX, may hide a still-locked
        # unlinked inode); treating it as dead could authorize overlapping paid work.
        raise _ScopeReportStorageConflict(
            "scope report action lease disappeared") from exc
    except _ScopeReportStorageConflict:
        raise
    except OSError as exc:
        raise _ScopeReportStorageConflict(
            "scope report action lease could not be probed") from exc
    try:
        if not _try_lock_scope_action_descriptor(descriptor):
            return True
        _unlock_scope_action_descriptor(descriptor)
        return False
    finally:
        os.close(descriptor)


def _acquire_scope_action_lease(
        reports_dir: Path, scope_type: str, scope_id: str,
        action_id: str) -> _ScopeActionLease | None:
    path = _scope_action_lease_path(reports_dir, action_id)
    expected = _action_lease_marker(scope_type, scope_id, action_id)
    _ensure_lease_marker(path, expected)
    # Re-read through the typed validator so malformed-but-equal-looking dictionaries cannot become
    # global UUID authority and so a dead marker can never be rebound by a caller-supplied scope.
    marker = _read_scope_action_lease_marker(reports_dir, action_id)
    if marker != expected:
        raise _ScopeReportActionConflict(
            "scope report action lease marker belongs to another scope")
    return _acquire_lease_path(path)


def _acquire_scope_action_scope_lease(
        reports_dir: Path, scope_type: str, scope_id: str) -> _ScopeActionLease | None:
    path = _scope_action_scope_lease_path(reports_dir, scope_type, scope_id)
    expected = _scope_lease_marker(scope_type, scope_id)
    _ensure_lease_marker(path, expected)
    if _read_scope_lease_marker(reports_dir, scope_type, scope_id) != expected:
        raise _ScopeReportStorageConflict(
            "scope report scope lease marker changed during acquisition")
    return _acquire_lease_path(path)


def _scope_action_lease_is_live(reports_dir: Path, action_id: str) -> bool:
    return _lease_path_is_live(_scope_action_lease_path(reports_dir, action_id))


def _scope_action_scope_lease_is_live(
        reports_dir: Path, scope_type: str, scope_id: str) -> bool:
    return _lease_path_is_live(
        _scope_action_scope_lease_path(reports_dir, scope_type, scope_id))


def _read_bounded_report_bytes(
        path: Path, *, max_bytes: int = _SCOPE_REPORT_RECORD_MAX_BYTES) -> bytes | None:
    """Read one immutable regular-file snapshot without following a swapped link."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _ScopeReportStorageConflict("scope report could not be inspected") from exc
    if (not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(before)
            or before.st_size > max_bytes):
        raise _ScopeReportStorageConflict("scope report is not a bounded regular file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or _is_link_or_reparse(opened)
                    or opened.st_size > max_bytes
                    or _stat_identity(opened) != _stat_identity(before)):
                raise _ScopeReportStorageConflict("scope report changed before it was read")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except _ScopeReportStorageConflict:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise _ScopeReportStorageConflict("scope report could not be read safely") from exc
    raw = b"".join(chunks)
    if (not stat.S_ISREG(after.st_mode) or _is_link_or_reparse(after)
            or len(raw) != opened.st_size
            or len(raw) > max_bytes
            or _stat_identity(after) != _stat_identity(opened)):
        raise _ScopeReportStorageConflict("scope report changed or exceeded its byte limit")
    return raw


def _serialize_scope_record(record: dict[str, Any]) -> str:
    """Serialize only records that can be read back inside the same storage budget."""
    try:
        encoded = json.dumps(record, indent=2)
    except (TypeError, ValueError) as exc:
        raise _ScopeReportStorageConflict("scope report is not serializable") from exc
    # this is the persisted-report resource boundary. Check encoded bytes, not Python
    # characters, before atomic replacement so a model result can never create an unreadable record.
    if len(encoded.encode("utf-8")) > _SCOPE_REPORT_RECORD_MAX_BYTES:
        raise _ScopeReportStorageConflict("scope report exceeds its persisted byte limit")
    return encoded


def _valid_scope_sig_row(row: object) -> bool:
    """Accept legacy second-resolution rows for migration and the reset-safe v2 shape."""
    if not isinstance(row, list):
        return False
    if len(row) == 3:
        return (isinstance(row[0], str) and type(row[1]) is int and row[1] >= 0
                and type(row[2]) is int and row[2] >= 0)
    return (
        len(row) == 7
        and isinstance(row[0], str)
        and isinstance(row[1], str)
        and (not row[1] or _RUN_GENERATION_RE.fullmatch(row[1]) is not None)
        and all(type(value) is int and value >= 0 for value in row[2:])
    )


def _valid_source_revision(revision: object) -> bool:
    if not isinstance(revision, dict):
        return False
    generation = revision.get("generation")
    digest = revision.get("tail_digest")
    log_sig = revision.get("log_sig")
    base_valid = (
        isinstance(revision.get("run_id"), str)
        and isinstance(generation, str)
        and _RUN_GENERATION_RE.fullmatch(generation) is not None
        and type(revision.get("tail_seq")) is int and revision["tail_seq"] >= -1
        and type(revision.get("event_count")) is int and revision["event_count"] >= 1
        and isinstance(digest, str) and _RUN_GENERATION_RE.fullmatch(digest) is not None
        and _valid_scope_sig_row(log_sig)
        and len(log_sig) == 7
        and log_sig[0] == revision["run_id"]
        and log_sig[1] == generation
    )
    if not base_valid:
        return False
    for field in ("events_digest", "task_snapshot_digest", "config_snapshot_digest"):
        value = revision.get(field)
        if value is not None and (
                not isinstance(value, str) or _RUN_GENERATION_RE.fullmatch(value) is None):
            return False
    event_bytes = revision.get("event_bytes")
    if event_bytes is not None and (type(event_bytes) is not int or event_bytes < 0):
        return False
    return True


def _complete_source_revision(revision: object) -> bool:
    """A v2 revision can prove every model-visible file, not only the event-log stat."""
    return (
        _valid_source_revision(revision)
        and all(isinstance(revision.get(field), str) for field in (
            "events_digest", "task_snapshot_digest", "config_snapshot_digest",
        ))
        and type(revision.get("event_bytes")) is int
    )


def _valid_source_receipt(
        run_ids: object, sig: object, source_revisions: object,
        omitted_runs: object, omitted_source_probes: object) -> bool:
    """Validate both legacy all-readable records and the explicit partial-source receipt."""
    if not isinstance(run_ids, list) or not isinstance(sig, list):
        return False
    if source_revisions is None:
        return omitted_runs is None and omitted_source_probes is None
    if (not isinstance(source_revisions, list)
            or not all(_valid_source_revision(row) for row in source_revisions)):
        return False
    revision_ids = [row["run_id"] for row in source_revisions]
    if omitted_runs is None:
        # Legacy v2 records represented only fully captured scopes.
        return revision_ids == run_ids and [row["log_sig"] for row in source_revisions] == sig
    if (not isinstance(omitted_runs, list)
            or not all(isinstance(run_id, str) for run_id in omitted_runs)
            or len(omitted_runs) != len(set(omitted_runs))
            or len(revision_ids) != len(set(revision_ids))):
        return False
    sig_by_id = {row[0]: row for row in sig}
    if len(sig_by_id) != len(sig) or set(sig_by_id) != set(run_ids):
        return False
    captured = set(revision_ids)
    omitted = set(omitted_runs)
    probes_valid = (
        omitted_source_probes is None
        or (
            isinstance(omitted_source_probes, dict)
            and set(omitted_source_probes) == omitted
            and all(
                isinstance(digest, str) and _RUN_GENERATION_RE.fullmatch(digest)
                for digest in omitted_source_probes.values()
            )
        )
    )
    return (
        not captured & omitted
        and captured | omitted == set(run_ids)
        and all(sig_by_id.get(row["run_id"]) == row["log_sig"] for row in source_revisions)
        and probes_valid
    )


def _record_payload_matches_scope(rec: object, scope_type: str, scope_id: str) -> bool:
    """Validate the historical report payload and its exact embedded display scope."""
    if not isinstance(rec, dict):
        return False
    expected = _scope_identity(scope_type, scope_id)
    scope = rec.get("scope")
    run_ids = rec.get("run_ids")
    sig = rec.get("sig")
    source_revisions = rec.get("source_revisions")
    return (
        isinstance(scope, dict)
        and scope.get("type") == expected["type"]
        and scope.get("id") == expected["id"]
        and isinstance(scope.get("label"), str)
        and type(rec.get("generated_at")) is int
        and rec["generated_at"] >= 0
        and isinstance(run_ids, list)
        and all(isinstance(run_id, str) for run_id in run_ids)
        and len(run_ids) == len(set(run_ids))
        and isinstance(sig, list)
        and all(_valid_scope_sig_row(row) for row in sig)
        and _valid_source_receipt(
            run_ids, sig, source_revisions, rec.get("omitted_runs"),
            rec.get("omitted_source_probes"))
        and (rec.get("action_id") is None
             or _scope_action_id(rec.get("action_id")) == rec.get("action_id"))
        and isinstance(rec.get("content"), dict)
    )


def _record_matches_scope(rec: object, scope_type: str, scope_id: str) -> bool:
    """Require both immutable storage identity and display scope to name the requested scope."""
    return (
        isinstance(rec, dict)
        and rec.get("scope_identity") == _scope_identity(scope_type, scope_id)
        and _record_payload_matches_scope(rec, scope_type, scope_id)
    )


def _read_json_record(
        path: Path, *, max_bytes: int = _SCOPE_REPORT_RECORD_MAX_BYTES) -> dict[str, Any] | None:
    """Read one already-confined regular file; missing is distinct from corrupt."""
    try:
        encoded = _read_bounded_report_bytes(path, max_bytes=max_bytes)
        if encoded is None:
            return None
        raw = encoded.decode("utf-8")
    except UnicodeError as exc:
        raise _ScopeReportStorageConflict("scope report could not be read") from exc
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise _ScopeReportStorageConflict("scope report is not valid JSON") from exc
    if not isinstance(rec, dict):
        raise _ScopeReportStorageConflict("scope report is not a JSON object")
    return rec


def _scope_action_id(value: object) -> str | None:
    if not isinstance(value, str) or _SCOPE_ACTION_ID_RE.fullmatch(value) is None:
        return None
    return value.lower()


def _scope_action_failure(result: object, action_id: str) -> dict[str, Any]:
    """Project only server-owned diagnostics; provider exceptions/prose never cross."""
    source = result if isinstance(result, dict) else {}
    code = source.get("code")
    if code == _SCOPE_INPUTS_CHANGED["code"]:
        safe = {**_SCOPE_INPUTS_CHANGED, "stale": True}
    elif code == _SCOPE_SOURCE_TOO_LARGE["code"]:
        safe = _SCOPE_SOURCE_TOO_LARGE
    elif code == _SCOPE_STORAGE_ERROR["code"]:
        safe = _SCOPE_STORAGE_ERROR
    elif code == _SCOPE_ACTION_CONFLICT["code"]:
        safe = _SCOPE_ACTION_CONFLICT
    elif code == _SCOPE_ACTION_INDETERMINATE["code"]:
        safe = _SCOPE_ACTION_INDETERMINATE
    elif code == _SCOPE_ACTION_ABANDONED["code"]:
        safe = _SCOPE_ACTION_ABANDONED
    else:
        safe = {
            "code": "job_failed",
            "error_kind": "internal",
            "error": "background job failed",
        }
    return {"ok": False, "action_id": action_id, **safe}


def _scope_action_success(action_id: str) -> dict[str, Any]:
    """Keep the action ledger compact; the canonical report file owns the paid payload."""
    return {
        "ok": True,
        "action_id": action_id,
        "authoritative": True,
        "published": True,
    }


def _valid_scope_action_result(
        result: object, scope_type: str, scope_id: str, action_id: str) -> bool:
    if (not isinstance(result, dict) or type(result.get("ok")) is not bool
            or result.get("action_id") != action_id):
        return False
    if result["ok"]:
        # The exact report is read from its canonical scope path and must independently carry this
        # action id. Duplicating a model-sized payload in every receipt made the ledger unbounded and
        # allowed an old action replay to masquerade as the current canonical scope report.
        return result == _scope_action_success(action_id)
    # Exact equality makes the receipt a server-owned vocabulary, not a durable echo channel for
    # arbitrary provider/internal prose that happened to resemble a failure object.
    return result == _scope_action_failure(result, action_id)


_SCOPE_ACTION_RECEIPT_KEYS = frozenset({
    "schema", "scope_identity", "action_id", "generation_identity", "job_id",
    "status", "updated_at", "result",
})
_SCOPE_USAGE_COUNTER_KEYS = ("calls", "prompt_tokens", "completion_tokens", "total_tokens")


def _valid_scope_action_usage(value: object) -> bool:
    """Shape gate for the paid-call receipt carried by an action ledger row.

    Kept as an OPTIONAL key rather than a schema bump on purpose: a receipt written by an older
    binary stays valid, so an upgrade cannot strand an in-flight action behind a permanent 409. Only
    server-derived numbers cross — never prompts, model URLs, or provider prose.
    """
    if not isinstance(value, dict):
        return False
    model = value.get("model")
    if (value.get("state") not in {"attempted", "observed"}
            or not isinstance(model, str) or len(model) > 200):
        return False
    if value["state"] == "attempted":
        # No provider answer was observed, so there is deliberately nothing to report but the
        # intent: this row exists so a crash mid-call is not mistaken for "never spent anything".
        return set(value) == {"state", "model"}
    if set(value) != {"state", "model", "cost", *_SCOPE_USAGE_COUNTER_KEYS}:
        return False
    cost = value.get("cost")
    if type(cost) is not float or not math.isfinite(cost) or cost < 0.0:
        return False
    return all(type(value[key]) is int and value[key] >= 0
               for key in _SCOPE_USAGE_COUNTER_KEYS)


def _scope_usage_model(model: object) -> str:
    # Bounded at the same length the receipt validator accepts: an operator can configure an
    # arbitrarily long model id, and letting it fail the shape gate would refuse the whole paid
    # action over a label.
    return str(model or "")[:200]


def _attempted_scope_usage(model: object) -> dict[str, Any]:
    return {"state": "attempted", "model": _scope_usage_model(model)}


def _zero_scope_usage(model: object) -> dict[str, Any]:
    return {"state": "observed", "model": _scope_usage_model(model), "cost": 0.0,
            **{key: 0 for key in _SCOPE_USAGE_COUNTER_KEYS}}


def _observed_scope_usage(client: object, model: object) -> dict[str, Any]:
    """Read what this one leased client actually spent, in the ledger's sanitized vocabulary.

    The client is minted fresh per generation, so its accountant total IS this action's delta — no
    before/after subtraction to get wrong. A client that reports nothing (offline fallback, a
    provider with no usage telemetry) yields an honest all-zero observation rather than silence.
    """
    from looplab.engine.costs import in_memory_cost_total, sanitize_usage_delta
    total = None
    if client is not None:
        try:
            total = in_memory_cost_total(SimpleNamespace(researcher=client))
        except Exception:  # noqa: BLE001 - hostile/partial client telemetry degrades to zero
            total = None
    if total is None:
        return _zero_scope_usage(model)
    clean = sanitize_usage_delta(total)
    return {"state": "observed", "model": _scope_usage_model(model),
            "cost": float(clean["cost"]),
            **{key: int(clean[key]) for key in _SCOPE_USAGE_COUNTER_KEYS}}


def _valid_scope_action_receipt(
        rec: object, scope_type: str, scope_id: str, action_id: str) -> bool:
    if not isinstance(rec, dict):
        return False
    status = rec.get("status")
    generation_identity = rec.get("generation_identity")
    if "usage" in rec and not _valid_scope_action_usage(rec["usage"]):
        return False
    return (
        set(rec) - {"usage"} == _SCOPE_ACTION_RECEIPT_KEYS
        and rec.get("schema") == _SCOPE_ACTION_SCHEMA
        and rec.get("scope_identity") == _scope_identity(scope_type, scope_id)
        and rec.get("action_id") == action_id
        and _scope_action_id(action_id) == action_id
        and isinstance(generation_identity, str)
        and re.fullmatch(r"scope-report:[0-9a-f]{64}", generation_identity) is not None
        and isinstance(rec.get("job_id"), str)
        and _SCOPE_JOB_ID_RE.fullmatch(rec["job_id"]) is not None
        and status in {"running", "done", "indeterminate", "abandoned"}
        and type(rec.get("updated_at")) is int and rec["updated_at"] >= 0
        and ((status == "running" and rec.get("result") is None)
             or (status == "done" and _valid_scope_action_result(
                 rec.get("result"), scope_type, scope_id, action_id)
                 and rec["result"] != _scope_action_failure(
                     _SCOPE_ACTION_INDETERMINATE, action_id)
                 and rec["result"] != _scope_action_failure(
                     _SCOPE_ACTION_ABANDONED, action_id))
             or (status == "indeterminate" and rec.get("result")
                 == _scope_action_failure(_SCOPE_ACTION_INDETERMINATE, action_id))
             or (status == "abandoned" and rec.get("result")
                 == _scope_action_failure(_SCOPE_ACTION_ABANDONED, action_id)))
    )


def _read_scope_action_receipt(
        reports_dir: Path, scope_type: str, scope_id: str,
        action_id: str) -> dict[str, Any] | None:
    rec = _read_json_record(
        _scope_action_path(reports_dir, action_id), max_bytes=_SCOPE_ACTION_RECORD_MAX_BYTES)
    if rec is None:
        return None
    if not _valid_scope_action_receipt(rec, scope_type, scope_id, action_id):
        raise _ScopeReportActionConflict(
            "scope report action identity is conflicting or corrupt")
    return rec


def _serialize_scope_action_receipt(receipt: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _ScopeReportStorageConflict("scope report action receipt is not serializable") from exc
    if len(encoded.encode("utf-8")) > _SCOPE_ACTION_RECORD_MAX_BYTES:
        raise _ScopeReportStorageConflict("scope report action receipt exceeds its byte limit")
    return encoded


def _write_scope_action_receipt(
        reports_dir: Path, scope_type: str, scope_id: str,
        receipt: dict[str, Any]) -> dict[str, Any]:
    """Publish and verify one receipt while the caller owns ``_scope_store_lock``."""
    action_id = receipt.get("action_id")
    if (not isinstance(action_id, str)
            or not _valid_scope_action_receipt(receipt, scope_type, scope_id, action_id)):
        raise _ScopeReportStorageConflict("scope report action receipt is invalid")
    try:
        _validated_reports_dir(reports_dir, create=True)
        path = _scope_action_path(reports_dir, action_id)
        # claims and terminals gate an external paid side effect. Best-effort fsync is
        # insufficient here: the worker may start only after the exact receipt survived strict sync.
        strict_atomic_write_text(path, _serialize_scope_action_receipt(receipt))
        verified = _read_scope_action_receipt(
            reports_dir, scope_type, scope_id, action_id)
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict(
            "scope report action receipt could not be durably published") from exc
    if verified is None:
        raise _ScopeReportStorageConflict("scope report action receipt disappeared")
    if verified != receipt:
        raise _ScopeReportStorageConflict(
            "scope report action receipt changed during publication")
    return verified


def _missing_scope_action_receipt(
        scope_type: str, scope_id: str, action_id: str) -> dict[str, Any]:
    """Permanent fail-closed tombstone for an indexed receipt lost outside the server."""
    return {
        "schema": _SCOPE_ACTION_SCHEMA,
        "scope_identity": _scope_identity(scope_type, scope_id),
        "action_id": action_id,
        "generation_identity": "scope-report:" + "0" * 64,
        "job_id": "0" * 16,
        "status": "indeterminate",
        "updated_at": int(time.time() * 1000),
        "result": _scope_action_failure(_SCOPE_ACTION_INDETERMINATE, action_id),
    }


def _scope_action_fence_path(
        reports_dir: Path, scope_type: str, scope_id: str) -> Path:
    return _confined_scope_root_path(
        reports_dir,
        f".scope-action-scope-{_scope_identity_hash(scope_type, scope_id)}.fence",
    )


def _valid_scope_action_fence(
        value: object, scope_type: str, scope_id: str) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {
            "schema", "scope_identity", "action_id", "status", "updated_at",
        }
        and value.get("schema") == _SCOPE_ACTION_FENCE_SCHEMA
        and value.get("scope_identity") == _scope_identity(scope_type, scope_id)
        and _scope_action_id(value.get("action_id")) == value.get("action_id")
        and value.get("status") in {"active", "clear"}
        and type(value.get("updated_at")) is int
        and value["updated_at"] >= 0
    )


def _read_scope_action_fence(
        reports_dir: Path, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    rec = _read_json_record(
        _scope_action_fence_path(reports_dir, scope_type, scope_id),
        max_bytes=_SCOPE_ACTION_FENCE_MAX_BYTES)
    if rec is None:
        return None
    if not _valid_scope_action_fence(rec, scope_type, scope_id):
        raise _ScopeReportStorageConflict(
            "scope report action fence is conflicting or corrupt")
    return rec


def _write_scope_action_fence(
        reports_dir: Path, scope_type: str, scope_id: str,
        action_id: str, status: str) -> dict[str, Any]:
    fence = {
        "schema": _SCOPE_ACTION_FENCE_SCHEMA,
        "scope_identity": _scope_identity(scope_type, scope_id),
        "action_id": action_id,
        "status": status,
        "updated_at": int(time.time() * 1000),
    }
    if not _valid_scope_action_fence(fence, scope_type, scope_id):
        raise _ScopeReportStorageConflict("scope report action fence is invalid")
    encoded = json.dumps(
        fence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _SCOPE_ACTION_FENCE_MAX_BYTES:
        raise _ScopeReportStorageConflict(
            "scope report action fence exceeds its byte limit")
    try:
        strict_atomic_write_text(
            _scope_action_fence_path(reports_dir, scope_type, scope_id), encoded)
        verified = _read_scope_action_fence(reports_dir, scope_type, scope_id)
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict(
            "scope report action fence could not be durably published") from exc
    if verified != fence:
        raise _ScopeReportStorageConflict(
            "scope report action fence changed during publication")
    return verified


def _read_scope_record(path: Path, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    """Read only a record proven to own this exact path; missing is distinct from corrupt."""
    rec = _read_json_record(path)
    if rec is None:
        return None
    if not _record_matches_scope(rec, scope_type, scope_id):
        raise _ScopeReportStorageConflict("scope report identity does not match its path")
    return rec


def _read_or_migrate_scope_record(
        reports_dir: Path, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    """Read the canonical report or safely copy an exact pre-hash record into canonical storage.

    The lossy legacy filename is never accepted as identity. Its embedded scope must exactly match
    the request, and a legacy ``scope_identity`` (if present) must also agree. The old file is kept:
    deleting it could destroy the only evidence for another id that collided on the old filename.
    """
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    current = _read_scope_record(canonical, scope_type, scope_id)
    if current is not None:
        return current
    legacy = _legacy_scope_report_path(reports_dir, scope_type, scope_id)
    old = _read_json_record(legacy)
    if old is None:
        return None
    expected = _scope_identity(scope_type, scope_id)
    legacy_identity = old.get("scope_identity")
    if (legacy_identity not in (None, expected)
            or not _record_payload_matches_scope(old, scope_type, scope_id)):
        raise _ScopeReportStorageConflict("legacy scope report identity is ambiguous")
    migrated = {**old, "scope_identity": expected}
    _validated_reports_dir(reports_dir, create=True)
    # Re-derive and re-read under the caller's store lock: another process may have migrated first.
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    current = _read_scope_record(canonical, scope_type, scope_id)
    if current is not None:
        return current
    atomic_write_text(canonical, _serialize_scope_record(migrated))
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    return _read_scope_record(canonical, scope_type, scope_id)


def _valid_observational_groups(rec: dict[str, Any], groups: object) -> bool:
    """Validate schema-v5's server-owned, outcome-free comparison projection."""
    run_ids = rec.get("run_ids")
    if (not isinstance(run_ids, list)
            or not all(isinstance(run_id, str) for run_id in run_ids)
            or len(run_ids) != len(set(run_ids))
            or not isinstance(groups, list) or len(groups) > MAX_SCOPE_REPORT_RUNS):
        return False
    scope_run_ids = set(run_ids)
    seen_contracts: set[str] = set()
    seen_measurements: set[str] = set()
    sources = {
        "search": "best.metric",
        "confirmed": "best.confirmed_mean",
        "holdout": "best.holdout_metric",
    }
    allowed_reasons = {
        "incomplete_measurements",
        "incomplete_runs",
        "insufficient_population",
        "point_estimates_only",
        "minimum_effect_not_declared",
        "incomplete_population",
    }

    for group in groups:
        if not isinstance(group, dict):
            return False
        contract_id = group.get("contract_id")
        direction = group.get("direction")
        phase = group.get("measurement_phase")
        protocol = group.get("uncertainty_protocol")
        reason = group.get("indeterminate")
        measurements = group.get("measurements")
        unavailable = group.get("unavailable_measurements")
        incomplete = group.get("incomplete_runs")
        if (
            not isinstance(contract_id, str)
            or _RUN_GENERATION_RE.fullmatch(contract_id) is None
            or contract_id in seen_contracts
            or direction not in {"min", "max"}
            or phase not in sources
            or not isinstance(protocol, str)
            or not protocol
            or group.get("contract_authority") != "declared"
            or group.get("outcome_policy") != "observations-only-v1"
            or group.get("winner") is not None
            or group.get("tied_winners") != []
            or reason not in allowed_reasons
            or not isinstance(measurements, list)
            or len(measurements) > MAX_SCOPE_REPORT_RUNS
            or not isinstance(unavailable, list)
            or not isinstance(incomplete, list)
        ):
            return False
        seen_contracts.add(contract_id)
        measured_ids: set[str] = set()
        for row in measurements:
            if not isinstance(row, dict):
                return False
            run_id = row.get("run_id")
            uncertainty = row.get("uncertainty")
            if (
                not isinstance(run_id, str)
                or run_id not in scope_run_ids
                or run_id in seen_measurements
                or row.get("authority") != "declared"
                or finite_measurement(row.get("metric")) is None
                or row.get("direction") != direction
                or row.get("phase") != phase
                or row.get("source") != sources[phase]
                or not isinstance(uncertainty, dict)
                or uncertainty.get("protocol") != protocol
            ):
                return False
            if phase == "confirmed":
                if set(uncertainty) != {
                    "protocol", "std", "std_source", "seeds", "seeds_source",
                }:
                    return False
                if (
                    finite_measurement(uncertainty.get("std")) is None
                    or uncertainty["std"] < 0
                    or type(uncertainty.get("seeds")) is not int
                    or uncertainty["seeds"] <= 0
                    or uncertainty.get("std_source") != "best.confirmed_std"
                    or uncertainty.get("seeds_source") != "best.confirmed_seeds"
                ):
                    return False
            elif set(uncertainty) != {"protocol"}:
                return False
            measured_ids.add(run_id)
            seen_measurements.add(run_id)

        def valid_id_list(value: list) -> bool:
            return (
                all(isinstance(run_id, str) and run_id in scope_run_ids for run_id in value)
                and len(value) == len(set(value))
            )

        if (not valid_id_list(unavailable) or not valid_id_list(incomplete)
                or measured_ids & set(unavailable) or not set(incomplete) <= measured_ids):
            return False
        if reason == "incomplete_measurements" and not unavailable:
            return False
        if reason == "incomplete_runs" and not incomplete:
            return False
        if reason == "insufficient_population" and len(measurements) >= 2:
            return False
        if reason == "point_estimates_only" and (
                phase == "confirmed" or len(measurements) < 2 or unavailable or incomplete):
            return False
        if reason == "minimum_effect_not_declared" and (
                phase != "confirmed" or len(measurements) < 2 or unavailable or incomplete):
            return False
    return True


def _valid_metric_observations(rec: dict[str, Any], observations: object) -> bool:
    if not isinstance(observations, list) or len(observations) > MAX_SCOPE_REPORT_RUNS:
        return False
    run_ids = set(rec.get("run_ids") or ())
    allowed = {
        "uncontracted",
        "no_valid_comparison_measurement",
        "contracted_measurement_unavailable",
        "contracted_group_omitted",
    }
    for row in observations:
        if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                or row["run_id"] not in run_ids
                or row.get("comparison_status") not in allowed):
            return False
        if "metric" in row and finite_measurement(row.get("metric")) is None:
            return False
        contract_id = row.get("contract_id")
        if (contract_id is not None and (
                not isinstance(contract_id, str)
                or _RUN_GENERATION_RE.fullmatch(contract_id) is None)):
            return False
    return True


def _public_scope_record(rec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Never present a self-asserted persisted outcome as server-derived authority."""
    content = dict(rec.get("content") or {})
    groups = content.get("comparison_groups")
    if (content.get("schema") == _SERVER_CONTENT_SCHEMA
            and content.get("verdict_authority") == _SERVER_VERDICT_AUTHORITY
            and content.get("narrative_authority") == "model-advisory"
            and _valid_observational_groups(rec, groups)
            and _valid_metric_observations(rec, content.get("metric_observations"))):
        return {**rec, "authoritative": True}, False
    content["verdict"] = "No authoritative verdict is available for this legacy report; regenerate it."
    content["verdict_authority"] = "legacy-unavailable"
    content["requires_regeneration"] = True
    content["headline"] = "Legacy scope report requires regeneration"
    content["narrative_authority"] = "legacy-quarantined"
    for field in (
        "best_runs", "comparison_groups", "metric_observations", "what_worked", "what_didnt",
        "learnings", "next_directions", "caveats",
    ):
        content[field] = []
    # outcome-bearing legacy narrative is quarantined, not merely relabelled. Renaming
    # an invented winner would still let a client accidentally render it as trusted prose.
    return {**rec, "content": content, "authoritative": False}, True


def _action_bound_scope_record_is_confirmed(
        reports_dir: Path, rec: dict[str, Any],
        scope_type: str, scope_id: str) -> bool:
    """Gate paid canonical content on an independently strict exact action terminal."""
    if "action_id" not in rec:
        # Explicit legacy path: pre-action records are still handled by their content-authority rules.
        return True
    action_id = _scope_action_id(rec.get("action_id"))
    if action_id is None or action_id != rec.get("action_id"):
        return False
    try:
        receipt = _read_scope_action_receipt(
            reports_dir, scope_type, scope_id, action_id)
        if (receipt is None or receipt.get("status") != "done"
                or receipt.get("result") != _scope_action_success(action_id)):
            return False
        marker = _read_scope_action_lease_marker(reports_dir, action_id)
        if marker is not None:
            if marker != _action_lease_marker(scope_type, scope_id, action_id):
                return False
            if _scope_action_lease_is_live(reports_dir, action_id):
                return False
        else:
            fence = _read_scope_action_fence(reports_dir, scope_type, scope_id)
            scope_marker = _read_scope_lease_marker(
                reports_dir, scope_type, scope_id)
            if (fence is not None and fence["status"] == "active"
                    and fence["action_id"] == action_id
                    and scope_marker is not None
                    and _scope_action_scope_lease_is_live(
                        reports_dir, scope_type, scope_id)):
                return False
        # Strictly re-publish before treating a visible terminal as cross-process authority. This
        # repairs the replace-visible/parent-sync-failed window without trusting process-local flags.
        return _write_scope_action_receipt(
            reports_dir, scope_type, scope_id, receipt) == receipt
    except (_ScopeReportActionConflict, _ScopeReportStorageConflict):
        return False


def _prior_learnings_index(reports_dir: Path) -> str:
    """Return a bounded JSON projection of untrusted prior-report evidence for Genesis."""
    try:
        base = _validated_reports_dir(reports_dir)
    except _ScopeReportStorageConflict:
        return ""
    if not base.exists():
        return ""

    inspected_files = 0
    discovered_names: list[str] = []
    try:
        with os.scandir(base) as entries:
            # this is a prompt-input authority boundary. Bound directory work before
            # inspecting names, then revalidate every selected path and redact every copied string.
            while inspected_files < _PRIOR_REPORT_MAX_FILES:
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                inspected_files += 1
                if entry.name.endswith(".json"):
                    discovered_names.append(entry.name)
    except OSError:
        return ""

    def _safe_text(value: object, max_chars: int) -> str:
        clean = redact_persisted_text(
            value, max_chars=max_chars, entropy=True, single_line=True)
        return " ".join(clean.split())

    records: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    parsed_bytes = 0
    parse_limited = False
    for filename in sorted(discovered_names):
        try:
            p = _confined_report_path(base, filename)
            candidate_bytes = int(p.lstat().st_size)
            if candidate_bytes < 0 or (
                    parsed_bytes + candidate_bytes > _PRIOR_REPORT_PARSE_MAX_BYTES):
                parse_limited = True
                continue
            # Count attempted bytes too: a directory full of corrupt bounded JSON must not bypass
            # the aggregate work budget merely because none of it becomes prompt evidence.
            parsed_bytes += candidate_bytes
            rec = _read_json_record(p)
            if rec is None:
                continue
            identity = rec.get("scope_identity") if isinstance(rec, dict) else None
            scope_type = identity.get("type") if isinstance(identity, dict) else None
            scope_id = identity.get("id") if isinstance(identity, dict) else None
            priority = 1
            if scope_type in _SCOPE_TYPES and isinstance(scope_id, str):
                valid = (_record_matches_scope(rec, scope_type, scope_id)
                         and _scope_report_path(base, scope_type, scope_id) == p)
            else:
                scope = rec.get("scope") if isinstance(rec, dict) else None
                scope_type = scope.get("type") if isinstance(scope, dict) else None
                scope_id = scope.get("id") if isinstance(scope, dict) else None
                priority = 0
                valid = (
                    scope_type in _SCOPE_TYPES
                    and isinstance(scope_id, str)
                    and _record_payload_matches_scope(rec, scope_type, scope_id)
                    and _legacy_scope_report_path(base, scope_type, scope_id) == p
                )
            if not valid:
                continue
            # Confirmation performs a strict receipt repair, so Genesis scanning obeys the same
            # cross-process store discipline as HTTP report reads and action reconciliation.
            with _scope_store_lock(base):
                confirmed = _action_bound_scope_record_is_confirmed(
                    base, rec, scope_type, scope_id)
            if not confirmed:
                continue
            content = rec.get("content") or {}
            raw_directions = content.get("next_directions")
            directions = (
                raw_directions if isinstance(raw_directions, (list, tuple)) else ())
            projection = {
                "scope": {
                    "type": _safe_text(scope_type, 32),
                    "id": _safe_text(scope_id, 160),
                    "label": _safe_text(
                        (rec.get("scope") or {}).get("label") or "scope report", 200),
                },
                "headline": _safe_text(content.get("headline") or "", 500),
                "next_directions": [
                    _safe_text(value, 300)
                    for value in directions[:_PRIOR_REPORT_MAX_NEXT_DIRECTIONS]
                ],
            }
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError,
                _ScopeReportStorageConflict):
            continue
        key = (scope_type, scope_id)
        if key not in records or priority > records[key][0]:
            # Keep only the already-redacted compact projection. Retaining the full parsed record for
            # every eligible file would amplify a 16 MiB byte budget into far larger Python objects.
            records[key] = (priority, projection)

    projected = [row for _key, (_priority, row) in sorted(records.items())]

    if not projected:
        return ""

    def _encoded(rows: list[dict[str, Any]]) -> str:
        payload = {
            "schema": "looplab.untrusted_prior_reports.v1",
            "trust": "untrusted_model_authored_advisory",
            "records": rows,
            "receipt": {
                "inspected_files": inspected_files,
                "included_records": len(rows),
                "eligible_records": len(projected),
                "omitted_records": len(projected) - len(rows),
                # Reaching the scan ceiling is conservatively reported as limited without reading a
                # 257th entry solely to discover whether it exists.
                "scan_limited": inspected_files >= _PRIOR_REPORT_MAX_FILES,
                "parse_limited": parse_limited,
                "parsed_bytes": parsed_bytes,
                "limits": {
                    "max_files": _PRIOR_REPORT_MAX_FILES,
                    "max_records": _PRIOR_REPORT_MAX_RECORDS,
                    "max_next_directions": _PRIOR_REPORT_MAX_NEXT_DIRECTIONS,
                    "max_bytes": _PRIOR_REPORT_MAX_BYTES,
                    "max_parse_bytes": _PRIOR_REPORT_PARSE_MAX_BYTES,
                },
            },
        }
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    included: list[dict[str, Any]] = []
    for row in projected[:_PRIOR_REPORT_MAX_RECORDS]:
        candidate = _encoded([*included, row])
        if len(candidate.encode("utf-8")) > _PRIOR_REPORT_MAX_BYTES:
            break
        included.append(row)
    encoded = _encoded(included)
    # The fixed envelope is comfortably below 8 KiB, but keep this fail-closed invariant local so a
    # future metadata addition cannot silently create an unbounded prompt fragment.
    return encoded if len(encoded.encode("utf-8")) <= _PRIOR_REPORT_MAX_BYTES else ""


__all__ = [
    "_PRIOR_REPORT_MAX_BYTES",
    "_PRIOR_REPORT_MAX_FILES",
    "_PRIOR_REPORT_MAX_NEXT_DIRECTIONS",
    "_PRIOR_REPORT_MAX_RECORDS",
    "_PRIOR_REPORT_PARSE_MAX_BYTES",
    "_RUN_GENERATION_RE",
    "_SCOPE_ACTION_ABANDONED",
    "_SCOPE_ACTION_ACTIVE",
    "_SCOPE_ACTION_CONFLICT",
    "_SCOPE_ACTION_FENCE_MAX_BYTES",
    "_SCOPE_ACTION_FENCE_SCHEMA",
    "_SCOPE_ACTION_ID_RE",
    "_SCOPE_ACTION_INDETERMINATE",
    "_SCOPE_ACTION_LEASES_LOCK",
    "_SCOPE_ACTION_LEASE_MARKER_MAX_BYTES",
    "_SCOPE_ACTION_LEASE_MARKER_SCHEMA",
    "_SCOPE_ACTION_RECEIPT_KEYS",
    "_SCOPE_ACTION_RECORD_MAX_BYTES",
    "_SCOPE_ACTION_REQUIRED",
    "_SCOPE_ACTION_SCHEMA",
    "_SCOPE_CONTEXT_SCHEMA",
    "_SCOPE_INPUTS_CHANGED",
    "_SCOPE_JOB_ID_RE",
    "_SCOPE_PUBLICATION_UNCONFIRMED",
    "_SCOPE_REPORT_RECORD_MAX_BYTES",
    "_SCOPE_REVISION_CACHE_MAX",
    "_SCOPE_REVISION_CACHE_TTL_S",
    "_SCOPE_SOURCE_TOO_LARGE",
    "_SCOPE_STORAGE_ERROR",
    "_SCOPE_STORE_THREAD_LOCK",
    "_SCOPE_TYPES",
    "_SCOPE_USAGE_COUNTER_KEYS",
    "_SERVER_CONTENT_SCHEMA",
    "_SERVER_VERDICT_AUTHORITY",
    "_ScopeActionLease",
    "_ScopeReportActionConflict",
    "_ScopeReportStorageConflict",
    "_acquire_lease_path",
    "_acquire_scope_action_lease",
    "_acquire_scope_action_scope_lease",
    "_action_bound_scope_record_is_confirmed",
    "_action_lease_marker",
    "_attempted_scope_usage",
    "_complete_source_revision",
    "_confined_report_path",
    "_confined_scope_root_path",
    "_ensure_lease_marker",
    "_is_link_or_reparse",
    "_lease_path_is_live",
    "_legacy_scope_report_path",
    "_missing_scope_action_receipt",
    "_observed_scope_usage",
    "_open_scope_action_lease",
    "_prior_learnings_index",
    "_public_scope_record",
    "_read_bounded_report_bytes",
    "_read_json_record",
    "_read_lease_marker",
    "_read_or_migrate_scope_record",
    "_read_scope_action_fence",
    "_read_scope_action_lease_marker",
    "_read_scope_action_receipt",
    "_read_scope_lease_marker",
    "_read_scope_record",
    "_record_matches_scope",
    "_record_payload_matches_scope",
    "_release_retained_scope_action_leases",
    "_retain_scope_action_leases",
    "_retained_scope_action_key",
    "_scope_action_failure",
    "_scope_action_fence_path",
    "_scope_action_id",
    "_scope_action_lease_is_live",
    "_scope_action_lease_path",
    "_scope_action_leases_are_retained",
    "_scope_action_path",
    "_scope_action_scope_lease_is_live",
    "_scope_action_scope_lease_path",
    "_scope_action_success",
    "_scope_identity",
    "_scope_identity_hash",
    "_scope_lease_marker",
    "_scope_report_path",
    "_scope_store_lock",
    "_scope_usage_model",
    "_serialize_scope_action_receipt",
    "_serialize_scope_record",
    "_stat_identity",
    "_try_lock_scope_action_descriptor",
    "_unlock_scope_action_descriptor",
    "_valid_metric_observations",
    "_valid_observational_groups",
    "_valid_scope_action_fence",
    "_valid_scope_action_receipt",
    "_valid_scope_action_result",
    "_valid_scope_action_usage",
    "_valid_scope_sig_row",
    "_valid_source_receipt",
    "_valid_source_revision",
    "_validated_reports_dir",
    "_write_scope_action_fence",
    "_write_scope_action_receipt",
    "_zero_scope_usage",
]
