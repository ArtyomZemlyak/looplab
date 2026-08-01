"""Durable writer fence for an in-place whole-run reset.

The reset receipt lives outside the run directory, but ordinary event/config writers need a cheap
run-local guard while generation A is archived and generation B has not been proven yet.  The marker
is deliberately strict and operation-bound: only the reset child carrying the exact operation id may
write through it.  A malformed or unreadable marker is a fail-closed storage condition, never
permission to mutate the run.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Optional

from looplab.core.atomicio import strict_atomic_write_text


RUN_RESET_MARKER = ".looplab-resetting.json"
RUN_RESET_OPERATION_ENV = "LOOPLAB_RESET_OPERATION_ID"
RUN_RESET_MARKER_MAX_BYTES = 8 * 1024
RUN_RESET_OPERATION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


class RunResetStorageError(RuntimeError):
    """The durable reset fence cannot be read or written safely."""


class RunResetFenceError(RuntimeError):
    """A non-owner attempted to mutate a run while its reset is unresolved."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__(
            f"run reset {operation_id} is still unresolved; retry/observe that exact reset before "
            "writing this run")


def reset_marker_path(run_dir: str | os.PathLike) -> Path:
    return Path(run_dir) / RUN_RESET_MARKER


def _is_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def load_run_reset_marker(run_dir: str | os.PathLike) -> Optional[dict[str, Any]]:
    path = reset_marker_path(run_dir)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunResetStorageError(f"reset marker cannot be inspected: {exc}") from exc
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise RunResetStorageError("reset marker is not a regular service-owned file")
    if before.st_size > RUN_RESET_MARKER_MAX_BYTES:
        raise RunResetStorageError("reset marker exceeds its safety limit")
    try:
        with path.open("rb") as stream:
            raw = stream.read(RUN_RESET_MARKER_MAX_BYTES + 1)
        after = path.lstat()
    except OSError as exc:
        raise RunResetStorageError(f"reset marker cannot be read: {exc}") from exc
    identity = lambda info: (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        int(getattr(info, "st_file_attributes", 0) or 0),
    )
    if identity(before) != identity(after):
        raise RunResetStorageError("reset marker changed while it was being read")
    if len(raw) > RUN_RESET_MARKER_MAX_BYTES:
        raise RunResetStorageError("reset marker exceeds its safety limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RunResetStorageError(f"reset marker cannot be decoded: {exc}") from exc
    if (not isinstance(value, dict)
            or set(value) != {"version", "operation_id", "expected_generation", "receipt_name"}
            or value.get("version") != 1
            or not isinstance(value.get("operation_id"), str)
            or RUN_RESET_OPERATION_RE.fullmatch(value["operation_id"]) is None
            or not isinstance(value.get("expected_generation"), str)
            or _RUN_GENERATION_RE.fullmatch(value["expected_generation"]) is None
            or not isinstance(value.get("receipt_name"), str)
            or not value["receipt_name"]
            or Path(value["receipt_name"]).name != value["receipt_name"]):
        raise RunResetStorageError("reset marker is malformed")
    return value


def publish_run_reset_marker(
        run_dir: str | os.PathLike, *, operation_id: str,
        expected_generation: str, receipt_name: str) -> dict[str, Any]:
    value = {
        "version": 1,
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "receipt_name": receipt_name,
    }
    if (RUN_RESET_OPERATION_RE.fullmatch(operation_id) is None
            or _RUN_GENERATION_RE.fullmatch(expected_generation) is None
            or Path(receipt_name).name != receipt_name):
        raise ValueError("invalid reset marker identity")
    path = reset_marker_path(run_dir)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > RUN_RESET_MARKER_MAX_BYTES:
            raise ValueError("reset marker exceeds its safety limit")
        strict_atomic_write_text(path, encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise RunResetStorageError(f"reset marker could not be published durably: {exc}") from exc
    # Read back the exact service contract. A strict write can fail after replacement became visible;
    # callers retry the same operation, and this read is the authority on every later decision.
    confirmed = load_run_reset_marker(run_dir)
    if confirmed != value:
        raise RunResetStorageError("reset marker publication could not be confirmed")
    return value


def assert_run_reset_write_allowed(
        run_dir: str | os.PathLike, operation_id: Optional[str] = None
        ) -> Optional[dict[str, Any]]:
    marker = load_run_reset_marker(run_dir)
    if marker is None:
        return None
    supplied = operation_id or os.environ.get(RUN_RESET_OPERATION_ENV, "")
    if supplied == marker["operation_id"]:
        return marker
    raise RunResetFenceError(marker["operation_id"])


def clear_run_reset_marker(
        run_dir: str | os.PathLike, operation_id: str) -> bool:
    marker = load_run_reset_marker(run_dir)
    if marker is None or marker["operation_id"] != operation_id:
        return False
    path = reset_marker_path(run_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RunResetStorageError(f"reset marker could not be retired: {exc}") from exc
    return True


__all__ = [
    "RUN_RESET_MARKER", "RUN_RESET_MARKER_MAX_BYTES", "RUN_RESET_OPERATION_ENV",
    "RUN_RESET_OPERATION_RE",
    "RunResetFenceError", "RunResetStorageError", "assert_run_reset_write_allowed",
    "clear_run_reset_marker", "load_run_reset_marker", "publish_run_reset_marker",
    "reset_marker_path",
]
