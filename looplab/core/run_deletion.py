"""Durable root-side writer fence for whole-run deletion.

The fence lives beside run directories rather than inside the run being removed.  It therefore
survives the atomic run -> quarantine rename and still stops a direct CLI, EventStore, config or
derived-index writer that was already waiting on one of the run's old lock files.  Only the exact
deletion operation may pass the fence while it owns the writer locks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional

from looplab.core.atomicio import strict_atomic_write_text, strict_fsync_parent


RUN_DELETION_FENCE_PREFIX = ".looplab-delete-fence-"
RUN_DELETION_OPERATION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RUN_DELETION_FENCE_MAX_BYTES = 8 * 1024
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class RunDeletionStorageError(RuntimeError):
    """The deletion fence cannot be read, written, or retired safely."""


class RunDeletionFenceError(RuntimeError):
    """A non-owner attempted to write a run while deletion is unresolved."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__(
            f"run deletion {operation_id} is unresolved; observe or retry that exact deletion "
            "before writing this run")


def run_deletion_key(run_dir: str | os.PathLike) -> str:
    identity = str(Path(run_dir).resolve(strict=False))
    if os.name == "nt":
        identity = os.path.normcase(identity)
    elif sys.platform == "darwin":
        identity = unicodedata.normalize("NFD", identity).casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns),
        int(info.st_ctime_ns), int(getattr(info, "st_file_attributes", 0) or 0),
    )


def run_deletion_snapshot_token(
        events_path: str | os.PathLike, generation: str) -> str:
    """Return a replacement-sensitive deletion identity, including for an empty event log."""
    if isinstance(generation, str) and _RUN_GENERATION_RE.fullmatch(generation):
        return generation
    if generation:
        raise RunDeletionStorageError("run generation is malformed")
    path = Path(events_path)
    try:
        before = path.lstat()
        if (_is_reparse(before) or not stat.S_ISREG(before.st_mode)
                or before.st_size != 0):
            raise RunDeletionStorageError(
                "an empty run deletion identity requires a stable regular empty event log")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(1)
        after = path.lstat()
    except RunDeletionStorageError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise RunDeletionStorageError(
            f"empty run deletion identity is unavailable: {exc}") from exc
    identity = _file_identity(before)
    if (data or not stat.S_ISREG(opened.st_mode) or opened.st_size != 0
            or identity != _file_identity(opened) or identity != _file_identity(after)):
        raise RunDeletionStorageError(
            "the empty event log changed while its deletion identity was inspected")
    material = json.dumps({
        "version": 1,
        "run_key": run_deletion_key(path.parent),
        "file_identity": identity,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(b"looplab-empty-run-deletion-v1\0" + material).hexdigest()


def run_deletion_fence_path(run_dir: str | os.PathLike) -> Path:
    rd = Path(run_dir).resolve(strict=False)
    return rd.parent / f"{RUN_DELETION_FENCE_PREFIX}{run_deletion_key(rd)}.json"


def _is_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def load_run_deletion_fence(run_dir: str | os.PathLike) -> Optional[dict[str, Any]]:
    path = run_deletion_fence_path(run_dir)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunDeletionStorageError(f"deletion fence cannot be inspected: {exc}") from exc
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise RunDeletionStorageError("deletion fence is not a regular service-owned file")
    if before.st_size > RUN_DELETION_FENCE_MAX_BYTES:
        raise RunDeletionStorageError("deletion fence exceeds its safety limit")
    try:
        with path.open("rb") as stream:
            raw = stream.read(RUN_DELETION_FENCE_MAX_BYTES + 1)
        after = path.lstat()
    except OSError as exc:
        raise RunDeletionStorageError(f"deletion fence cannot be read: {exc}") from exc
    identity = lambda info: (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        int(getattr(info, "st_file_attributes", 0) or 0),
    )
    if identity(before) != identity(after):
        raise RunDeletionStorageError("deletion fence changed while it was being read")
    if len(raw) > RUN_DELETION_FENCE_MAX_BYTES:
        raise RunDeletionStorageError("deletion fence exceeds its safety limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RunDeletionStorageError(f"deletion fence cannot be decoded: {exc}") from exc
    expected_key = run_deletion_key(run_dir)
    if (not isinstance(value, dict)
            or set(value) != {
                "version", "operation_id", "run_key", "expected_generation", "expected_seq",
                "receipt_name",
            }
            or value.get("version") != 1
            or not isinstance(value.get("operation_id"), str)
            or RUN_DELETION_OPERATION_RE.fullmatch(value["operation_id"]) is None
            or not isinstance(value.get("run_key"), str)
            or _RUN_KEY_RE.fullmatch(value["run_key"]) is None
            or value["run_key"] != expected_key
            or not isinstance(value.get("expected_generation"), str)
            or _RUN_GENERATION_RE.fullmatch(value["expected_generation"]) is None
            or type(value.get("expected_seq")) is not int or value["expected_seq"] < -1
            or not isinstance(value.get("receipt_name"), str) or not value["receipt_name"]
            or Path(value["receipt_name"]).name != value["receipt_name"]):
        raise RunDeletionStorageError("deletion fence is malformed")
    return value


def publish_run_deletion_fence(
        run_dir: str | os.PathLike, *, operation_id: str, expected_generation: str,
        expected_seq: int, receipt_name: str) -> dict[str, Any]:
    run_key = run_deletion_key(run_dir)
    value = {
        "version": 1,
        "operation_id": operation_id,
        "run_key": run_key,
        "expected_generation": expected_generation,
        "expected_seq": expected_seq,
        "receipt_name": receipt_name,
    }
    if (RUN_DELETION_OPERATION_RE.fullmatch(operation_id) is None
            or _RUN_GENERATION_RE.fullmatch(expected_generation) is None
            or type(expected_seq) is not int or expected_seq < -1
            or Path(receipt_name).name != receipt_name):
        raise ValueError("invalid deletion fence identity")
    path = run_deletion_fence_path(run_dir)
    current = load_run_deletion_fence(run_dir)
    if current is not None:
        if current != value:
            raise RunDeletionStorageError("another deletion operation owns this run")
        return current
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > RUN_DELETION_FENCE_MAX_BYTES:
            raise ValueError("deletion fence exceeds its safety limit")
        strict_atomic_write_text(path, encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise RunDeletionStorageError(
            f"deletion fence could not be published durably: {exc}") from exc
    confirmed = load_run_deletion_fence(run_dir)
    if confirmed != value:
        raise RunDeletionStorageError("deletion fence publication could not be confirmed")
    return value


def assert_run_deletion_write_allowed(
        run_dir: str | os.PathLike, operation_id: Optional[str] = None
        ) -> Optional[dict[str, Any]]:
    fence = load_run_deletion_fence(run_dir)
    if fence is None:
        return None
    if operation_id and operation_id == fence["operation_id"]:
        return fence
    raise RunDeletionFenceError(fence["operation_id"])


def clear_run_deletion_fence(run_dir: str | os.PathLike, operation_id: str) -> bool:
    fence = load_run_deletion_fence(run_dir)
    if fence is None:
        return True
    if fence["operation_id"] != operation_id:
        return False
    path = run_deletion_fence_path(run_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        strict_fsync_parent(path)
        return True
    except OSError as exc:
        raise RunDeletionStorageError(f"deletion fence could not be retired: {exc}") from exc
    try:
        strict_fsync_parent(path)
    except OSError as exc:
        raise RunDeletionStorageError(
            f"deletion fence retirement could not be confirmed durably: {exc}") from exc
    try:
        return load_run_deletion_fence(run_dir) is None
    except RunDeletionStorageError:
        raise


__all__ = [
    "RUN_DELETION_FENCE_MAX_BYTES", "RUN_DELETION_FENCE_PREFIX",
    "RUN_DELETION_OPERATION_RE", "RunDeletionFenceError", "RunDeletionStorageError",
    "assert_run_deletion_write_allowed", "clear_run_deletion_fence",
    "load_run_deletion_fence", "publish_run_deletion_fence", "run_deletion_fence_path",
    "run_deletion_key", "run_deletion_snapshot_token",
]
