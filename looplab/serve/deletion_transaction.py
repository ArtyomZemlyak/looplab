"""Strict durable receipts for whole-run deletion."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Optional

from looplab.core.atomicio import strict_atomic_write_text
from looplab.core.run_deletion import RUN_DELETION_OPERATION_RE, run_deletion_key


DELETE_RECEIPT_PREFIX = ".looplab-delete-receipt-"
DELETE_QUARANTINE_PREFIX = ".looplab-delete-quarantine-"
DELETE_RECEIPT_VERSION = 1
DELETE_RECEIPT_MAX_BYTES = 64 * 1024
DELETE_PHASES = (
    "prepared", "fenced", "quarantining", "quarantined", "metadata_committed",
    "purging", "retiring_fence", "succeeded",
)
_PHASE_INDEX = {phase: index for index, phase in enumerate(DELETE_PHASES)}
_ALL_PHASES = frozenset((*DELETE_PHASES, "quarantine_ambiguous", "superseded"))
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_KEYS = frozenset({
    "version", "id", "run_id", "run_key", "expected_generation", "expected_seq",
    "status", "phase", "created_at", "updated_at",
})
_IMMUTABLE_FIELDS = frozenset({
    "version", "id", "run_id", "run_key", "expected_generation", "expected_seq", "created_at",
})


class DeletionReceiptError(RuntimeError):
    """A deletion receipt is unavailable, malformed, or changed while being read."""


def deletion_receipt_path(srv, rd: Path, operation_id: str) -> Path:
    if RUN_DELETION_OPERATION_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid deletion operation id")
    run_key = run_deletion_key(rd)
    sequence_path = srv.commands._sequence_path(rd)
    if sequence_path.stem != run_key:
        raise DeletionReceiptError("deletion identity disagrees with the command sequencer")
    return srv.root.resolve() / f"{DELETE_RECEIPT_PREFIX}{run_key}.{operation_id}.json"


def deletion_quarantine_path(srv, rd: Path, operation_id: str) -> Path:
    run_key = run_deletion_key(rd)
    if RUN_DELETION_OPERATION_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid deletion operation id")
    return srv.root.resolve() / f"{DELETE_QUARANTINE_PREFIX}{run_key}.{operation_id}"


def _is_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _regular_file(path: Path) -> Optional[os.stat_result]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DeletionReceiptError(f"deletion receipt cannot be inspected: {exc}") from exc
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise DeletionReceiptError("deletion receipt is not a regular service-owned file")
    return info


def _validate_receipt(value: Any, *, path: Path) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != _RECEIPT_KEYS
            or value.get("version") != DELETE_RECEIPT_VERSION
            or not isinstance(value.get("id"), str)
            or RUN_DELETION_OPERATION_RE.fullmatch(value["id"]) is None
            or not isinstance(value.get("run_id"), str) or not value["run_id"]
            or Path(value["run_id"]).name != value["run_id"]
            or value["run_id"] in {".", ".."}
            or not isinstance(value.get("run_key"), str)
            or _SHA256_RE.fullmatch(value["run_key"]) is None
            or not isinstance(value.get("expected_generation"), str)
            or _SHA256_RE.fullmatch(value["expected_generation"]) is None
            or type(value.get("expected_seq")) is not int or value["expected_seq"] < -1
            or value.get("phase") not in _ALL_PHASES
            or value.get("status") != (
                value.get("phase") if value.get("phase") in {"succeeded", "superseded"}
                else "pending")
            or isinstance(value.get("created_at"), bool)
            or not isinstance(value.get("created_at"), (int, float))
            or not math.isfinite(value["created_at"])
            or isinstance(value.get("updated_at"), bool)
            or not isinstance(value.get("updated_at"), (int, float))
            or not math.isfinite(value["updated_at"])
            or value["updated_at"] < value["created_at"]):
        raise DeletionReceiptError("deletion receipt is malformed")
    expected_name = (
        f"{DELETE_RECEIPT_PREFIX}{value['run_key']}.{value['id']}.json")
    if path.name != expected_name:
        raise DeletionReceiptError("deletion receipt path and identity disagree")
    return value


def load_deletion_receipt(path: Path) -> Optional[dict[str, Any]]:
    before = _regular_file(path)
    if before is None:
        return None
    if before.st_size > DELETE_RECEIPT_MAX_BYTES:
        raise DeletionReceiptError("deletion receipt exceeds its safety limit")
    try:
        with path.open("rb") as stream:
            raw = stream.read(DELETE_RECEIPT_MAX_BYTES + 1)
        after = _regular_file(path)
    except OSError as exc:
        raise DeletionReceiptError(f"deletion receipt cannot be read: {exc}") from exc
    identity = lambda info: (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        int(getattr(info, "st_file_attributes", 0) or 0),
    )
    if after is None or identity(before) != identity(after):
        raise DeletionReceiptError("deletion receipt changed while it was being read")
    if len(raw) > DELETE_RECEIPT_MAX_BYTES:
        raise DeletionReceiptError("deletion receipt exceeds its safety limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DeletionReceiptError(f"deletion receipt cannot be decoded: {exc}") from exc
    return _validate_receipt(value, path=path)


def save_deletion_receipt(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    value = _validate_receipt(value, path=path)
    current = load_deletion_receipt(path)
    if current is not None:
        if any(current.get(key) != value.get(key) for key in _IMMUTABLE_FIELDS):
            raise DeletionReceiptError("deletion receipt immutable identity changed")
        if value["phase"] == "quarantine_ambiguous":
            if current["phase"] not in {"quarantining", "quarantine_ambiguous"}:
                raise DeletionReceiptError(
                    f"invalid deletion receipt transition {current['phase']} -> "
                    "quarantine_ambiguous")
        elif current["phase"] == "quarantine_ambiguous":
            raise DeletionReceiptError(
                "an ambiguous quarantine move requires manual storage recovery")
        elif value["phase"] == "superseded":
            if current["phase"] not in {"prepared", "superseded"}:
                raise DeletionReceiptError(
                    f"invalid deletion receipt transition {current['phase']} -> superseded")
        elif current["phase"] == "superseded":
            if value["phase"] != "superseded":
                raise DeletionReceiptError("a superseded deletion receipt is terminal")
        else:
            old_phase = _PHASE_INDEX[current["phase"]]
            new_phase = _PHASE_INDEX[value["phase"]]
            if new_phase < old_phase or new_phase > old_phase + 1:
                raise DeletionReceiptError(
                    f"invalid deletion receipt transition {current['phase']} -> {value['phase']}")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > DELETE_RECEIPT_MAX_BYTES:
            raise ValueError("deletion receipt exceeds its safety limit")
        strict_atomic_write_text(path, encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise DeletionReceiptError(f"deletion receipt could not be published durably: {exc}") from exc
    confirmed = load_deletion_receipt(path)
    if confirmed != value:
        raise DeletionReceiptError("deletion receipt publication could not be confirmed")
    return confirmed


def prepare_deletion_receipt(
        srv, rd: Path, *, operation_id: str, expected_generation: str,
        expected_seq: int) -> dict[str, Any]:
    now = time.time()
    return {
        "version": DELETE_RECEIPT_VERSION,
        "id": operation_id,
        "run_id": rd.name,
        "run_key": run_deletion_key(rd),
        "expected_generation": expected_generation,
        "expected_seq": expected_seq,
        "status": "pending",
        "phase": "prepared",
        "created_at": now,
        "updated_at": now,
    }


def advance_deletion_receipt(
        path: Path, receipt: dict[str, Any], phase: str) -> dict[str, Any]:
    if phase not in _PHASE_INDEX:
        raise ValueError("invalid deletion phase")
    return save_deletion_receipt(path, {
        **receipt,
        "phase": phase,
        "status": "succeeded" if phase == "succeeded" else "pending",
        "updated_at": time.time(),
    })


def supersede_prepared_deletion_receipt(
        path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Terminally retire an unfenced prepared intent after its inspected snapshot changed."""
    if receipt.get("phase") not in {"prepared", "superseded"}:
        raise DeletionReceiptError("only an unfenced prepared deletion can be superseded")
    return save_deletion_receipt(path, {
        **receipt,
        "phase": "superseded",
        "status": "superseded",
        "updated_at": time.time(),
    })


def mark_deletion_quarantine_ambiguous(
        path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Fail closed when Windows reports an error after the quarantine becomes visible."""
    if receipt.get("phase") not in {"quarantining", "quarantine_ambiguous"}:
        raise DeletionReceiptError(
            "only a quarantining deletion can record an ambiguous move")
    return save_deletion_receipt(path, {
        **receipt,
        "phase": "quarantine_ambiguous",
        "status": "pending",
        "updated_at": time.time(),
    })


def deletion_receipts_for_run(srv, rd: Path) -> list[tuple[Path, dict[str, Any]]]:
    key = run_deletion_key(rd)
    paths = srv.root.resolve().glob(f"{DELETE_RECEIPT_PREFIX}{key}.*.json")
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        receipt = load_deletion_receipt(path)
        if receipt is not None:
            found.append((path, receipt))
    return found


def deletion_result(receipt: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = {
        "ok": receipt.get("status") == "succeeded",
        "status": receipt.get("status"),
        "phase": receipt.get("phase"),
        "operation_id": receipt.get("id"),
        "run_id": receipt.get("run_id"),
        "expected_generation": receipt.get("expected_generation"),
        "expected_seq": receipt.get("expected_seq"),
    }
    result.update(extra)
    return result


__all__ = [
    "DELETE_PHASES", "DELETE_QUARANTINE_PREFIX", "DELETE_RECEIPT_PREFIX",
    "DeletionReceiptError", "advance_deletion_receipt", "deletion_quarantine_path",
    "deletion_receipt_path", "deletion_receipts_for_run", "deletion_result",
    "load_deletion_receipt", "mark_deletion_quarantine_ambiguous",
    "prepare_deletion_receipt", "save_deletion_receipt",
    "supersede_prepared_deletion_receipt",
]
