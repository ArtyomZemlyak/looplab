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
from pathlib import Path
from typing import Any, Optional

from looplab.core.atomicio import file_identity, strict_fsync_parent
from looplab.core.fence import (
    FENCE_GENERATION_RE, FENCE_MAX_BYTES, FENCE_OPERATION_RE,
    load_bounded_json_marker, publish_bounded_json_marker)
from looplab.core.pathsafe import filesystem_identity, is_reparse


RUN_DELETION_FENCE_PREFIX = ".looplab-delete-fence-"
# Aliases, not second declarations — see `core/fence.py` (doc 25 CO-01).
RUN_DELETION_OPERATION_RE = FENCE_OPERATION_RE
RUN_DELETION_FENCE_MAX_BYTES = FENCE_MAX_BYTES
_RUN_GENERATION_RE = FENCE_GENERATION_RE
# The run key is a sha256 of the filesystem identity, so it happens to share the generation's shape
# while meaning something else entirely. Kept as its own name for exactly that reason.
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
    identity = filesystem_identity(str(Path(run_dir).resolve(strict=False)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
        if (is_reparse(before) or not stat.S_ISREG(before.st_mode)
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
    identity = file_identity(before)
    if (data or not stat.S_ISREG(opened.st_mode) or opened.st_size != 0
            or identity != file_identity(opened) or identity != file_identity(after)):
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


def _deletion_fence_is_valid(value: dict[str, Any], expected_key: str) -> bool:
    """This module's half of the fence: WHAT a deletion fence must say. Unlike the reset marker it
    also binds the fence to the exact run — `run_key` is checked against the key derived from the
    directory being asked about, so a fence file moved or copied beside a different run is malformed
    rather than authoritative. The read protocol itself lives in `core/fence.py` (doc 25 CO-01)."""
    return (set(value) == {
                "version", "operation_id", "run_key", "expected_generation", "expected_seq",
                "receipt_name",
            }
            and value.get("version") == 1
            and isinstance(value.get("operation_id"), str)
            and RUN_DELETION_OPERATION_RE.fullmatch(value["operation_id"]) is not None
            and isinstance(value.get("run_key"), str)
            and _RUN_KEY_RE.fullmatch(value["run_key"]) is not None
            and value["run_key"] == expected_key
            and isinstance(value.get("expected_generation"), str)
            and _RUN_GENERATION_RE.fullmatch(value["expected_generation"]) is not None
            and type(value.get("expected_seq")) is int and value["expected_seq"] >= -1
            and isinstance(value.get("receipt_name"), str) and bool(value["receipt_name"])
            and Path(value["receipt_name"]).name == value["receipt_name"])


def load_run_deletion_fence(run_dir: str | os.PathLike) -> Optional[dict[str, Any]]:
    expected_key = run_deletion_key(run_dir)
    return load_bounded_json_marker(
        run_deletion_fence_path(run_dir),
        label="deletion fence",
        error_cls=RunDeletionStorageError,
        validate=lambda value: _deletion_fence_is_valid(value, expected_key),
        max_bytes=RUN_DELETION_FENCE_MAX_BYTES,
    )


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
    # Unlike the reset marker, an existing fence is NOT overwritten: a different operation already
    # owns this run, and replacing its fence would hand ownership to a second deleter. An identical
    # fence is the same operation retrying, which is idempotent.
    current = load_run_deletion_fence(run_dir)
    if current is not None:
        if current != value:
            raise RunDeletionStorageError("another deletion operation owns this run")
        return current
    return publish_bounded_json_marker(
        run_deletion_fence_path(run_dir), value,
        label="deletion fence",
        error_cls=RunDeletionStorageError,
        confirm=lambda: load_run_deletion_fence(run_dir),
        max_bytes=RUN_DELETION_FENCE_MAX_BYTES,
    )


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
