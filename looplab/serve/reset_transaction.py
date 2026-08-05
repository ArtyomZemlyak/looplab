"""Durable receipt primitives for whole-run reset/replay.

The receipt SCHEMA — its key set, its phase lattice, its identity binding — is this module's; the
read-then-verify load and the validate/immutable/transition/publish/confirm save it shares with
`deletion_transaction.py` live in `serve/durable_op.py` (doc 25 SC-06).
"""
from __future__ import annotations

import math
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from looplab.core.run_reset import (
    RUN_RESET_OPERATION_RE, RunResetStorageError, clear_run_reset_marker,
    load_run_reset_marker)
from looplab.serve.durable_op import (
    ReceiptProtocol, load_receipt, receipts_for_run, save_receipt)


RESET_RECEIPT_PREFIX = ".looplab-reset-receipt-"
RESET_RECEIPT_VERSION = 1
RESET_RECEIPT_MAX_BYTES = 64 * 1024 * 1024
RESET_ARTIFACT_NAMES = (
    ".llm-usage-outbox", "spans.jsonl", "spans.index.jsonl",
    "readmodel.sqlite-wal", "readmodel.sqlite-shm", "readmodel.sqlite",
    "nodes", "chat.jsonl", "events.jsonl",
)
RESET_PHASES = frozenset({
    "prepared", "archiving", "archived", "popen_pending", "popen_returned",
    "launch_uncertain", "succeeded", "superseded",
})
RESET_STATUSES = frozenset({"pending", "accepted", "succeeded", "superseded"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_NAME_RE = re.compile(
    rf"^{re.escape(RESET_RECEIPT_PREFIX)}(?P<sequence>[0-9a-f]{{64}})\."
    r"(?P<operation>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.json$")
_RECEIPT_KEYS = frozenset({
    "version", "id", "run_id", "run_key", "expected_generation", "status", "phase",
    "archive_stamp", "artifacts", "task_stage", "task_digest", "effective_config",
    "new_generation", "created_at", "updated_at",
})
_STATUS_FOR_PHASE = {
    "prepared": "pending",
    "archiving": "pending",
    "archived": "pending",
    "popen_pending": "pending",
    "popen_returned": "accepted",
    "launch_uncertain": "pending",
    "succeeded": "succeeded",
    "superseded": "superseded",
}
_PHASE_TRANSITIONS = {
    "prepared": frozenset({"prepared", "archiving", "superseded"}),
    "archiving": frozenset({"archiving", "archived", "superseded"}),
    "archived": frozenset({"archived", "popen_pending", "superseded"}),
    "popen_pending": frozenset({
        "popen_pending", "popen_returned", "launch_uncertain", "archived", "succeeded"}),
    "popen_returned": frozenset({
        "popen_returned", "launch_uncertain", "archived", "succeeded"}),
    "launch_uncertain": frozenset({"launch_uncertain", "archived", "succeeded"}),
    "succeeded": frozenset({"succeeded"}),
    "superseded": frozenset({"superseded"}),
}
_IMMUTABLE_RECEIPT_FIELDS = frozenset({
    "version", "id", "run_id", "run_key", "expected_generation", "archive_stamp",
    "artifacts", "task_stage", "task_digest", "effective_config", "created_at",
})


class ResetReceiptError(RuntimeError):
    """A reset receipt is unavailable, malformed, or changed while being read."""


def reset_receipt_path(srv, rd: Path, operation_id: str) -> Path:
    if RUN_RESET_OPERATION_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid reset operation id")
    sequence_path = srv.commands._sequence_path(rd)
    root = srv.root.resolve()
    expected_lock_root = root / ".command-locks"
    if (os.path.normcase(os.path.abspath(sequence_path.parent))
            != os.path.normcase(os.path.abspath(expected_lock_root))
            or _SHA256_RE.fullmatch(sequence_path.stem) is None):
        raise ResetReceiptError("run sequencer path is outside the canonical lock namespace")
    return root / (
        f"{RESET_RECEIPT_PREFIX}{sequence_path.stem}.{operation_id}.json")


def validate_reset_binding(
        srv, rd: Path, marker: dict[str, Any], receipt_path: Path,
        receipt: Optional[dict[str, Any]] = None) -> Path:
    """Bind marker, root receipt, run identity, generation and sequencer digest exactly."""
    expected_path = reset_receipt_path(srv, rd, marker["operation_id"])
    same_path = os.path.normcase(os.path.abspath(receipt_path)) == os.path.normcase(
        os.path.abspath(expected_path))
    if not same_path or marker.get("receipt_name") != expected_path.name:
        raise ResetReceiptError("reset marker does not name the run's derived receipt path")
    if receipt is not None and (
            receipt.get("id") != marker.get("operation_id")
            or receipt.get("run_id") != rd.name
            or receipt.get("run_key") != expected_path.name.removeprefix(
                RESET_RECEIPT_PREFIX).split(".", 1)[0]
            or receipt.get("expected_generation") != marker.get("expected_generation")):
        raise ResetReceiptError("reset marker and receipt do not bind the same run generation")
    return expected_path


def _valid_artifacts(value: Any, archive_stamp: Any) -> bool:
    if (type(archive_stamp) is not int or archive_stamp <= 0
            or not isinstance(value, list)
            or len(value) != len(RESET_ARTIFACT_NAMES)):
        return False
    for row, source in zip(value, RESET_ARTIFACT_NAMES):
        if (not isinstance(row, dict)
                or set(row) != {"source", "archive", "existed"}
                or type(row.get("existed")) is not bool
                or row.get("source") != source
                or row.get("archive") != f"{source}.reset-{archive_stamp}"):
            return False
    return True


def _validate_receipt(value: Any, *, path: Path) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or set(value) != _RECEIPT_KEYS
            or value.get("version") != RESET_RECEIPT_VERSION
            or not isinstance(value.get("id"), str)
            or RUN_RESET_OPERATION_RE.fullmatch(value["id"]) is None
            or not isinstance(value.get("run_id"), str) or not value["run_id"]
            or Path(value["run_id"]).name != value["run_id"]
            or value["run_id"] in {".", ".."}
            or not isinstance(value.get("run_key"), str)
            or _SHA256_RE.fullmatch(value["run_key"]) is None
            or not isinstance(value.get("expected_generation"), str)
            or _SHA256_RE.fullmatch(value["expected_generation"]) is None
            or value.get("status") not in RESET_STATUSES
            or value.get("phase") not in RESET_PHASES
            or _STATUS_FOR_PHASE.get(value.get("phase")) != value.get("status")
            or not _valid_artifacts(value.get("artifacts"), value.get("archive_stamp"))
            or not isinstance(value.get("task_stage"), str)
            or Path(value["task_stage"]).name != value["task_stage"]
            or not value["task_stage"].startswith(f".looplab-reset-task-{value['id']}.")
            or not isinstance(value.get("task_digest"), str)
            or _SHA256_RE.fullmatch(value["task_digest"]) is None
            or not isinstance(value.get("effective_config"), dict)
            or isinstance(value.get("created_at"), bool)
            or not isinstance(value.get("created_at"), (int, float))
            or not math.isfinite(value["created_at"])
            or isinstance(value.get("updated_at"), bool)
            or not isinstance(value.get("updated_at"), (int, float))
            or not math.isfinite(value["updated_at"])
            or value["updated_at"] < value["created_at"]):
        raise ResetReceiptError("reset receipt is malformed")
    name_match = _RECEIPT_NAME_RE.fullmatch(path.name)
    if (name_match is None
            or name_match.group("operation") != value["id"]
            or name_match.group("sequence") != value["run_key"]):
        raise ResetReceiptError("reset receipt path and operation identity disagree")
    new_generation = value.get("new_generation")
    if value["phase"] == "succeeded":
        if (not isinstance(new_generation, str)
                or _SHA256_RE.fullmatch(new_generation) is None
                or new_generation == value["expected_generation"]):
            raise ResetReceiptError("reset success has no distinct replacement generation")
    elif new_generation is not None:
        raise ResetReceiptError("incomplete reset receipt cannot name a replacement generation")
    return value


def _check_transition(current: dict[str, Any], value: dict[str, Any]) -> None:
    """Reset's phase lattice is an UNORDERED adjacency table, not a monotonic index.

    A launch that turns out uncertain legitimately falls back to `archived` and retries, so the
    permitted moves are not "forward by one" — which is exactly what deletion's rule is. The two
    lattices stay separate functions for that reason; see `durable_op.ReceiptProtocol`.
    """
    if value["phase"] not in _PHASE_TRANSITIONS[current["phase"]]:
        raise ResetReceiptError(
            f"invalid reset receipt transition {current['phase']} -> {value['phase']}")


_PROTOCOL = ReceiptProtocol(
    label="reset receipt",
    error_cls=ResetReceiptError,
    max_bytes=RESET_RECEIPT_MAX_BYTES,
    validate=_validate_receipt,
    immutable=_IMMUTABLE_RECEIPT_FIELDS,
    check_transition=_check_transition,
)


def load_reset_receipt(path: Path) -> Optional[dict[str, Any]]:
    return load_receipt(path, _PROTOCOL)


def save_reset_receipt(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    return save_receipt(path, value, _PROTOCOL, load=load_reset_receipt)


def reset_receipts_for_run(srv, rd: Path) -> list[tuple[Path, dict[str, Any]]]:
    # The run key is the command sequencer's file STEM, and the receipt root is its grandparent —
    # `reset_receipt_path` is what proves that directory is the canonical lock namespace, and this
    # scan reaches the same place from the same authority rather than from `srv.root`. Deletion
    # derives its key the other way round (`run_deletion_key(rd)`, cross-checked against the stem),
    # which is why `durable_op.receipts_for_run` takes an already-derived root and pattern.
    sequence_path = srv.commands._sequence_path(rd)
    root = sequence_path.parent.parent
    pattern = f"{RESET_RECEIPT_PREFIX}{sequence_path.stem}.*.json"
    return receipts_for_run(root, pattern, load_reset_receipt)


def receipt_result(receipt: dict[str, Any]) -> dict[str, Any]:
    result = {
        "ok": receipt.get("status") == "succeeded",
        "status": receipt.get("status"),
        "phase": receipt.get("phase"),
        "operation_id": receipt.get("id"),
        "expected_generation": receipt.get("expected_generation"),
    }
    if receipt.get("new_generation"):
        result["generation"] = receipt["new_generation"]
    return result


def complete_reset_if_observed(
        srv, rd: Path, receipt_path: Path, receipt: dict[str, Any]
        ) -> tuple[dict[str, Any], bool]:
    """Commit success only after the matching fenced writer produced a replacement generation.

    Caller holds the run command sequencer. The marker is the correlation proof: EventStore refuses
    every non-matching writer while it exists, so a new first event could only be written by the child
    carrying this receipt's exact operation id.
    """
    if receipt.get("status") == "succeeded":
        marker = load_run_reset_marker(rd)
        if marker is not None and marker.get("operation_id") == receipt["id"]:
            validate_reset_binding(srv, rd, marker, receipt_path, receipt)
            clear_run_reset_marker(rd, receipt["id"])
        return receipt, True
    if receipt.get("phase") not in {
            "popen_pending", "popen_returned", "launch_uncertain"}:
        return receipt, False
    marker = load_run_reset_marker(rd)
    if marker is None or marker.get("operation_id") != receipt["id"]:
        return receipt, False
    validate_reset_binding(srv, rd, marker, receipt_path, receipt)
    current = srv.commands.run_generation_if_present(rd)
    if not current or current == receipt["expected_generation"]:
        return receipt, False
    completed = {
        **receipt,
        "status": "succeeded",
        "phase": "succeeded",
        "new_generation": current,
        "updated_at": time.time(),
    }
    completed = save_reset_receipt(receipt_path, completed)
    clear_run_reset_marker(rd, receipt["id"])
    try:
        srv.commands.observe_external_spawn(rd, f"reset:{receipt['id']}")
    except Exception:  # noqa: BLE001 - terminal receipt is authority; claim cleanup can retry later
        pass
    return completed, True


def reconcile_run_reset_observation(srv, rd: Path) -> bool:
    """State/SSE cleanup for one exact marker; caller holds the command sequencer."""
    marker = load_run_reset_marker(rd)
    if marker is None:
        return False
    path = reset_receipt_path(srv, rd, marker["operation_id"])
    validate_reset_binding(srv, rd, marker, path)
    receipt = load_reset_receipt(path)
    if receipt is None:
        raise ResetReceiptError("reset marker has no matching durable receipt")
    validate_reset_binding(srv, rd, marker, path, receipt)
    _receipt, completed = complete_reset_if_observed(srv, rd, path, receipt)
    return completed


__all__ = [
    "RESET_ARTIFACT_NAMES", "RESET_RECEIPT_MAX_BYTES", "RESET_RECEIPT_PREFIX",
    "ResetReceiptError", "complete_reset_if_observed",
    "load_reset_receipt", "receipt_result", "reconcile_run_reset_observation",
    "reset_receipt_path", "reset_receipts_for_run", "save_reset_receipt",
    "validate_reset_binding",
]
