"""The paid scope-report ACTION protocol: reconcile a durable claim, then abandon or disclose it.

Extracted from ``routers/reports.py`` (doc 25 SR-02), where it was five closures inside
``build_router`` plus the two action endpoints' bodies. SR-12 had already moved the ~1 400-line
STORE out of that file (``serve/scope_report_store.py``); what stayed behind was the layer ABOVE it
— the crash-recovery state machine that decides, from a receipt plus a lease marker plus a fence
plus two OS byte-range locks, whether a paid provider call is still live, already terminal, or
permanently unknowable. None of that is HTTP, and being closures made every branch of it reachable
only by building the whole ASGI app and driving requests.

Bodies are verbatim moves. The only edits are mechanical: ``srv`` threaded explicitly where it was
captured (``srv.reports_dir`` in place of the captured ``_reports_dir``, which ``AppState`` assigns
once in ``__init__`` and nothing ever reassigns), and the five helpers renamed to public spellings
because ``routers/reports.py`` calls four of them. ``HTTPException`` travels with the bodies rather
than being translated at the boundary — ``deletion_service.py`` and ``trace_clear.py`` already raise
it from ``serve/`` modules, and inventing a new exception type here would change which refusals are
terminal and which are retryable, which is the one thing a paid-action extraction must not do.

MONKEYPATCH SEAM: importing a store name BINDS BY VALUE, exactly as the star import in
``routers/reports.py`` does, so this module holds a THIRD copy of every name it imports below. A
test that injects a store failure must patch it wherever it is bound —
``tests/test_report.py::_patch_store`` sweeps ``_STORE_PATCH_MODULE_PATHS``, and
``tests/test_scope_actions_service.py`` fails if any module imports from the store without being
listed there. That is the failure mode doc 25 SR-12 paid for once: a patch left on one module keeps
passing while testing nothing. This module is now the ONLY reader of
``_read_scope_action_lease_marker`` outside the store, so it is not a hypothetical here.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from looplab.serve.scope_report_store import (
    _SCOPE_ACTION_ABANDONED,
    _SCOPE_ACTION_ACTIVE,
    _SCOPE_ACTION_CONFLICT,
    _SCOPE_ACTION_INDETERMINATE,
    _SCOPE_STORAGE_ERROR,
    _SCOPE_TYPES,
    _ScopeActionLease,
    _ScopeReportActionConflict,
    _ScopeReportStorageConflict,
    _acquire_scope_action_lease,
    _action_lease_marker,
    _ensure_lease_marker,
    _missing_scope_action_receipt,
    _read_scope_action_fence,
    _read_scope_action_lease_marker,
    _read_scope_action_receipt,
    _read_scope_lease_marker,
    _release_retained_scope_action_leases,
    _scope_action_failure,
    _scope_action_id,
    _scope_action_lease_is_live,
    _scope_action_lease_path,
    _scope_action_leases_are_retained,
    _scope_action_scope_lease_is_live,
    _scope_action_scope_lease_path,
    _scope_lease_marker,
    _scope_store_lock,
    _write_scope_action_fence,
    _write_scope_action_receipt,
)


def action_response(receipt: dict[str, Any]) -> dict[str, Any]:
    action_id = receipt["action_id"]
    if receipt["status"] == "running":
        return {"status": "running", "action_id": action_id,
                "job_id": receipt["job_id"]}
    return {**receipt["result"], "status": receipt["status"], "action_id": action_id}


def indeterminate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        **receipt,
        "status": "indeterminate",
        "updated_at": int(time.time() * 1000),
        "result": _scope_action_failure(
            _SCOPE_ACTION_INDETERMINATE, receipt["action_id"]),
    }


def _reconcile_running_action(
        srv, scope_type: str, scope_id: str,
        receipt: dict[str, Any]) -> dict[str, Any]:
    """Turn an orphaned/done-without-ledger running claim into a durable unknown terminal.

    The volatile result is deliberately ignored. A successful paid payload is authoritative only
    after the worker strictly published both the canonical report and the compact action terminal.
    """
    if receipt["status"] != "running":
        return receipt
    retained_leases = _scope_action_leases_are_retained(
        srv.reports_dir, receipt["action_id"])
    marker = _read_scope_action_lease_marker(
        srv.reports_dir, receipt["action_id"])
    expected_marker = _action_lease_marker(
        scope_type, scope_id, receipt["action_id"])
    if marker is None:
        fence = _read_scope_action_fence(srv.reports_dir, scope_type, scope_id)
        if (fence is None or fence["status"] != "active"
                or fence["action_id"] != receipt["action_id"]):
            raise _ScopeReportStorageConflict(
                "running scope report action marker disappeared without its exact fence")
        if _read_scope_lease_marker(
                srv.reports_dir, scope_type, scope_id) is None:
            raise _ScopeReportStorageConflict(
                "running scope report lease markers disappeared")
        if (not retained_leases
                and _scope_action_scope_lease_is_live(
                    srv.reports_dir, scope_type, scope_id)):
            # The scope lease is the independent proof that a worker may still hold an unlinked
            # action-lock inode. Never recreate that marker while paid work can be live.
            return receipt
        _ensure_lease_marker(
            _scope_action_lease_path(srv.reports_dir, receipt["action_id"]),
            expected_marker,
        )
    elif marker != expected_marker:
        raise _ScopeReportActionConflict(
            "running scope report action marker belongs to another scope")
    if _read_scope_lease_marker(srv.reports_dir, scope_type, scope_id) is None:
        if (not retained_leases
                and _scope_action_lease_is_live(
                    srv.reports_dir, receipt["action_id"])):
            return receipt
        fence = _read_scope_action_fence(srv.reports_dir, scope_type, scope_id)
        if (fence is None or fence["status"] != "active"
                or fence["action_id"] != receipt["action_id"]):
            raise _ScopeReportStorageConflict(
                "running scope report scope marker disappeared without its exact fence")
        _ensure_lease_marker(
            _scope_action_scope_lease_path(
                srv.reports_dir, scope_type, scope_id),
            _scope_lease_marker(scope_type, scope_id),
        )
    if retained_leases:
        # The provider already returned, but neither its terminal nor fallback tombstone could
        # be strictly confirmed. A later exact reconciliation may retry the conservative write;
        # only after it succeeds are both quarantining handles released.
        reconciled = _write_scope_action_receipt(
            srv.reports_dir, scope_type, scope_id, indeterminate_receipt(receipt))
        _release_retained_scope_action_leases(
            srv.reports_dir, receipt["action_id"])
        return reconciled
    # JobRegistry is process-local, but deployments may run multiple ASGI workers.
    # The OS lease is the cross-process liveness authority; a sibling must not orphan live paid
    # work merely because it cannot see this worker's in-memory job receipt.
    if _scope_action_lease_is_live(srv.reports_dir, receipt["action_id"]):
        return receipt
    # The existing marker was successfully locked, so no process owns the paid worker anymore.
    # Volatile JobRegistry state may itself be orphaned (for example a BaseException escaped its
    # worker); it cannot override the cross-process liveness authority.
    reconciled = _write_scope_action_receipt(
        srv.reports_dir, scope_type, scope_id, indeterminate_receipt(receipt))
    srv.jobs.discard_orphaned_running(receipt["job_id"])
    return reconciled


def read_reconciled_action(
        srv, scope_type: str, scope_id: str, action_id: str) -> dict[str, Any] | None:
    receipt = _read_scope_action_receipt(
        srv.reports_dir, scope_type, scope_id, action_id)
    if receipt is None:
        marker = _read_scope_action_lease_marker(srv.reports_dir, action_id)
        fence = _read_scope_action_fence(srv.reports_dir, scope_type, scope_id)
        retained_leases = _scope_action_leases_are_retained(
            srv.reports_dir, action_id)
        fence_binds_action = (
            fence is not None and fence["action_id"] == action_id)
        if marker is None:
            if not fence_binds_action:
                return None
            if fence["status"] == "clear":
                # A clear fence is permanent exact evidence that this UUID was already consumed
                # for this scope. Return a conservative tombstone so deletion cannot rebill it.
                return _missing_scope_action_receipt(scope_type, scope_id, action_id)
            scope_marker = _read_scope_lease_marker(
                srv.reports_dir, scope_type, scope_id)
            if scope_marker is None:
                raise _ScopeReportStorageConflict(
                    "active scope report lease markers disappeared")
            if (not retained_leases
                    and _scope_action_scope_lease_is_live(
                        srv.reports_dir, scope_type, scope_id)):
                # The still-live scope inode proves provider work may be active. Do not recreate
                # the deleted action-lock path and accidentally bypass its unlinked lock.
                return {
                    **_missing_scope_action_receipt(scope_type, scope_id, action_id),
                    "status": "running",
                    "result": None,
                }
            # Both paid-work leases are now proven dead and the active fence supplies the exact
            # scope binding. It is safe to recreate only the immutable marker, then persist an
            # indeterminate tombstone; explicit abandon remains required to clear the fence.
            _ensure_lease_marker(
                _scope_action_lease_path(srv.reports_dir, action_id),
                _action_lease_marker(scope_type, scope_id, action_id),
            )
            marker = _read_scope_action_lease_marker(srv.reports_dir, action_id)
        expected_marker = _action_lease_marker(scope_type, scope_id, action_id)
        if marker != expected_marker:
            raise _ScopeReportActionConflict(
                "scope report action marker belongs to another scope")
        if fence is not None and not fence_binds_action:
            # Never replace another UUID's scope authority. A dead scope-bound marker can still
            # own its own conservative tombstone, which lets the stale tab reconcile/discard A
            # without touching a live or clear fence for B.
            retained_leases = _scope_action_leases_are_retained(
                srv.reports_dir, action_id)
            if (not retained_leases
                    and _scope_action_lease_is_live(srv.reports_dir, action_id)):
                raise _ScopeReportStorageConflict(
                    "live scope report action conflicts with another action fence")
            receipt = _write_scope_action_receipt(
                srv.reports_dir, scope_type, scope_id,
                _missing_scope_action_receipt(scope_type, scope_id, action_id))
            if retained_leases:
                _release_retained_scope_action_leases(srv.reports_dir, action_id)
            return receipt
        if (not retained_leases
                and _scope_action_lease_is_live(srv.reports_dir, action_id)):
            # The provider may still be running even though external corruption removed its
            # receipt. Exact retries can safely rejoin by action UUID, but may not mint work.
            return {
                **_missing_scope_action_receipt(scope_type, scope_id, action_id),
                "status": "running",
                "result": None,
            }
        # The immutable marker supplies the missing scope binding. Rebuild a conservative durable
        # tombstone and active fence; explicit abandon is then the only operation that can reopen
        # this scope for a new paid UUID.
        receipt = _write_scope_action_receipt(
            srv.reports_dir, scope_type, scope_id,
            _missing_scope_action_receipt(scope_type, scope_id, action_id))
        if retained_leases:
            _release_retained_scope_action_leases(srv.reports_dir, action_id)
        if fence is None:
            scope_marker = _read_scope_lease_marker(
                srv.reports_dir, scope_type, scope_id)
            if scope_marker is None:
                _ensure_lease_marker(
                    _scope_action_scope_lease_path(
                        srv.reports_dir, scope_type, scope_id),
                    _scope_lease_marker(scope_type, scope_id),
                )
            elif _scope_action_scope_lease_is_live(
                    srv.reports_dir, scope_type, scope_id):
                raise _ScopeReportStorageConflict(
                    "scope report action fence disappeared while a worker is live")
            _write_scope_action_fence(
                srv.reports_dir, scope_type, scope_id, action_id, "active")
    if (receipt["status"] == "indeterminate"
            and _scope_action_leases_are_retained(srv.reports_dir, action_id)):
        # The fallback replace may itself have become visible before parent sync failed. Strictly
        # confirm that conservative tombstone before releasing the quarantining handles.
        receipt = _write_scope_action_receipt(
            srv.reports_dir, scope_type, scope_id, receipt)
        _release_retained_scope_action_leases(srv.reports_dir, action_id)
    if receipt["status"] in {"done", "abandoned"}:
        marker = _read_scope_action_lease_marker(srv.reports_dir, action_id)
        if _scope_action_leases_are_retained(srv.reports_dir, action_id):
            receipt = _write_scope_action_receipt(
                srv.reports_dir, scope_type, scope_id,
                indeterminate_receipt(receipt))
            _release_retained_scope_action_leases(
                srv.reports_dir, action_id)
        elif marker is not None:
            if marker != _action_lease_marker(scope_type, scope_id, action_id):
                raise _ScopeReportActionConflict(
                    "scope report action marker belongs to another scope")
            if _scope_action_lease_is_live(srv.reports_dir, action_id):
                # The worker writes terminal before releasing its leases. Treat a visible terminal
                # as running until the cross-process hand-off completes.
                return {**receipt, "status": "running", "result": None}
        else:
            fence = _read_scope_action_fence(
                srv.reports_dir, scope_type, scope_id)
            scope_marker = _read_scope_lease_marker(
                srv.reports_dir, scope_type, scope_id)
            if (fence is not None and fence["status"] == "active"
                    and fence["action_id"] == action_id
                    and scope_marker is not None
                    and _scope_action_scope_lease_is_live(
                        srv.reports_dir, scope_type, scope_id)):
                # A worker writes terminal before releasing both handles. If its action marker
                # was unlinked, the independent scope lease still quarantines that visible file.
                return {**receipt, "status": "running", "result": None}
        # Confirmation cannot be process-local: a strict replace may be visible even though its
        # parent sync failed. Re-publish every authority-granting terminal before returning it.
        try:
            receipt = _write_scope_action_receipt(
                srv.reports_dir, scope_type, scope_id, receipt)
        except _ScopeReportStorageConflict:
            return indeterminate_receipt(receipt)
    receipt = _reconcile_running_action(srv, scope_type, scope_id, receipt)
    if receipt["status"] in {"done", "abandoned", "indeterminate"}:
        # A strict read-back is itself sufficient durable confirmation. This also closes the
        # narrow window where publication succeeded but the worker was interrupted before it
        # could flip the process-local receipt's consumption policy.
        srv.jobs.mark_consumable(receipt["job_id"])
        srv.jobs.poll(receipt["job_id"])
    return receipt


def active_scope_action(
        srv, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    scope_marker = _read_scope_lease_marker(
        srv.reports_dir, scope_type, scope_id)
    fence = _read_scope_action_fence(srv.reports_dir, scope_type, scope_id)
    if scope_marker is None:
        if fence is None:
            return None
        if fence["status"] != "clear":
            raise _ScopeReportStorageConflict(
                "active scope report scope lease marker disappeared")
        action_marker = _read_scope_action_lease_marker(
            srv.reports_dir, fence["action_id"])
        if (action_marker != _action_lease_marker(
                scope_type, scope_id, fence["action_id"])
                or _scope_action_lease_is_live(
                    srv.reports_dir, fence["action_id"])):
            raise _ScopeReportStorageConflict(
                "scope report scope lease marker cannot be safely reconstructed")
        # A clear exact fence plus the dead immutable action marker proves no paid worker can
        # still own this scope. Strictly reconstruct the deterministic scope marker so one lost
        # metadata file does not brick all future actions.
        _ensure_lease_marker(
            _scope_action_scope_lease_path(
                srv.reports_dir, scope_type, scope_id),
            _scope_lease_marker(scope_type, scope_id),
        )
        scope_marker = _read_scope_lease_marker(
            srv.reports_dir, scope_type, scope_id)
        if scope_marker is None:
            raise _ScopeReportStorageConflict(
                "scope report scope lease marker reconstruction failed")
    scope_live = _scope_action_scope_lease_is_live(
        srv.reports_dir, scope_type, scope_id)
    if fence is None:
        # Once the permanent scope marker exists the permanent fence must exist too. Missing is
        # external corruption, including the unlink-while-live case; never fail open to paid work.
        raise _ScopeReportStorageConflict(
            "scope report action fence disappeared")
    if fence["status"] == "clear":
        if scope_live:
            raise _ScopeReportStorageConflict(
                "scope report action fence cleared while its worker is live")
        return None
    receipt = read_reconciled_action(
        srv, scope_type, scope_id, fence["action_id"])
    if receipt is None:
        raise _ScopeReportStorageConflict(
            "active scope report action receipt disappeared")
    if receipt["status"] in {"done", "abandoned"}:
        _write_scope_action_fence(
            srv.reports_dir, scope_type, scope_id,
            fence["action_id"], "clear")
        return None
    return receipt


def get_scope_action(
        srv, scope_type: str, scope_id: str, action_id: str) -> dict[str, Any]:
    """Read one paid scope-report action, reconciling its durable ledger first.

    The route docstring in ``routers/reports.py`` states the user-facing contract; this is the
    implementation.
    """
    if scope_type not in _SCOPE_TYPES:
        raise HTTPException(400, "bad scope type")
    normalized = _scope_action_id(action_id)
    if normalized is None:
        raise HTTPException(400, "bad scope report action id")
    try:
        with _scope_store_lock(srv.reports_dir):
            receipt = read_reconciled_action(srv, scope_type, scope_id, normalized)
    except _ScopeReportActionConflict as exc:
        raise HTTPException(409, _SCOPE_ACTION_CONFLICT) from exc
    except _ScopeReportStorageConflict as exc:
        raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
    if receipt is None:
        # unknown means no durable claim exists. The client may safely retry only the
        # same UUID; it must never mint a replacement action merely because a volatile job vanished.
        return {"status": "unknown", "action_id": normalized}
    return action_response(receipt)


def durable_abandon_scope_action(
        srv, scope_type: str, scope_id: str, action_id: str) -> dict[str, Any]:
    """Explicitly release an indeterminate paid-action fence without erasing its identity.

    The route docstring in ``routers/reports.py`` states the user-facing contract; this is the
    implementation. Abandon is intentionally never automatic: after a process crash the provider
    outcome cannot be proven. The old UUID remains a durable tombstone, and only an explicit new
    UUID may bill a new attempt. A process-local running worker always wins the race and makes
    abandon a conflict.
    """
    if scope_type not in _SCOPE_TYPES:
        raise HTTPException(400, "bad scope type")
    normalized = _scope_action_id(action_id)
    if normalized is None:
        raise HTTPException(400, "bad scope report action id")
    try:
        with _scope_store_lock(srv.reports_dir):
            fence = _read_scope_action_fence(srv.reports_dir, scope_type, scope_id)
            receipt = _read_scope_action_receipt(
                srv.reports_dir, scope_type, scope_id, normalized)
            fence_active_exact = (
                fence is not None and fence["status"] == "active"
                and fence["action_id"] == normalized)
            scope_worker_live = False
            if fence_active_exact:
                scope_marker = _read_scope_lease_marker(
                    srv.reports_dir, scope_type, scope_id)
                if scope_marker is None:
                    action_marker = _read_scope_action_lease_marker(
                        srv.reports_dir, normalized)
                    if action_marker != _action_lease_marker(
                            scope_type, scope_id, normalized):
                        raise _ScopeReportStorageConflict(
                            "active scope report lease markers disappeared")
                    if _scope_action_lease_is_live(srv.reports_dir, normalized):
                        raise HTTPException(409, {
                            **_SCOPE_ACTION_ACTIVE,
                            "action_id": normalized,
                        })
                    _ensure_lease_marker(
                        _scope_action_scope_lease_path(
                            srv.reports_dir, scope_type, scope_id),
                        _scope_lease_marker(scope_type, scope_id),
                    )
                scope_worker_live = _scope_action_scope_lease_is_live(
                    srv.reports_dir, scope_type, scope_id)
            if receipt is None:
                marker = _read_scope_action_lease_marker(
                    srv.reports_dir, normalized)
                expected_marker = _action_lease_marker(
                    scope_type, scope_id, normalized)
                fence_binds_action = (
                    fence is not None and fence["action_id"] == normalized)
                marker_lease: _ScopeActionLease | None = None
                repair_missing_fence = False
                retained_leases = _scope_action_leases_are_retained(
                    srv.reports_dir, normalized)
                if marker is not None:
                    if marker != expected_marker:
                        raise _ScopeReportActionConflict(
                            "scope report action marker belongs to another scope")
                    if (not retained_leases
                            and _scope_action_lease_is_live(
                                srv.reports_dir, normalized)):
                        raise HTTPException(409, {
                            **_SCOPE_ACTION_ACTIVE,
                            "action_id": normalized,
                        })
                    if (not retained_leases
                            and fence_active_exact and scope_worker_live):
                        raise HTTPException(409, {
                            **_SCOPE_ACTION_ACTIVE,
                            "action_id": normalized,
                        })
                    if fence is None:
                        scope_marker = _read_scope_lease_marker(
                            srv.reports_dir, scope_type, scope_id)
                        if (scope_marker is not None
                                and _scope_action_scope_lease_is_live(
                                    srv.reports_dir, scope_type, scope_id)):
                            raise HTTPException(409, {
                                **_SCOPE_ACTION_ACTIVE,
                                "action_id": normalized,
                            })
                        if scope_marker is None:
                            _ensure_lease_marker(
                                _scope_action_scope_lease_path(
                                    srv.reports_dir, scope_type, scope_id),
                                _scope_lease_marker(scope_type, scope_id),
                            )
                        repair_missing_fence = True
                else:
                    if not fence_binds_action:
                        # Server-unknown UUIDs own no durable action and require no tombstone.
                        # Rejecting the no-op also prevents arbitrary abandon calls from growing
                        # permanent root-level marker/receipt files without paid work.
                        return {"status": "unknown", "action_id": normalized}
                    if (not retained_leases and fence["status"] == "active"
                            and scope_worker_live):
                        raise HTTPException(409, {
                            **_SCOPE_ACTION_ACTIVE,
                            "action_id": normalized,
                        })
                    # The exact fence supplies the deleted marker's scope binding. With the scope
                    # lease proven dead (or already clear), strict recreation is safe.
                    if retained_leases:
                        _ensure_lease_marker(
                            _scope_action_lease_path(srv.reports_dir, normalized),
                            expected_marker,
                        )
                    else:
                        marker_lease = _acquire_scope_action_lease(
                            srv.reports_dir, scope_type, scope_id, normalized)
                        if marker_lease is None:
                            raise HTTPException(409, {
                                **_SCOPE_ACTION_ACTIVE,
                                "action_id": normalized,
                            })
                base = _missing_scope_action_receipt(
                    scope_type, scope_id, normalized)
                try:
                    receipt = _write_scope_action_receipt(
                        srv.reports_dir, scope_type, scope_id, {
                            **base,
                            "status": "abandoned",
                            "result": _scope_action_failure(
                                _SCOPE_ACTION_ABANDONED, normalized),
                        })
                finally:
                    if marker_lease is not None:
                        marker_lease.release()
                if ((fence_binds_action and fence["status"] == "active")
                        or repair_missing_fence):
                    _write_scope_action_fence(
                        srv.reports_dir, scope_type, scope_id, normalized, "clear")
                if retained_leases:
                    _release_retained_scope_action_leases(
                        srv.reports_dir, normalized)
                return action_response(receipt)
            receipt = read_reconciled_action(srv, scope_type, scope_id, normalized)
            assert receipt is not None
            repair_missing_fence = False
            if fence is None:
                scope_marker = _read_scope_lease_marker(
                    srv.reports_dir, scope_type, scope_id)
                if (scope_marker is not None
                        and _scope_action_scope_lease_is_live(
                            srv.reports_dir, scope_type, scope_id)):
                    raise HTTPException(409, {
                        **_SCOPE_ACTION_ACTIVE,
                        "action_id": normalized,
                    })
                if scope_marker is None:
                    _ensure_lease_marker(
                        _scope_action_scope_lease_path(
                            srv.reports_dir, scope_type, scope_id),
                        _scope_lease_marker(scope_type, scope_id),
                    )
                repair_missing_fence = True
            if receipt["status"] in {"done", "abandoned"}:
                if (repair_missing_fence
                        or (fence is not None and fence["status"] == "active"
                            and fence["action_id"] == normalized)):
                    _write_scope_action_fence(
                        srv.reports_dir, scope_type, scope_id, normalized, "clear")
                return action_response(receipt)
            if receipt["status"] == "running":
                raise HTTPException(409, {
                    **_SCOPE_ACTION_ACTIVE,
                    "action_id": normalized,
                })
            abandoned = {
                **receipt,
                "status": "abandoned",
                "updated_at": int(time.time() * 1000),
                "result": _scope_action_failure(
                    _SCOPE_ACTION_ABANDONED, normalized),
            }
            receipt = _write_scope_action_receipt(
                srv.reports_dir, scope_type, scope_id, abandoned)
            if (repair_missing_fence
                    or (fence is not None and fence["status"] == "active"
                        and fence["action_id"] == normalized)):
                _write_scope_action_fence(
                    srv.reports_dir, scope_type, scope_id, normalized, "clear")
            # A local done receipt is now backed by a durable tombstone and may be retired. Its
            # result remains deliberately unread: only the strict ledger controls reconciliation.
            srv.jobs.mark_consumable(receipt["job_id"])
            srv.jobs.poll(receipt["job_id"])
    except HTTPException:
        raise
    except _ScopeReportActionConflict as exc:
        raise HTTPException(409, _SCOPE_ACTION_CONFLICT) from exc
    except _ScopeReportStorageConflict as exc:
        raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
    return action_response(receipt)
