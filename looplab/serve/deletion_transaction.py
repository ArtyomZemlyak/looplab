"""Strict durable receipts for whole-run deletion.

The receipt SCHEMA — its key set, its monotonic phase index and the two terminal branches that step
outside it — is this module's; the read-then-verify load and the
validate/immutable/transition/publish/confirm save it shares with `reset_transaction.py` live in
`serve/durable_op.py` (doc 25 SC-06).
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Optional

from looplab.core.atomicio import atomic_write_text
from looplab.core.run_deletion import RUN_DELETION_OPERATION_RE, run_deletion_key
from looplab.serve.durable_op import (
    ReceiptProtocol, load_receipt, receipts_for_run, save_receipt)


DELETE_RECEIPT_PREFIX = ".looplab-delete-receipt-"
DELETE_QUARANTINE_PREFIX = ".looplab-delete-quarantine-"
DELETE_IDENTITY_PREFIX = ".looplab-delete-identity-"
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


def deletion_identity_path(srv, rd: Path, operation_id: str) -> Path:
    """Where this operation's MEMORY-CASCADE identity is parked, beside its receipt.

    A SIDECAR rather than two more receipt keys, because `_RECEIPT_KEYS` is a closed set whose
    loader refuses extras — widening it would retro-invalidate every receipt already on disk.

    It exists because the two facts the cross-run purge needs (`run_uid`, and the run's OWN
    `memory_dir`) live only inside the run directory, and the purge runs after that directory is
    gone. On the first attempt the caller reads them and uses them immediately; on a RETRY of a
    deletion that had already left the workspace — the four documented "pending" phases, or a crash
    mid-transaction — the read returns nothing and the uid was lost permanently. The server then
    told the operator to "pass the run_uid and memory_dir from the original deletion receipt",
    fields no receipt and no 202 body ever carried, and the UI's retry button posted two empty
    strings forever. This is what makes that instruction true.

    Its own PREFIX, not a suffix on the receipt's name: `deletion_receipts_for_run` globs
    `{DELETE_RECEIPT_PREFIX}{run_key}.*` and hands every hit to the strict receipt loader, so a
    sidecar sitting inside that glob is read as a malformed receipt and the whole deletion answers
    503 `delete_outcome_unknown`."""
    receipt = deletion_receipt_path(srv, rd, operation_id)     # also does the identity cross-check
    return receipt.with_name(
        f"{DELETE_IDENTITY_PREFIX}{run_deletion_key(rd)}.{operation_id}.json")


def save_deletion_identity(srv, rd: Path, operation_id: str, identity: dict) -> None:
    """Park the cascade identity for a retry. Best-effort: failing to write it must not fail the
    deletion, which is the operator's actual request — it only costs the retry its uid."""
    try:
        path = deletion_identity_path(srv, rd, operation_id)
        atomic_write_text(path, json.dumps({
            "version": DELETE_RECEIPT_VERSION,
            "run_id": str(identity.get("run_id") or ""),
            "run_uid": str(identity.get("run_uid") or ""),
            "memory_dir": str(identity.get("memory_dir") or ""),
        }, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def load_deletion_identity(srv, rd: Path, operation_id: str) -> dict:
    """The parked identity, or `{}`. Never raises: an unreadable sidecar degrades this attempt to
    the same "identity unknown" answer it would have given with no sidecar at all."""
    try:
        data = json.loads(deletion_identity_path(srv, rd, operation_id).read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict) or data.get("version") != DELETE_RECEIPT_VERSION:
        return {}
    run_id = data.get("run_id")
    run_uid = data.get("run_uid")
    memory_dir = data.get("memory_dir")
    if not all(isinstance(v, str) for v in (run_id, run_uid, memory_dir)) or not run_id:
        return {}
    return {"run_id": run_id, "run_uid": run_uid, "memory_dir": memory_dir}


def deletion_quarantine_path(srv, rd: Path, operation_id: str) -> Path:
    run_key = run_deletion_key(rd)
    if RUN_DELETION_OPERATION_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid deletion operation id")
    return srv.root.resolve() / f"{DELETE_QUARANTINE_PREFIX}{run_key}.{operation_id}"


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


def _check_transition(current: dict[str, Any], value: dict[str, Any]) -> None:
    """Deletion's phase lattice: a MONOTONIC index, plus two branches that leave it terminally.

    Not a table like reset's, and the difference is load-bearing rather than stylistic. Deletion
    moves forward by exactly one rung and never back — a rung it has already crossed has mutated the
    filesystem. `quarantine_ambiguous` and `superseded` sit OUTSIDE the index and are absorbing:
    an ambiguous quarantine move is the one state this protocol refuses to resume from at all,
    because Windows reported a failure after the destination became visible and only a human can say
    which side the run is on.
    """
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


_PROTOCOL = ReceiptProtocol(
    label="deletion receipt",
    error_cls=DeletionReceiptError,
    max_bytes=DELETE_RECEIPT_MAX_BYTES,
    validate=_validate_receipt,
    immutable=_IMMUTABLE_FIELDS,
    check_transition=_check_transition,
)


def load_deletion_receipt(path: Path) -> Optional[dict[str, Any]]:
    return load_receipt(path, _PROTOCOL)


def save_deletion_receipt(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    return save_receipt(path, value, _PROTOCOL, load=load_deletion_receipt)


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
    # Deletion's run key comes from `run_deletion_key(rd)` — the run's own filesystem identity —
    # while reset derives its key from the command sequencer's file stem. Both bindings are exact and
    # neither may answer for the other, which is why `durable_op.receipts_for_run` scans an
    # already-derived root and pattern instead of taking `(srv, rd)`.
    key = run_deletion_key(rd)
    return receipts_for_run(
        srv.root.resolve(), f"{DELETE_RECEIPT_PREFIX}{key}.*.json", load_deletion_receipt)


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
    "DELETE_IDENTITY_PREFIX", "DELETE_PHASES", "DELETE_QUARANTINE_PREFIX",
    "DELETE_RECEIPT_PREFIX",
    "DeletionReceiptError", "advance_deletion_receipt", "deletion_identity_path",
    "deletion_quarantine_path",
    "deletion_receipt_path", "deletion_receipts_for_run", "deletion_result",
    "load_deletion_identity", "load_deletion_receipt", "mark_deletion_quarantine_ambiguous",
    "prepare_deletion_receipt", "save_deletion_identity", "save_deletion_receipt",
    "supersede_prepared_deletion_receipt",
]
