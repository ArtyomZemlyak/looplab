"""Durable roll-forward implementation of ``POST /api/runs/{id}/reset``."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import anyio
from fastapi import HTTPException, Request

from looplab.adapters.tasks import load_task
from looplab.core.atomicio import (
    _windows_move_write_through, strict_atomic_write_bytes, strict_fsync_parent)
from looplab.core.config import settings_from_snapshot
from looplab.core.run_reset import (
    RUN_RESET_OPERATION_ENV, RUN_RESET_OPERATION_RE, RunResetFenceError,
    RunResetStorageError, load_run_reset_marker, publish_run_reset_marker)
from looplab.events.eventstore import EventStoreLockError, _interprocess_lock
from looplab.events.span_index import (
    invalidate as invalidate_span_index, span_index_write_guard)
from looplab.serve.engine_proc import (
    _engine_alive, _engine_liveness, _fresh_resume_launch_pending,
    _fresh_run_launch_pending, _resolve_task_file, engine_write_lock_http,
    run_lifecycle_lock_http)
from looplab.serve.appstate import (
    _DELETE_FENCE_PREFIX, _DELETE_QUARANTINE_PREFIX, _DELETE_RECEIPT_PREFIX,
    _LIFECYCLE_LOCK_PREFIX, _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX,
    _TRACE_CLEAR_RECEIPT_PREFIX)
from looplab.serve.protocol import EXPECTED_RUN_GENERATION_FIELD
from looplab.serve.reset_transaction import (
    RESET_ARTIFACT_NAMES, ResetReceiptError, complete_reset_if_observed,
    load_reset_receipt, receipt_result, reset_receipt_path, reset_receipts_for_run,
    save_reset_receipt, validate_reset_binding)
from looplab.serve.run_files import run_config_write_lock
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
# Namespace for the DERIVED operation id a non-browser Replay gets (see `durable_reset_run`). A fixed
# constant, so the same (run, generation) always names the same operation across processes.
_RESET_OPERATION_NS = uuid.UUID("6f5b7f2a-1c4d-5e8a-9b3c-2d1e0f4a7c65")
_TASK_MAX_BYTES = 16 * 1024 * 1024
_CONFIG_MAX_BYTES = 8 * 1024 * 1024


def _durable_archive_move(source: Path, destination: Path) -> None:
    """Move one sibling artifact durably without ever replacing an archive name.

    Windows exposes the required no-replace + write-through contract through MoveFileExW. POSIX
    needs a native exclusive rename (plain ``rename`` may replace a raced destination), followed by
    an fsync of the containing directory so both the new archive name and source-name removal survive
    a crash before the receipt advances.
    """
    if source.parent != destination.parent:
        raise ValueError("Replay archives must remain in the run directory")
    if os.path.lexists(destination):
        raise FileExistsError(
            errno.EEXIST, "Replay archive destination already exists", str(destination))
    if os.name == "nt":
        _windows_move_write_through(source, destination, replace=False)
        return

    import ctypes
    import sys

    old_name = os.fsencode(os.path.abspath(source))
    new_name = os.fsencode(os.path.abspath(destination))
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        rename_exclusive = getattr(libc, "renameat2", None)
        if rename_exclusive is None:
            raise OSError(
                errno.ENOTSUP, "atomic no-replace rename is unavailable", str(destination))
        rename_exclusive.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(-100, old_name, -100, new_name, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rename_exclusive = getattr(libc, "renamex_np", None)
        if rename_exclusive is None:
            raise OSError(
                errno.ENOTSUP, "atomic no-replace rename is unavailable", str(destination))
        rename_exclusive.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(old_name, new_name, 0x00000004)  # RENAME_EXCL
    else:
        raise OSError(
            errno.ENOTSUP, "atomic no-replace rename is unavailable", str(destination))
    if result != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, "durable no-replace archive rename failed", str(destination))
    strict_fsync_parent(destination)


def _receipt_http_error(operation_id: str, exc: BaseException) -> HTTPException:
    return HTTPException(503, {
        "code": "reset_receipt_unavailable",
        "operation_id": operation_id,
        "message": "The durable Replay receipt is unavailable.",
        "remediation": "Inspect receipt storage; retry only this exact operation.",
    })


def _read_bounded_regular(path: Path, limit: int, *, code: str, label: str) -> bytes:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{label} is not a regular file")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except FileNotFoundError as exc:
        raise HTTPException(409, {
            "code": code,
            "message": f"Replay {label} is missing; Replay did not archive or restart the run.",
        }) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(503, {
            "code": code,
            "message": f"Replay {label} cannot be read safely; Replay did not archive or restart the run.",
        }) from exc
    if len(data) > limit:
        raise HTTPException(409, {
            "code": code,
            "message": f"Replay {label} exceeds its safety limit; Replay did not archive or restart the run.",
        })
    return data


def _pending(record: dict[str, Any], message: str) -> None:
    raise HTTPException(425, {
        **receipt_result(record),
        "code": "reset_pending",
        "message": message,
        "remediation": "Retry this exact operation; do not submit a new Replay.",
    })


def _identity(
        record: dict[str, Any], *, operation_id: str,
        run_id: str, expected_generation: str) -> None:
    if (record.get("id") != operation_id
            or record.get("run_id") != run_id
            or record.get("expected_generation") != expected_generation):
        raise HTTPException(409, {
            "code": "reset_operation_conflict",
            "operation_id": operation_id,
            "message": "This Replay operation id belongs to a different request.",
        })


def _save_receipt(
        path: Path, record: dict[str, Any], *, operation_id: str) -> dict[str, Any]:
    try:
        return save_reset_receipt(path, record)
    except ResetReceiptError as exc:
        raise HTTPException(503, {
            "code": "reset_outcome_unknown",
            "operation_id": operation_id,
            "message": "Replay progress could not receive a durable receipt.",
            "remediation": "Retry this exact operation; do not submit a new Replay.",
        }) from exc


def _frozen_launch(
        srv, rd: Path, record: dict[str, Any], *, operation_id: str
        ) -> tuple[list[str], dict[str, str]]:
    stage = rd / record["task_stage"]
    task_bytes = _read_bounded_regular(
        stage, _TASK_MAX_BYTES, code="reset_frozen_inputs_unavailable", label="task snapshot")
    if hashlib.sha256(task_bytes).hexdigest() != record["task_digest"]:
        _pending(record, "The task snapshot changed after Replay was committed.")
    try:
        load_task(stage)
        replay_settings = settings_from_snapshot(record["effective_config"])
    except Exception as exc:  # noqa: BLE001 - a committed operation remains fail-closed
        raise HTTPException(503, {
            "code": "reset_frozen_inputs_unavailable",
            "operation_id": operation_id,
            "message": "Replay's frozen task or configuration is no longer valid.",
            "remediation": "Inspect the durable receipt; do not create another Replay.",
        }) from exc
    effective = replay_settings.masked_snapshot()
    spawn_args = ["run", str(stage), "--out", str(rd)]
    # ``_spawn_engine`` starts from the ambient process environment and overlays only supplied
    # values. Pin every frozen nullable setting explicitly so an ambient LOOPLAB_* value cannot
    # resurrect a value whose generation-A snapshot deliberately recorded null. Secrets are the
    # exception by design: they are refreshed by SettingsStore and never persisted in the receipt.
    for key in sorted(_ALLOWED_FIELDS - _SECRET_FIELDS):
        if effective.get(key) is None:
            spawn_args.extend(["--set", f"{key}=null"])
    env = srv.settings.settings_env({
        key: value for key, value in effective.items()
        if key in _ALLOWED_FIELDS and key not in _SECRET_FIELDS and value is not None
    })
    env[RUN_RESET_OPERATION_ENV] = operation_id
    return spawn_args, env


def _validate_reset_quiescence(srv, rd: Path, expected_generation: str) -> None:
    """Reject every deterministic preflight failure before publishing reset ownership."""
    current_generation = srv.commands.run_generation(rd)
    if expected_generation != current_generation:
        raise HTTPException(409, {
            "code": "run_generation_changed",
            "expected_generation": expected_generation,
            "current_generation": current_generation or None,
            "message": "The run changed before Replay was committed.",
        })
    ownership = _engine_liveness(rd)
    if ownership is None:
        raise HTTPException(409, {
            "code": "engine_liveness_unknown",
            "message": "Engine ownership is unknown; Replay did not archive or restart the run.",
        })
    if (ownership is True or _engine_alive(rd) or _fresh_resume_launch_pending(rd)
            or _fresh_run_launch_pending(rd) or not srv.state(rd).finished):
        raise HTTPException(409, "run is active, launching, or no longer finished")


def _revalidate_reset_quiescence_locked(
        srv, rd: Path, expected_generation: str) -> None:
    """Repeat mutable quiescence checks after this request owns engine/config/event locks."""
    current_generation = srv.commands.run_generation(rd)
    if expected_generation != current_generation:
        raise HTTPException(409, {
            "code": "run_generation_changed",
            "expected_generation": expected_generation,
            "current_generation": current_generation or None,
            "message": "The run changed before Replay ownership was published.",
        })
    if (_fresh_resume_launch_pending(rd) or _fresh_run_launch_pending(rd)
            or not srv.state(rd).finished):
        raise HTTPException(409, "run changed or began launching during Replay preflight")


def _discard_unpublished_task_stage(task_stage: Path) -> None:
    try:
        task_stage.unlink(missing_ok=True)
        strict_fsync_parent(task_stage)
    except OSError as exc:
        raise HTTPException(503, {
            "code": "replay_task_cleanup_failed",
            "message": "Replay could not clean up its unpublished task snapshot safely.",
        }) from exc


def _prepare_receipt(
        srv, rd: Path, *, expected_generation: str,
        operation_id: str) -> dict[str, Any]:
    """Validate and freeze deterministic inputs without publishing operation authority."""

    task_file = _resolve_task_file(rd)
    if not task_file:
        raise HTTPException(400, "run has no reproducible task snapshot")
    task_source = Path(task_file)
    task_bytes = _read_bounded_regular(
        task_source, _TASK_MAX_BYTES, code="replay_task_invalid", label="task snapshot")

    snap = rd / "config.snapshot.json"
    config_bytes = _read_bounded_regular(
        snap, _CONFIG_MAX_BYTES, code="replay_config_unavailable", label="configuration snapshot")
    try:
        raw_config = json.loads(config_bytes.decode("utf-8"))
        if not isinstance(raw_config, dict):
            raise ValueError("config snapshot must be an object")
        effective_config = settings_from_snapshot(raw_config).masked_snapshot()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, {
            "code": "replay_config_invalid",
            "message": "Replay configuration is invalid; Replay did not archive or restart the run.",
        }) from exc

    stage_suffix = task_source.suffix or ".json"
    task_stage = rd / f".looplab-reset-task-{operation_id}{stage_suffix}"
    stamp = int(time.time() * 1000)
    while any(
            os.path.lexists(rd / f"{name}.reset-{stamp}")
            for name in RESET_ARTIFACT_NAMES):
        stamp += 1
    artifacts = [{
        "source": name,
        "archive": f"{name}.reset-{stamp}",
        "existed": os.path.lexists(rd / name),
    } for name in RESET_ARTIFACT_NAMES]
    now = time.time()
    record = {
        "version": 1,
        "id": operation_id,
        "run_id": rd.name,
        "run_key": srv.commands._sequence_path(rd).stem,
        "expected_generation": expected_generation,
        "status": "pending",
        "phase": "prepared",
        "archive_stamp": stamp,
        "artifacts": artifacts,
        "task_stage": task_stage.name,
        "task_digest": hashlib.sha256(task_bytes).hexdigest(),
        "effective_config": effective_config,
        "new_generation": None,
        "created_at": now,
        "updated_at": now,
    }

    # Stage last, after every in-memory receipt field has been derived. A validation error is then
    # the only unpublished failure that can leave this exact service-owned path behind.
    try:
        strict_atomic_write_bytes(task_stage, task_bytes)
        load_task(task_stage)
    except Exception as exc:  # noqa: BLE001
        _discard_unpublished_task_stage(task_stage)
        raise HTTPException(409, {
            "code": "replay_task_invalid",
            "message": "Replay task is invalid; Replay did not archive or restart the run.",
        }) from exc
    return record


def _flush_reset_cost_evidence(srv, rd: Path) -> None:
    """Drain durable cost evidence before taking the event-log lock it appends through."""
    flush_durable_costs = getattr(srv, "flush_durable_run_costs", None)
    if not callable(flush_durable_costs):
        raise HTTPException(503, "durable run-cost recovery is unavailable")
    try:
        durable_costs_flushed = flush_durable_costs(rd)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "durable run-cost recovery failed") from exc
    if durable_costs_flushed is not True:
        raise HTTPException(409, "run-cost evidence is pending or conflicting")

    outbox = rd / ".llm-usage-outbox"
    if os.path.lexists(outbox):
        try:
            info = outbox.lstat()
            is_junction = getattr(outbox, "is_junction", None)
            invalid = (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                       or (callable(is_junction) and is_junction()))
        except OSError as exc:
            raise HTTPException(409, "run-cost outbox cannot be inspected") from exc
        if invalid:
            raise HTTPException(409, "run-cost outbox is not a regular directory")


def _supersede_archive(
        receipt_path: Path, record: dict[str, Any], *, operation_id: str,
        code: str, message: str) -> None:
    record = _save_receipt(receipt_path, {
        **record,
        "status": "superseded",
        "phase": "superseded",
        "updated_at": time.time(),
    }, operation_id=operation_id)
    raise HTTPException(409, {
        **receipt_result(record),
        "code": code,
        "message": message,
    })


def _validate_archived_manifest(
        rd: Path, receipt_path: Path, record: dict[str, Any], *, operation_id: str
        ) -> None:
    """Confirm the exact receipt-owned archive state and its POSIX directory durability."""
    stamp = record.get("archive_stamp")
    artifacts = record.get("artifacts")
    canonical = bool(
        isinstance(stamp, int)
        and isinstance(artifacts, list)
        and len(artifacts) == len(RESET_ARTIFACT_NAMES)
        and all(
            row.get("source") == source
            and row.get("archive") == f"{source}.reset-{stamp}"
            for row, source in zip(artifacts, RESET_ARTIFACT_NAMES)
        )
    )
    if not canonical:
        _supersede_archive(
            receipt_path, record, operation_id=operation_id,
            code="reset_archive_manifest_invalid",
            message="Replay's archive manifest no longer names its exact artifact set.")

    for row in artifacts:
        source_exists = os.path.lexists(rd / row["source"])
        archive_exists = os.path.lexists(rd / row["archive"])
        state_matches = (
            (not source_exists and archive_exists)
            if row["existed"] else
            (not source_exists and not archive_exists)
        )
        if not state_matches:
            _supersede_archive(
                receipt_path, record, operation_id=operation_id,
                code="reset_archive_conflict",
                message="Replay's durable archive state diverged from its exact manifest.")

    if os.name != "nt":
        try:
            # Every source/destination rename is within ``rd``. One final parent sync is the commit
            # barrier for the complete manifest, including moves that became visible before a crash.
            strict_fsync_parent(rd / artifacts[0]["archive"])
        except OSError as exc:
            _pending(record, f"Replay could not durably commit its archive manifest: {exc}")


def _archive_forward(
        rd: Path, receipt_path: Path, record: dict[str, Any], *, operation_id: str
        ) -> dict[str, Any]:
    if record["phase"] == "prepared":
        record = _save_receipt(receipt_path, {
            **record, "phase": "archiving", "updated_at": time.time(),
        }, operation_id=operation_id)
    if record["phase"] == "archived":
        _validate_archived_manifest(
            rd, receipt_path, record, operation_id=operation_id)
        return record
    if record["phase"] != "archiving":
        return record
    for row in record["artifacts"]:
        source = rd / row["source"]
        archived = rd / row["archive"]
        source_exists = os.path.lexists(source)
        archive_exists = os.path.lexists(archived)
        if row["existed"]:
            if source_exists and not archive_exists:
                try:
                    _durable_archive_move(source, archived)
                except OSError as exc:
                    if os.path.lexists(source) and os.path.lexists(archived):
                        _supersede_archive(
                            receipt_path, record, operation_id=operation_id,
                            code="reset_archive_conflict",
                            message="Replay refused to overwrite an existing archive destination.")
                    _pending(record, f"Replay could not archive {row['source']}: {exc}")
            elif not source_exists and archive_exists:
                continue
            else:
                _supersede_archive(
                    receipt_path, record, operation_id=operation_id,
                    code="reset_archive_conflict",
                    message="Replay archive state is ambiguous.")
        elif source_exists or archive_exists:
            _supersede_archive(
                receipt_path, record, operation_id=operation_id,
                code="reset_archive_conflict",
                message="A previously absent Replay artifact appeared.")
    _validate_archived_manifest(
        rd, receipt_path, record, operation_id=operation_id)
    archived_record = _save_receipt(receipt_path, {
        **record, "phase": "archived", "updated_at": time.time(),
    }, operation_id=operation_id)
    # The phase is now authoritative to retries, so re-check its exact postcondition before this
    # invocation is allowed to cross Popen. A concurrent/manual divergence therefore fails closed.
    _validate_archived_manifest(
        rd, receipt_path, archived_record, operation_id=operation_id)
    return archived_record


def _reset_blocking(
        srv, rd: Path, *, run_id: str, expected_generation: str,
        operation_id: str, spawn_engine: Callable[..., Optional[int]]) -> dict[str, Any]:
    try:
        receipt_path = reset_receipt_path(srv, rd, operation_id)
        receipt = load_reset_receipt(receipt_path)
        marker_hint = load_run_reset_marker(rd)
    except ResetReceiptError as exc:
        raise _receipt_http_error(operation_id, exc) from exc
    except RunResetStorageError as exc:
        raise HTTPException(503, {
            "code": "reset_fence_unavailable",
            "operation_id": operation_id,
            "message": "Replay ownership cannot be inspected safely.",
        }) from exc
    if receipt is None and marker_hint is None and not (rd / "events.jsonl").exists():
        # A duplicate request can take this optimistic snapshot immediately before the first request
        # publishes authority and archives events.jsonl. Re-probe under the sequencer so that request
        # rejoins the exact durable operation instead of reporting a false 404 after mutation began.
        with srv.commands.sequence(rd):
            try:
                receipt = load_reset_receipt(receipt_path)
                marker_hint = load_run_reset_marker(rd)
            except ResetReceiptError as exc:
                raise _receipt_http_error(operation_id, exc) from exc
            except RunResetStorageError as exc:
                raise HTTPException(503, {
                    "code": "reset_fence_unavailable",
                    "operation_id": operation_id,
                    "message": "Replay ownership cannot be inspected safely.",
                }) from exc
            if receipt is None and marker_hint is None and not (rd / "events.jsonl").exists():
                raise HTTPException(404, "no such run")

    # This pre-sequence hook can close a retained paid-call activity whose cleanup itself needs the
    # sequencer. Any marker already fences EventStore, and marker-only recovery proves this flush
    # completed before ownership publication, so never retry it through its own writer fence.
    if receipt is None and marker_hint is None:
        flush_pending_cost = getattr(srv, "flush_pending_run_costs", None)
        if callable(flush_pending_cost):
            try:
                flushed = flush_pending_cost(rd)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(503, "pending run-cost recovery failed") from exc
            if flushed is False:
                raise HTTPException(409, "a paid call is awaiting durable accounting")

    spawn_args: Optional[list[str]] = None
    spawn_env: Optional[dict[str, str]] = None
    with srv.commands.sequence(rd):
        try:
            receipt = load_reset_receipt(receipt_path)
            marker = load_run_reset_marker(rd)
            if receipt is not None:
                _identity(
                    receipt, operation_id=operation_id,
                    run_id=rd.name, expected_generation=expected_generation)
            if marker is not None:
                if (marker.get("operation_id") != operation_id
                        or marker.get("expected_generation") != expected_generation
                        or marker.get("receipt_name") != receipt_path.name):
                    if receipt is None or receipt.get("status") != "succeeded":
                        raise HTTPException(409, {
                            "code": "reset_operation_conflict",
                            "operation_id": marker.get("operation_id"),
                            "message": "Another Replay operation owns this run.",
                        })
                else:
                    validate_reset_binding(
                        srv, rd, marker, receipt_path, receipt)
            for other_path, other in reset_receipts_for_run(srv, rd):
                if (other_path != receipt_path
                        and other.get("expected_generation") == expected_generation):
                    raise HTTPException(409, {
                        "code": "reset_operation_conflict",
                        "operation_id": other.get("id"),
                        "message": "A different Replay operation already owns this generation.",
                    })
        except ResetReceiptError as exc:
            raise _receipt_http_error(operation_id, exc) from exc
        except RunResetStorageError as exc:
            raise HTTPException(503, {
                "code": "reset_fence_unavailable",
                "operation_id": operation_id,
                "message": "Replay ownership cannot be inspected safely.",
            }) from exc

        # Receipts written by the pre-marker implementation, or a manually lost marker, already
        # represent durable nonterminal authority. Restore their exact writer/delete fence before
        # any quiescence/pending response; fresh operations still publish only after full preflight.
        if receipt is not None and receipt["status"] != "succeeded" and marker is None:
            snap = rd / "config.snapshot.json"
            try:
                with (run_config_write_lock(snap, operation_id=operation_id),
                      _interprocess_lock(
                          Path(str(rd / "events.jsonl") + ".lock"), required=True)):
                    marker = load_run_reset_marker(rd)
                    if marker is None:
                        marker = publish_run_reset_marker(
                            rd, operation_id=operation_id,
                            expected_generation=expected_generation,
                            receipt_name=receipt_path.name)
                    validate_reset_binding(
                        srv, rd, marker, receipt_path, receipt)
            except EventStoreLockError as exc:
                raise HTTPException(503, {
                    "code": "reset_writer_lock_unavailable",
                    "operation_id": operation_id,
                    "message": "Replay could not restore its event/config writer fence.",
                }) from exc
            except RunResetFenceError as exc:
                raise HTTPException(409, {
                    "code": "reset_operation_conflict",
                    "operation_id": exc.operation_id,
                    "message": "Another Replay operation owns this run.",
                }) from exc
            except (ResetReceiptError, RunResetStorageError) as exc:
                raise HTTPException(503, {
                    "code": "reset_fence_unavailable",
                    "operation_id": operation_id,
                    "message": "Replay's existing durable authority cannot be fenced exactly.",
                }) from exc

        # Same checks as destructive_guard, expressed under the already-owned sequencer so two
        # simultaneous copies of the SAME operation rejoin instead of the loser seeing a stale claim.
        if receipt is None or receipt["phase"] == "prepared":
            active = srv.commands._active_command_ids(rd)
            if active:
                raise HTTPException(
                    409, "cannot reset run: active command(s) " + ", ".join(active[:3]))
            if srv.commands._recent_spawn_claim(rd):
                raise HTTPException(409, "cannot reset run: an engine start is unresolved")
            if srv.commands._finalize_incomplete(rd):
                raise HTTPException(409, "cannot reset run: terminal projections are incomplete")
            canonical = srv.run_dir(run_id)
            if canonical != rd:
                raise HTTPException(409, "run path changed during Replay validation")

        with run_lifecycle_lock_http(rd):
            if receipt is None or receipt["phase"] == "prepared":
                _validate_reset_quiescence(srv, rd, expected_generation)
            launch_evidence: Optional[str] = "absent" if receipt is None else None
            retry_exact_launch = False
            if receipt is not None:
                try:
                    receipt, completed = complete_reset_if_observed(
                        srv, rd, receipt_path, receipt)
                except (ResetReceiptError, RunResetStorageError) as exc:
                    raise HTTPException(503, {
                        "code": "reset_outcome_unknown",
                        "operation_id": operation_id,
                        "message": "Replay completion evidence is unavailable.",
                    }) from exc
                if completed:
                    return receipt_result(receipt)
                if receipt["status"] == "superseded":
                    raise HTTPException(409, {
                        **receipt_result(receipt),
                        "code": "reset_operation_superseded",
                        "message": "Replay storage diverged after the operation was committed.",
                    })
                if receipt["phase"] in {
                        "popen_pending", "popen_returned", "launch_uncertain"}:
                    evidence = srv.commands.observe_external_spawn(
                        rd, f"reset:{operation_id}")
                    if evidence == "mismatched":
                        raise HTTPException(409, {
                            "code": "reset_spawn_conflict",
                            "operation_id": operation_id,
                            "message": "Another engine launch owns this run.",
                        })
                    ownership = _engine_liveness(rd)
                    current_generation = srv.commands.run_generation_if_present(rd)
                    if (evidence in {"absent", "dead_or_cleared"}
                            and ownership is False and not current_generation):
                        # The reset child writes the generation-defining setup_started event before
                        # Engine.run performs setup/provider/eval work. A definitively absent/dead
                        # exact owner plus an empty generation therefore remains before paid/eval
                        # effects; after acquiring engine.lock below, the exact launch is retryable.
                        retry_exact_launch = True
                        launch_evidence = evidence
                    else:
                        _pending(
                            receipt,
                            "Replay crossed the process-launch boundary; its replacement generation "
                            "is not yet durably visible.")
                else:
                    if receipt["phase"] == "archived":
                        cancel_preclaim = getattr(
                            srv.commands, "cancel_external_preclaim", None)
                        try:
                            if callable(cancel_preclaim):
                                cancel_preclaim(rd, f"reset:{operation_id}")
                        except OSError as exc:
                            raise HTTPException(503, {
                                "code": "reset_spawn_claim_unavailable",
                                "operation_id": operation_id,
                                "message": "Replay's exact pre-launch claim could not be retired.",
                            }) from exc
                    evidence = srv.commands.observe_external_spawn(
                        rd, f"reset:{operation_id}")
                    if evidence == "mismatched":
                        raise HTTPException(409, {
                            "code": "reset_spawn_conflict",
                            "operation_id": operation_id,
                            "message": "Another engine launch owns this run.",
                        })
                    if evidence not in {"absent", "dead_or_cleared"}:
                        _pending(receipt, "Replay has an unresolved exact launch claim.")
                    launch_evidence = evidence

            # The required event/config locks close direct-CLI and HTTP writer races. The reset marker
            # is published while both are owned, then every later writer checks it inside the same lock.
            with engine_write_lock_http(rd):
                if receipt is None and marker is None:
                    _flush_reset_cost_evidence(srv, rd)
                snap = rd / "config.snapshot.json"
                try:
                    with (run_config_write_lock(snap, operation_id=operation_id),
                          _interprocess_lock(
                              Path(str(rd / "events.jsonl") + ".lock"), required=True),
                          span_index_write_guard(rd / "spans.jsonl", required=True)):
                        locked_marker = load_run_reset_marker(rd)
                        if locked_marker is not None:
                            if (locked_marker.get("operation_id") != operation_id
                                    or locked_marker.get("expected_generation")
                                    != expected_generation
                                    or locked_marker.get("receipt_name") != receipt_path.name):
                                raise HTTPException(409, {
                                    "code": "reset_operation_conflict",
                                    "operation_id": locked_marker.get("operation_id"),
                                    "message": "Another Replay operation owns this run.",
                                })
                            validate_reset_binding(
                                srv, rd, locked_marker, receipt_path, receipt)

                        prepared_record: Optional[dict[str, Any]] = None
                        if receipt is None or receipt["phase"] == "prepared":
                            _revalidate_reset_quiescence_locked(
                                srv, rd, expected_generation)
                        if receipt is None:
                            # Marker-only recovery repeats this deterministic preparation. No archive
                            # move or Popen can have happened before the prepared receipt existed.
                            prepared_record = _prepare_receipt(
                                srv, rd, expected_generation=expected_generation,
                                operation_id=operation_id)

                        # Publication order is deliberate: validation/staging first, exact writer
                        # fence second, and only then the prepared receipt becomes durable authority.
                        # A crash in the final gap leaves marker-only state that this exact op rebuilds.
                        if locked_marker is None:
                            try:
                                locked_marker = publish_run_reset_marker(
                                    rd, operation_id=operation_id,
                                    expected_generation=expected_generation,
                                    receipt_name=receipt_path.name)
                            except RunResetStorageError as exc:
                                # If publication definitely left no authority, remove the exact staged
                                # task so a rejected attempt has no run-local residue. Ambiguous or
                                # durable marker state retains the frozen bytes for same-op recovery.
                                try:
                                    marker_after = load_run_reset_marker(rd)
                                    receipt_after = load_reset_receipt(receipt_path)
                                except (RunResetStorageError, ResetReceiptError):
                                    marker_after = receipt_after = object()
                                if (marker_after is None and receipt_after is None
                                        and prepared_record is not None):
                                    _discard_unpublished_task_stage(
                                        rd / prepared_record["task_stage"])
                                raise HTTPException(503, {
                                    "code": "reset_outcome_unknown",
                                    "operation_id": operation_id,
                                    "message": "Replay's durable writer fence is unavailable.",
                                }) from exc
                        validate_reset_binding(
                            srv, rd, locked_marker, receipt_path, receipt)
                        if retry_exact_launch:
                            # Acquiring engine.lock after the tri-state liveness probe closes the
                            # last race with a late exact child. Recheck the event identity while its
                            # writer lock is held before durably authorizing the same Popen again.
                            current_generation = srv.commands.run_generation_if_present(rd)
                            if current_generation:
                                try:
                                    receipt, completed = complete_reset_if_observed(
                                        srv, rd, receipt_path, receipt)
                                except (ResetReceiptError, RunResetStorageError) as exc:
                                    raise HTTPException(503, {
                                        "code": "reset_outcome_unknown",
                                        "operation_id": operation_id,
                                        "message": "Replay replacement evidence is unavailable.",
                                    }) from exc
                                if completed:
                                    return receipt_result(receipt)
                                _pending(
                                    receipt,
                                    "Replay acquired new generation evidence that is not safely "
                                    "correlated to this operation.")
                            receipt = _save_receipt(receipt_path, {
                                **receipt,
                                "status": "pending",
                                "phase": "archived",
                                "updated_at": time.time(),
                            }, operation_id=operation_id)
                        if receipt is None:
                            assert prepared_record is not None
                            receipt = _save_receipt(
                                receipt_path, prepared_record, operation_id=operation_id)
                            validate_reset_binding(
                                srv, rd, locked_marker, receipt_path, receipt)

                        spawn_args, spawn_env = _frozen_launch(
                            srv, rd, receipt, operation_id=operation_id)
                        receipt = _archive_forward(
                            rd, receipt_path, receipt, operation_id=operation_id)
                        if receipt["phase"] != "archived":
                            _pending(receipt, "Replay has not completed its archive phase.")
                        invalidate_span_index(rd / "spans.jsonl")
                        srv.invalidate_trace_view(rd)

                        evidence = launch_evidence
                        if evidence is None:
                            _pending(receipt, "Replay launch evidence is unavailable.")
                        if evidence == "mismatched":
                            raise HTTPException(409, {
                                "code": "reset_spawn_conflict",
                                "operation_id": operation_id,
                                "message": "Another engine launch owns this run.",
                            })
                        if evidence not in {"absent", "dead_or_cleared"}:
                            _pending(receipt, "Replay has an unresolved exact launch claim.")
                        srv.commands.begin_external_spawn(
                            rd, f"reset:{operation_id}")
                        receipt = _save_receipt(receipt_path, {
                            **receipt,
                            "status": "pending",
                            "phase": "popen_pending",
                            "updated_at": time.time(),
                        }, operation_id=operation_id)
                except EventStoreLockError as exc:
                    raise HTTPException(503, {
                        "code": "reset_writer_lock_unavailable",
                        "operation_id": operation_id,
                        "message": "Replay could not obtain all event/config writer locks.",
                    }) from exc
                except RunResetFenceError as exc:
                    raise HTTPException(409, {
                        "code": "reset_operation_conflict",
                        "operation_id": exc.operation_id,
                        "message": "Another Replay operation owns this run.",
                    }) from exc
                except RunResetStorageError as exc:
                    raise HTTPException(503, {
                        "code": "reset_fence_unavailable",
                        "operation_id": operation_id,
                        "message": "Replay's writer fence cannot be verified.",
                    }) from exc

            # Release engine.lock before Popen; command + lifecycle ownership still serialize the
            # exact preclaim -> process -> receipt boundary.
            assert spawn_args is not None and spawn_env is not None and receipt is not None
            try:
                pid = spawn_engine(spawn_args, env=spawn_env, run_dir=rd)
                srv.commands.record_external_spawn(rd, f"reset:{operation_id}", pid)
                receipt = _save_receipt(receipt_path, {
                    **receipt,
                    "status": "accepted",
                    "phase": "popen_returned",
                    "updated_at": time.time(),
                }, operation_id=operation_id)
            except BaseException as exc:
                uncertain = {
                    **receipt,
                    "status": "pending",
                    "phase": "launch_uncertain",
                    "updated_at": time.time(),
                }
                try:
                    receipt = _save_receipt(
                        receipt_path, uncertain, operation_id=operation_id)
                except HTTPException:
                    pass
                raise HTTPException(503, {
                    **receipt_result(receipt),
                    "code": "reset_launch_uncertain",
                    "message": "Replay crossed the process-launch boundary without a confirmed "
                               "replacement generation.",
                    "remediation": "Retry this exact operation; never submit a new Replay.",
                }) from exc

            deadline = time.monotonic() + min(
                3.0, max(0.1, float(getattr(srv.commands, "startup_timeout", 1.0))))
            while True:
                try:
                    receipt, completed = complete_reset_if_observed(
                        srv, rd, receipt_path, receipt)
                except (ResetReceiptError, RunResetStorageError) as exc:
                    raise HTTPException(503, {
                        "code": "reset_outcome_unknown",
                        "operation_id": operation_id,
                        "message": "Replay replacement evidence could not be committed.",
                    }) from exc
                if completed:
                    return receipt_result(receipt)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            _pending(
                receipt,
                "Replay launch was accepted, but its replacement generation is not yet visible.")


async def durable_reset_run(
        srv, run_id: str, request: Request, *,
        spawn_engine: Callable[..., Optional[int]]) -> dict[str, Any]:
    try:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "reset body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "reset body must be a JSON object")
    expected_generation = body.get(EXPECTED_RUN_GENERATION_FIELD)
    operation_id = body.get("operation_id")
    # A BROWSER Replay must carry its own generation fence: the tab may be acting on a snapshot the
    # run has already moved past, and 428 is what tells it to re-read and retry. A scripted caller —
    # the CLI, operator tooling, a test client — has no stale snapshot to guard against and has always
    # been allowed to omit the field; requiring it unconditionally 400'd every non-browser reset
    # before any of the route's real work. Origin is the same browser signal the pre-split handler
    # keyed on, and the 428/400/409 ladder below is the contract
    # `test_browser_replay_requires_and_validates_generation` pins.
    browser = bool(request.headers.get("origin"))
    if expected_generation is None and browser:
        raise HTTPException(428, "expected_generation is required for browser Replay")
    if expected_generation is not None and (
            not isinstance(expected_generation, str)
            or _GENERATION_RE.fullmatch(expected_generation) is None):
        raise HTTPException(400, "expected_generation must be a lowercase SHA-256 token")
    # The durable operation id gets the same split, and a supplied one is validated either way. It
    # exists so a caller whose request outcome became unknown can REJOIN that exact operation rather
    # than start a second one against the same generation — which `reset_receipts_for_run` below
    # rejects as `reset_operation_conflict`. A browser persists its id to get that; a scripted caller
    # has nowhere to persist one, so the server DERIVES it below from (run, generation) — a random id
    # per request would make an ordinary retry collide with its own predecessor's receipt.
    if operation_id is not None and (
            not isinstance(operation_id, str)
            or RUN_RESET_OPERATION_RE.fullmatch(operation_id) is None):
        raise HTTPException(400, "operation_id must be a lowercase UUID")
    if operation_id is None and browser:
        raise HTTPException(400, "operation_id must be a lowercase UUID")

    root = srv.root.resolve()
    requested = root / run_id
    try:
        entry = requested.lstat()
        rd = requested.resolve()
        is_junction = getattr(requested, "is_junction", None)
        junction = bool(callable(is_junction) and is_junction())
        attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(404, "no such run") from exc
    requested_identity = os.path.normcase(os.path.abspath(requested))
    resolved_identity = os.path.normcase(os.path.abspath(rd))
    if (Path(run_id).name != run_id or run_id in {".", ".."}
            or requested.name.lower() in _RESERVED_RUN_IDS
            or requested.name.lower().startswith((
                _LIFECYCLE_LOCK_PREFIX, _TRACE_CLEAR_RECEIPT_PREFIX,
                _RESET_RECEIPT_PREFIX, _DELETE_FENCE_PREFIX,
                _DELETE_RECEIPT_PREFIX, _DELETE_QUARANTINE_PREFIX))
            or requested.parent != root or rd.parent != root
            or requested_identity != resolved_identity
            or stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode)
            or bool(attributes & reparse_flag)
            or junction
            or not rd.is_dir()):
        raise HTTPException(404, "no such run")

    if expected_generation is None:
        # The non-browser opt-out, resolved ONCE here so everything downstream keeps working on a real
        # token: the fence, the receipt binding (which requires a 64-hex generation) and the locked
        # re-check are all unchanged. Skipping the fence entirely — what the old handler did — would
        # mean the CAS could no longer reject a generation change at all; resolving the CURRENT
        # generation instead narrows the window to "changed between this read and the locked
        # re-check", which is precisely what the fence is for.
        expected_generation = srv.commands.run_generation(rd)
        if not expected_generation:
            # No durable generation identity yet (an empty or torn first line). Accepting a mutation
            # here would re-open the very reset race the token exists to close — see
            # `run_commands.run_generation_token`.
            raise HTTPException(409, {
                "code": "run_generation_unknown",
                "message": "The run has no durable generation identity to fence Replay against.",
            })
    if operation_id is None:
        # DERIVED, not random: "replay THIS generation of THIS run" is one operation, so a retry
        # after an unknown outcome must land on the same receipt and rejoin — which is exactly what a
        # browser gets by persisting its id. A fresh random id per request would instead be a SECOND
        # operation against the same generation, and the ownership check below correctly rejects that
        # as `reset_operation_conflict`. The generation is part of the key, so the next Replay (of the
        # replacement generation) is a genuinely new operation with a new id.
        operation_id = str(uuid.uuid5(_RESET_OPERATION_NS, f"{run_id}\n{expected_generation}"))
    return await anyio.to_thread.run_sync(lambda: _reset_blocking(
        srv, rd, run_id=run_id, expected_generation=expected_generation,
        operation_id=operation_id, spawn_engine=spawn_engine))


__all__ = ["durable_reset_run"]
