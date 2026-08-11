"""One durable whole-run deletion transaction shared by HTTP and Assistant control."""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from looplab.core.atomicio import durable_no_replace_rename, strict_fsync_parent
from looplab.core.pathsafe import is_reparse, WINDOWS_RESERVED
from looplab.core.run_deletion import (
    RUN_DELETION_OPERATION_RE, RunDeletionStorageError,
    assert_run_deletion_write_allowed, clear_run_deletion_fence,
    load_run_deletion_fence, publish_run_deletion_fence, run_deletion_key,
    run_deletion_snapshot_token)
from looplab.core.run_reset import RunResetStorageError, load_run_reset_marker
from looplab.events.eventstore import (
    EventStoreLockError, InterprocessLockContended, _interprocess_lock)
from looplab.events.span_index import (
    invalidate as invalidate_span_index, span_destructive_write_guard)
from looplab.serve.appstate import (
    _LIFECYCLE_LOCK_PREFIX, _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX,
    _TRACE_CLEAR_RECEIPT_PREFIX)
from looplab.serve.deletion_transaction import (
    DELETE_QUARANTINE_PREFIX, DELETE_RECEIPT_PREFIX, DeletionReceiptError,
    advance_deletion_receipt, deletion_quarantine_path, deletion_receipt_path,
    deletion_receipts_for_run, deletion_result, load_deletion_receipt,
    mark_deletion_quarantine_ambiguous, prepare_deletion_receipt, save_deletion_receipt,
    supersede_prepared_deletion_receipt)
from looplab.serve.durable_op import refuse_unless_quiescent
from looplab.serve.engine_proc import (
    _engine_alive, _engine_liveness, _fresh_resume_launch_pending,
    _fresh_run_launch_pending, engine_write_lock_http, run_lifecycle_lock_http)
from looplab.serve.projects import ProjectStoreLockError
from looplab.serve.run_commands import run_generation_token
from looplab.serve.run_files import run_config_write_lock


_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_PREFIXES = (
    _LIFECYCLE_LOCK_PREFIX, _TRACE_CLEAR_RECEIPT_PREFIX, _RESET_RECEIPT_PREFIX,
    DELETE_RECEIPT_PREFIX, DELETE_QUARANTINE_PREFIX, ".looplab-delete-fence-",
)


def _detail(code: str, message: str, *, operation_id: str | None = None,
            remediation: str = "", retryable: bool = False, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if operation_id:
        value["operation_id"] = operation_id
    if remediation:
        value["remediation"] = remediation
    value.update(extra)
    return value


def _plain_run_path(srv, run_id: str) -> Path:
    if (not isinstance(run_id, str) or not run_id or len(run_id) > 255
            or run_id != run_id.strip() or run_id.endswith((".", " "))
            or ":" in run_id or any(ord(ch) < 32 or ord(ch) == 127 for ch in run_id)
            or run_id.split(".", 1)[0].upper() in WINDOWS_RESERVED
            or Path(run_id).name != run_id or run_id in {".", ".."}
            or "/" in run_id or "\\" in run_id):
        raise HTTPException(404, _detail(
            "run_not_found", "No run exists with that exact direct-child identity."))
    root = srv.root.resolve()
    requested = root / run_id
    if (requested.name.lower() in _RESERVED_RUN_IDS
            or requested.name.lower().startswith(_SERVICE_PREFIXES)):
        raise HTTPException(404, _detail(
            "run_not_found", "No run exists with that exact direct-child identity."))
    if requested.parent != root:
        raise HTTPException(404, _detail("run_not_found", "No such run."))
    return requested


def _strict_existing_run(srv, run_id: str) -> Path:
    requested = _plain_run_path(srv, run_id)
    try:
        entry = requested.lstat()
        canonical = requested.resolve(strict=True)
        is_junction = getattr(requested, "is_junction", None)
        junction = bool(callable(is_junction) and is_junction())
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(404, _detail("run_not_found", "No such run.")) from exc
    if (is_reparse(entry) or junction or not stat.S_ISDIR(entry.st_mode)
            or canonical != requested.resolve(strict=False)
            or canonical.parent != srv.root.resolve()):
        raise HTTPException(404, _detail(
            "run_not_found", "The requested run path is not a canonical direct directory."))
    events = canonical / "events.jsonl"
    try:
        event_entry = events.lstat()
        event_canonical = events.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(404, _detail("run_not_found", "No such run.")) from exc
    if (is_reparse(event_entry) or not stat.S_ISREG(event_entry.st_mode)
            or event_canonical.parent != canonical or event_canonical != events):
        raise HTTPException(404, _detail(
            "run_not_found", "The run event source is not a canonical regular file."))
    return canonical


def _strict_quarantine(path: Path) -> bool:
    try:
        info = path.lstat()
        junction_fn = getattr(path, "is_junction", None)
        junction = bool(callable(junction_fn) and junction_fn())
        canonical = path.resolve(strict=True)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeletionReceiptError(f"deletion quarantine cannot be inspected: {exc}") from exc
    if (is_reparse(info) or junction or not stat.S_ISDIR(info.st_mode)
            or canonical != path.resolve(strict=False)):
        raise DeletionReceiptError("deletion quarantine is not a canonical service directory")
    return True


def _durable_no_replace_move(source: Path, destination: Path) -> None:
    """Atomically move sibling directories without replacing a raced destination.

    The rename contract lives in `core/atomicio.py`; the quarantine-is-a-sibling rule is this
    service's own, and its message names the run rather than the generic source.
    """
    if source.parent != destination.parent:
        raise ValueError("deletion quarantine must be a sibling of the run")
    durable_no_replace_rename(source, destination, label="deletion quarantine")

def _same_receipt_identity(
        receipt: dict[str, Any], *, run_id: str, operation_id: str,
        expected_generation: str, expected_seq: int, rd: Path) -> bool:
    return bool(
        receipt.get("id") == operation_id
        and receipt.get("run_id") == run_id
        and receipt.get("run_key") == run_deletion_key(rd)
        and receipt.get("expected_generation") == expected_generation
        and receipt.get("expected_seq") == expected_seq)


def _validate_fence_binding(
        fence: dict[str, Any], receipt: dict[str, Any], receipt_path: Path) -> None:
    if (fence.get("operation_id") != receipt.get("id")
            or fence.get("run_key") != receipt.get("run_key")
            or fence.get("expected_generation") != receipt.get("expected_generation")
            or fence.get("expected_seq") != receipt.get("expected_seq")
            or fence.get("receipt_name") != receipt_path.name):
        raise DeletionReceiptError("deletion fence and receipt do not bind the same run snapshot")


def _validate_fence_request(
        fence: dict[str, Any], *, operation_id: str, expected_generation: str,
        expected_seq: int, receipt_path: Path) -> None:
    """Validate a fence whose process crashed before its prepared receipt was published."""
    if (fence.get("operation_id") != operation_id
            or fence.get("expected_generation") != expected_generation
            or fence.get("expected_seq") != expected_seq
            or fence.get("receipt_name") != receipt_path.name):
        raise HTTPException(409, _detail(
            "delete_operation_conflict",
            "Another deletion operation owns this run snapshot.",
            operation_id=fence.get("operation_id")))


def _pending(receipt: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return deletion_result(
        receipt, code=code, message=message, retryable=True,
        remediation="Retry or check only this exact deletion operation.")


def _flush_pending_costs(srv, rd: Path) -> None:
    flush = getattr(srv, "flush_pending_run_costs", None)
    if not callable(flush):
        return
    try:
        result = flush(rd)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, _detail(
            "delete_cost_recovery_unavailable",
            "Pending run-cost recovery failed before deletion.", retryable=True)) from exc
    if result is False:
        raise HTTPException(409, _detail(
            "delete_cost_pending",
            "A paid call is still waiting for durable run-cost accounting.", retryable=True))


def _flush_durable_costs(srv, rd: Path) -> None:
    flush = getattr(srv, "flush_durable_run_costs", None)
    if not callable(flush):
        raise HTTPException(503, _detail(
            "delete_cost_recovery_unavailable",
            "Durable run-cost recovery is unavailable; the run was not deleted.", retryable=True))
    try:
        result = flush(rd)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, _detail(
            "delete_cost_recovery_unavailable",
            "Durable run-cost recovery failed; the run was not deleted.", retryable=True)) from exc
    if result is not True:
        raise HTTPException(409, _detail(
            "delete_cost_pending",
            "Run-cost evidence is pending, busy, malformed, or conflicting.", retryable=True))


def _retire_metadata(srv, rd: Path, receipt: dict[str, Any]) -> None:
    try:
        with srv.projects.transaction():
            srv.projects.forget_locked(receipt["run_id"])
    except ProjectStoreLockError as exc:
        raise DeletionReceiptError("project metadata lock is unavailable") from exc
    start = srv.commands.load_start_record(rd)
    if start is not None:
        start_id = start.get("id") if isinstance(start, dict) else None
        if not isinstance(start_id, str) or not start_id:
            raise DeletionReceiptError("run start record is malformed")
        if not srv.commands.retire_start_record(rd, start_id):
            raise DeletionReceiptError("run start record changed before retirement")
    invalidate = getattr(srv, "invalidate_run_caches", None)
    if callable(invalidate):
        invalidate(rd)
    else:
        srv.summary_cache.pop(receipt["run_id"], None)
        srv.invalidate_trace_view(rd)
        invalidate_span_index(rd / "spans.jsonl")


def _purge_quarantine(path: Path) -> bool:
    if not _strict_quarantine(path):
        return True
    engine_lock = path / "engine.lock"
    try:
        with _interprocess_lock(engine_lock, required=True, blocking=False):
            pass
    except InterprocessLockContended:
        return False
    except EventStoreLockError as exc:
        raise DeletionReceiptError("quarantine engine ownership cannot be verified") from exc
    shutil.rmtree(path)
    strict_fsync_parent(path)
    return not os.path.lexists(path)


def _purge_recreated_writer_shell(rd: Path) -> bool:
    """Remove only the empty/engine-lock shell a fenced direct CLI can race into existence.

    The direct CLI now checks the root fence both before materializing a run and after acquiring its
    engine lock. A process that crossed the first check just before publication can still recreate
    the original directory after quarantine. It cannot write events, but that harmless shell must
    not wedge the exact deletion forever. Never treat a directory with any other entry as debris.
    """
    try:
        info = rd.lstat()
        junction_fn = getattr(rd, "is_junction", None)
        junction = bool(callable(junction_fn) and junction_fn())
        if (is_reparse(info) or junction or not stat.S_ISDIR(info.st_mode)
                or rd.resolve(strict=True) != rd.resolve(strict=False)):
            return False
        children = list(rd.iterdir())
        if any(child.name != "engine.lock" for child in children):
            return False
        engine_lock = rd / "engine.lock"
        if engine_lock.exists():
            lock_info = engine_lock.lstat()
            if is_reparse(lock_info) or not stat.S_ISREG(lock_info.st_mode):
                return False
            try:
                with _interprocess_lock(engine_lock, required=True, blocking=False):
                    pass
            except InterprocessLockContended:
                return False
        shutil.rmtree(rd)
        strict_fsync_parent(rd)
        return not os.path.lexists(rd)
    except (FileNotFoundError, OSError, EventStoreLockError):
        return False


def _resume_after_quarantine(
        srv, rd: Path, receipt_path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    quarantine = deletion_quarantine_path(srv, rd, receipt["id"])
    if receipt["phase"] == "quarantined":
        try:
            _retire_metadata(srv, rd, receipt)
            receipt = advance_deletion_receipt(receipt_path, receipt, "metadata_committed")
        except (DeletionReceiptError, OSError) as exc:
            return _pending(
                receipt, "delete_metadata_pending",
                f"The run left the workspace, but metadata cleanup is not yet confirmed: {exc}")
    if receipt["phase"] == "metadata_committed":
        receipt = advance_deletion_receipt(receipt_path, receipt, "purging")
    if receipt["phase"] == "purging":
        try:
            if not _purge_quarantine(quarantine):
                return _pending(
                    receipt, "delete_purge_pending",
                    "The run left the workspace, but a previous writer still owns its quarantine.")
        except (DeletionReceiptError, OSError) as exc:
            return _pending(
                receipt, "delete_purge_pending",
                f"The run left the workspace, but quarantine cleanup is not yet confirmed: {exc}")
        receipt = advance_deletion_receipt(receipt_path, receipt, "retiring_fence")
    if receipt["phase"] == "retiring_fence":
        try:
            if os.path.lexists(quarantine):
                return _pending(
                    receipt, "delete_purge_pending",
                    "Deletion quarantine still exists; writer ownership must be rechecked.")
            if not clear_run_deletion_fence(rd, receipt["id"]):
                return _pending(
                    receipt, "delete_operation_conflict",
                    "Another deletion fence owns this run identity.")
            receipt = advance_deletion_receipt(receipt_path, receipt, "succeeded")
        except (RunDeletionStorageError, DeletionReceiptError, OSError) as exc:
            return _pending(
                receipt, "delete_fence_unavailable",
                f"Run files are gone, but the deletion fence could not be retired: {exc}")
    return deletion_result(receipt)


def begin_or_resume_run_deletion(
        srv, run_id: str, *, operation_id: str, expected_generation: str,
        expected_seq: int) -> dict[str, Any]:
    """Commit or rejoin one exact generation+tail deletion, rolling forward after every crash point."""
    rd = _plain_run_path(srv, run_id)
    try:
        receipt_path = deletion_receipt_path(srv, rd, operation_id)
        receipt = load_deletion_receipt(receipt_path)
        initial_fence = load_run_deletion_fence(rd)
    except (DeletionReceiptError, RunDeletionStorageError, OSError) as exc:
        raise HTTPException(503, _detail(
            "delete_receipt_unavailable", "Deletion recovery storage is unavailable.",
            operation_id=operation_id, retryable=True,
            remediation="Repair receipt storage and retry only this exact operation.")) from exc

    unfenced_prepared = (
        receipt is None or receipt.get("phase") == "prepared") and initial_fence is None
    if receipt is None or unfenced_prepared:
        rd = _strict_existing_run(srv, run_id)
        # Cost recovery can close an activity whose cleanup acquires the command sequencer. Do it
        # before taking that lock, but never run it through an already-published deletion fence. A
        # fence without a receipt is the exact crash gap recovered below; its original preflight had
        # already completed cost recovery before publishing the fence.
        if unfenced_prepared:
            _flush_pending_costs(srv, rd)
        elif receipt is None:
            _validate_fence_request(
                initial_fence, operation_id=operation_id,
                expected_generation=expected_generation, expected_seq=expected_seq,
                receipt_path=receipt_path)
    elif not _same_receipt_identity(
            receipt, run_id=run_id, operation_id=operation_id,
            expected_generation=expected_generation, expected_seq=expected_seq, rd=rd):
        raise HTTPException(409, _detail(
            "delete_operation_conflict",
            "This deletion identity is already bound to another run snapshot.",
            operation_id=operation_id))

    try:
        with srv.commands.sequence(rd):
            receipt = load_deletion_receipt(receipt_path)
            for other_path, other in deletion_receipts_for_run(srv, rd):
                if (other_path == receipt_path
                        or other.get("status") in {"succeeded", "superseded"}):
                    continue
                raise HTTPException(409, _detail(
                    "delete_operation_conflict",
                    "Another deletion operation already owns this run.",
                    operation_id=other.get("id"), current_phase=other.get("phase")))
            fence = load_run_deletion_fence(rd)
            if receipt is not None and not _same_receipt_identity(
                    receipt, run_id=run_id, operation_id=operation_id,
                    expected_generation=expected_generation, expected_seq=expected_seq, rd=rd):
                raise HTTPException(409, _detail(
                    "delete_operation_conflict",
                    "This deletion identity is bound to another run snapshot.",
                    operation_id=operation_id))
            if fence is not None:
                if fence.get("operation_id") != operation_id:
                    raise HTTPException(409, _detail(
                        "delete_operation_conflict",
                        "Another deletion operation owns this run.",
                        operation_id=fence.get("operation_id")))
                if receipt is None:
                    _validate_fence_request(
                        fence, operation_id=operation_id,
                        expected_generation=expected_generation, expected_seq=expected_seq,
                        receipt_path=receipt_path)
                else:
                    _validate_fence_binding(fence, receipt, receipt_path)
            if (receipt is not None and receipt["status"] == "pending"
                    and receipt["phase"] not in {"prepared", "retiring_fence"}
                    and fence is None):
                raise HTTPException(503, _detail(
                    "delete_fence_missing",
                    "Deletion recovery lost its root writer fence and will not touch run metadata.",
                    operation_id=operation_id, retryable=False,
                    remediation="Repair the exact deletion fence from its receipt before recovery."))
            if receipt is not None and receipt["status"] == "succeeded":
                if fence is not None:
                    if not clear_run_deletion_fence(rd, operation_id):
                        raise HTTPException(503, _detail(
                            "delete_fence_unavailable",
                            "A completed deletion's stale root fence could not be retired.",
                            operation_id=operation_id, retryable=True))
                return deletion_result(receipt)
            if receipt is not None and receipt["status"] == "superseded":
                raise HTTPException(409, _detail(
                    "delete_operation_superseded",
                    "This unfenced deletion intent was retired after its inspected run changed.",
                    operation_id=operation_id))
            if receipt is not None and receipt["phase"] == "quarantine_ambiguous":
                return _pending(
                    receipt, "delete_quarantine_outcome_unknown",
                    "Windows reported a failed durable move after the exact quarantine became "
                    "visible. The operation is fenced and requires storage repair before it can "
                    "continue.")

            needs_fresh_preflight = receipt is None or (
                receipt["phase"] == "prepared" and fence is None)
            if needs_fresh_preflight:
                rd = _strict_existing_run(srv, run_id)
                try:
                    reset = load_run_reset_marker(rd)
                except RunResetStorageError as exc:
                    raise HTTPException(503, _detail(
                        "delete_fence_unavailable",
                        "Replay ownership cannot be verified; the run was not deleted.",
                        operation_id=operation_id, retryable=True)) from exc
                if reset is not None:
                    raise HTTPException(409, _detail(
                        "run_reset_in_progress",
                        "Resolve the existing Replay operation before deleting this run.",
                        operation_id=reset.get("operation_id")))
                # The probe set and its order are `durable_op.refuse_unless_quiescent`'s (doc 25
                # SC-06); the `_detail` envelopes are deletion's own contract — each code, and
                # `engine_launching`'s `retryable`, is what the UI branches on.
                refuse_unless_quiescent(
                    srv.commands, rd,
                    active_command=lambda active: HTTPException(409, _detail(
                        "run_command_active", "The run has an active command.",
                        operation_id=operation_id, active_command_ids=active[:3])),
                    engine_start=lambda: HTTPException(409, _detail(
                        "engine_launching", "The run engine is still launching.",
                        operation_id=operation_id, retryable=True)),
                    finalize_incomplete=lambda: HTTPException(409, _detail(
                        "run_finalization_incomplete",
                        "Finish terminal projections before deleting this run.",
                        operation_id=operation_id)))

            with run_lifecycle_lock_http(rd):
                if receipt is None or receipt["phase"] == "prepared":
                    liveness = _engine_liveness(rd)
                    if liveness is None:
                        raise HTTPException(503, _detail(
                            "engine_liveness_unknown",
                            "Engine ownership cannot be verified; the run was not deleted.",
                            operation_id=operation_id, retryable=True))
                    if (liveness is True or _engine_alive(rd)
                            or _fresh_resume_launch_pending(rd)
                            or _fresh_run_launch_pending(rd)):
                        raise HTTPException(409, _detail(
                            "engine_running", "Pause or finish this run before deleting it.",
                            operation_id=operation_id, retryable=True))
                    with engine_write_lock_http(rd):
                        _flush_durable_costs(srv, rd)
                        snap = rd / "config.snapshot.json"
                        try:
                            with (run_config_write_lock(
                                      snap, deletion_operation_id=operation_id),
                                  _interprocess_lock(
                                      Path(str(rd / "events.jsonl") + ".lock"), required=True),
                                  span_destructive_write_guard(
                                      rd / "spans.jsonl", required=True)):
                                current_fence = load_run_deletion_fence(rd)
                                if current_fence is not None:
                                    if receipt is None:
                                        _validate_fence_request(
                                            current_fence, operation_id=operation_id,
                                            expected_generation=expected_generation,
                                            expected_seq=expected_seq,
                                            receipt_path=receipt_path)
                                    else:
                                        _validate_fence_binding(
                                            current_fence, receipt, receipt_path)
                                    assert_run_deletion_write_allowed(rd, operation_id)
                                events = srv.events(rd)
                                current_generation = run_deletion_snapshot_token(
                                    rd / "events.jsonl", run_generation_token(events))
                                current_seq = events[-1].seq if events else -1
                                unfenced_existing_prepared = (
                                    receipt is not None and receipt["phase"] == "prepared"
                                    and current_fence is None)
                                if current_generation != expected_generation:
                                    if unfenced_existing_prepared:
                                        receipt = supersede_prepared_deletion_receipt(
                                            receipt_path, receipt)
                                    raise HTTPException(409, _detail(
                                        "run_generation_changed",
                                        "The inspected run generation changed and was not deleted.",
                                        operation_id=operation_id,
                                        current_generation=current_generation))
                                if current_seq != expected_seq:
                                    if unfenced_existing_prepared:
                                        receipt = supersede_prepared_deletion_receipt(
                                            receipt_path, receipt)
                                    raise HTTPException(409, _detail(
                                        "run_seq_changed",
                                        "The run changed after confirmation and was not deleted.",
                                        operation_id=operation_id, current_seq=current_seq))
                                prepared = (prepare_deletion_receipt(
                                    srv, rd, operation_id=operation_id,
                                    expected_generation=expected_generation,
                                    expected_seq=expected_seq) if receipt is None else None)
                                # The root fence is the first durable authority. If the process dies
                                # before the prepared receipt lands, every writer remains blocked and
                                # the exact request can reconstruct that receipt from the fence. The
                                # reverse order leaves a crash gap in which reset/start can replace the
                                # generation while an orphan prepared receipt permanently owns it.
                                if current_fence is None:
                                    current_fence = publish_run_deletion_fence(
                                        rd, operation_id=operation_id,
                                        expected_generation=expected_generation,
                                        expected_seq=expected_seq,
                                        receipt_name=receipt_path.name)
                                if receipt is None:
                                    receipt = save_deletion_receipt(receipt_path, prepared)
                                _validate_fence_binding(
                                    current_fence, receipt, receipt_path)
                                if receipt["phase"] == "prepared":
                                    receipt = advance_deletion_receipt(
                                        receipt_path, receipt, "fenced")
                        except EventStoreLockError as exc:
                            raise HTTPException(503, _detail(
                                "delete_writer_lock_unavailable",
                                "Deletion could not obtain every writer lock.",
                                operation_id=operation_id, retryable=True)) from exc

                if receipt is None:
                    raise HTTPException(503, _detail(
                        "delete_receipt_unavailable",
                        "Deletion did not establish durable authority.",
                        operation_id=operation_id, retryable=True))
                if receipt["phase"] == "fenced":
                    receipt = advance_deletion_receipt(
                        receipt_path, receipt, "quarantining")
                if receipt["phase"] == "quarantining":
                    quarantine = deletion_quarantine_path(srv, rd, operation_id)
                    source_exists = os.path.lexists(rd)
                    quarantine_exists = os.path.lexists(quarantine)
                    if source_exists and not quarantine_exists:
                        try:
                            _durable_no_replace_move(rd, quarantine)
                        except OSError as exc:
                            source_exists = os.path.lexists(rd)
                            quarantine_exists = os.path.lexists(quarantine)
                            if os.name == "nt" and not source_exists and quarantine_exists:
                                receipt = mark_deletion_quarantine_ambiguous(
                                    receipt_path, receipt)
                                return _pending(
                                    receipt, "delete_quarantine_outcome_unknown",
                                    "Windows reported a failed durable move after the exact "
                                    "quarantine became visible. The operation remains fenced for "
                                    "manual storage recovery.")
                            return _pending(
                                receipt, "delete_quarantine_unavailable",
                                f"The run is fenced but could not enter deletion quarantine: {exc}")
                    elif source_exists and quarantine_exists:
                        if not _purge_recreated_writer_shell(rd):
                            return _pending(
                                receipt, "delete_quarantine_conflict",
                                "Both the run and its deletion quarantine exist; the source is not "
                                "a removable fenced writer shell.")
                        source_exists = False
                    elif not source_exists and not quarantine_exists:
                        return _pending(
                            receipt, "delete_quarantine_missing",
                            "Neither the fenced run nor its exact quarantine can be found.")
                    try:
                        if not _strict_quarantine(quarantine):
                            return _pending(
                                receipt, "delete_quarantine_missing",
                                "Deletion quarantine is not yet visible.")
                        # Observing the destination is not enough: a rename can be visible before
                        # its directory entry is durable. Exact recovery retries this confirmation
                        # before the receipt may advance beyond quarantining.
                        strict_fsync_parent(quarantine)
                    except DeletionReceiptError as exc:
                        return _pending(
                            receipt, "delete_quarantine_unavailable", str(exc))
                    except OSError as exc:
                        return _pending(
                            receipt, "delete_quarantine_unavailable",
                            f"Deletion quarantine durability is not yet confirmed: {exc}")
                    receipt = advance_deletion_receipt(
                        receipt_path, receipt, "quarantined")

                return _resume_after_quarantine(srv, rd, receipt_path, receipt)
    except HTTPException:
        raise
    except (DeletionReceiptError, RunDeletionStorageError, RunResetStorageError, OSError) as exc:
        raise HTTPException(503, _detail(
            "delete_outcome_unknown",
            "Deletion outcome storage is unavailable; retry only this exact operation.",
            operation_id=operation_id, retryable=True,
            remediation="Repair storage and retry/check this exact operation.")) from exc


def get_run_deletion(
        srv, run_id: str, operation_id: str) -> dict[str, Any]:
    rd = _plain_run_path(srv, run_id)
    if (not isinstance(operation_id, str)
            or RUN_DELETION_OPERATION_RE.fullmatch(operation_id) is None):
        raise HTTPException(404, _detail(
            "delete_operation_not_found", "No deletion receipt exists for that operation."))
    try:
        receipt = load_deletion_receipt(deletion_receipt_path(srv, rd, operation_id))
    except (DeletionReceiptError, RunDeletionStorageError, OSError) as exc:
        raise HTTPException(503, _detail(
            "delete_receipt_unavailable", "Deletion recovery storage is unavailable.",
            operation_id=operation_id, retryable=True)) from exc
    if receipt is None or receipt.get("run_id") != run_id:
        raise HTTPException(404, _detail(
            "delete_operation_not_found", "No deletion receipt exists for that exact operation.",
            operation_id=operation_id))
    return deletion_result(receipt)


def validate_deletion_request(body: Any) -> tuple[str, str, int]:
    if not isinstance(body, dict):
        raise HTTPException(400, _detail(
            "invalid_delete_request", "Deletion body must be a JSON object."))
    if set(body) != {"operation_id", "expected_generation", "expected_seq"}:
        raise HTTPException(400, _detail(
            "invalid_delete_request",
            "Deletion requires exactly operation_id, expected_generation, and expected_seq."))
    operation_id = body.get("operation_id")
    generation = body.get("expected_generation")
    expected_seq = body.get("expected_seq")
    if (not isinstance(operation_id, str)
            or RUN_DELETION_OPERATION_RE.fullmatch(operation_id) is None):
        raise HTTPException(400, _detail(
            "invalid_delete_operation_id", "operation_id must be a lowercase UUID."))
    if not isinstance(generation, str) or _GENERATION_RE.fullmatch(generation) is None:
        raise HTTPException(400, _detail(
            "invalid_delete_generation",
            "expected_generation must be a lowercase SHA-256 token."))
    if type(expected_seq) is not int or expected_seq < -1:
        raise HTTPException(400, _detail(
            "invalid_delete_seq", "expected_seq must be an integer greater than or equal to -1."))
    return operation_id, generation, expected_seq


__all__ = [
    "begin_or_resume_run_deletion", "get_run_deletion", "validate_deletion_request",
]
