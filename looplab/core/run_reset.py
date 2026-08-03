"""Durable writer fence for an in-place whole-run reset.

The reset receipt lives outside the run directory, but ordinary event/config writers need a cheap
run-local guard while generation A is archived and generation B has not been proven yet.  The marker
is deliberately strict and operation-bound: only the reset child carrying the exact operation id may
write through it.  A malformed or unreadable marker is a fail-closed storage condition, never
permission to mutate the run.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from looplab.core.fence import (
    FENCE_GENERATION_RE, FENCE_MAX_BYTES, FENCE_OPERATION_RE,
    load_bounded_json_marker, publish_bounded_json_marker)


RUN_RESET_MARKER = ".looplab-resetting.json"
RUN_RESET_OPERATION_ENV = "LOOPLAB_RESET_OPERATION_ID"
# Aliases, not second declarations: the shapes and the 8 KiB cap are the fence PROTOCOL's, shared
# with the deletion fence (doc 25 CO-01), and these names stay because callers/tests import them.
RUN_RESET_MARKER_MAX_BYTES = FENCE_MAX_BYTES
RUN_RESET_OPERATION_RE = FENCE_OPERATION_RE
_RUN_GENERATION_RE = FENCE_GENERATION_RE


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


def _reset_marker_is_valid(value: dict[str, Any]) -> bool:
    """This module's half of the fence: WHAT a reset marker must say. The protocol (bounded,
    identity-checked read; fail-closed on anything unreadable) lives in `core/fence.py`."""
    return (set(value) == {"version", "operation_id", "expected_generation", "receipt_name"}
            and value.get("version") == 1
            and isinstance(value.get("operation_id"), str)
            and RUN_RESET_OPERATION_RE.fullmatch(value["operation_id"]) is not None
            and isinstance(value.get("expected_generation"), str)
            and _RUN_GENERATION_RE.fullmatch(value["expected_generation"]) is not None
            and isinstance(value.get("receipt_name"), str)
            and bool(value["receipt_name"])
            and Path(value["receipt_name"]).name == value["receipt_name"])


def load_run_reset_marker(run_dir: str | os.PathLike) -> Optional[dict[str, Any]]:
    return load_bounded_json_marker(
        reset_marker_path(run_dir),
        label="reset marker",
        error_cls=RunResetStorageError,
        validate=_reset_marker_is_valid,
        max_bytes=RUN_RESET_MARKER_MAX_BYTES,
    )


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
    # The read-back confirm goes through THIS module's loader, so publication is proven against the
    # same schema every later decision reads it with.
    return publish_bounded_json_marker(
        reset_marker_path(run_dir), value,
        label="reset marker",
        error_cls=RunResetStorageError,
        confirm=lambda: load_run_reset_marker(run_dir),
        max_bytes=RUN_RESET_MARKER_MAX_BYTES,
    )


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
