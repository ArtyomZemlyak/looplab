"""Cross-run aggregate report routes. On-demand portfolio reports over a SET of runs (a project
folder, a task, or a super-task) — ONE generator, three scope axes. Persisted under
<run-root>/reports/ with a run-set fingerprint so the UI can flag staleness; an agent reads every
accepted run through a bounded/redacted brief and bounded drill projection, then synthesizes.
Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Header, HTTPException

from looplab.core.atomicio import (strict_atomic_write_text)
from looplab.core.comparison import (
    canonical_comparison_contract,
    comparison_measurement,
    finite_measurement,
)
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.replay import fold
from looplab.serve.scope_report import (
    DEFAULT_SCOPE_REPORT_TIME_S,
    DEFAULT_SCOPE_REPORT_TURNS,
    MAX_SCOPE_REPORT_RUNS,
)
from looplab.serve.scope_sources import (MAX_SCOPE_CONFIG_BYTES, MAX_SCOPE_TASK_BYTES, MAX_SCOPE_TOTAL_EVENT_BYTES, FrozenScopeSource, ScopeSourceCapacityError, ScopeSourceError, capture_scope_source, probe_scope_log_sig, scope_event_size)
from looplab.core.redact import redact_persisted_text

# The durable scope-report STORE moved to `serve/scope_report_store.py` (doc 25 SR-12):
# ~1 400 lines of path validation, receipts, leases, fences and record migration with no
# HTTP dependency, which `routers/genesis.py` could otherwise only reach by importing this
# router's privates. Star-imported so `reports.<name>` keeps resolving for `build_router`
# and for the tests that spell it that way — but see that module's docstring: a star import
# binds BY VALUE, so a monkeypatch seam belongs THERE, not here.
from looplab.serve.scope_report_store import *  # noqa: F401,F403


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _phase = srv.phase
    projects = srv.projects
    _reports_dir = srv.reports_dir
    revision_cache_lock = threading.Lock()
    revision_cache: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
    omission_cache: OrderedDict[tuple, float] = OrderedDict()
    def _scope_label_from_data(data: dict[str, Any], scope_type: str, scope_id: str) -> str:
        if scope_type == "project":
            p = next((x for x in data["projects"] if x["id"] == scope_id), None)
            return f"project “{p['name']}”" if p else f"project {scope_id}"
        if scope_type == "supertask":
            s = next((x for x in data["supertasks"] if x["id"] == scope_id), None)
            return f"super-task “{s['name']}”" if s else f"super-task {scope_id}"
        return f"task {scope_id}"

    def _scope_label(scope_type: str, scope_id: str) -> str:
        return _scope_label_from_data(projects.load(), scope_type, scope_id)

    def _scope_run_ids(scope_type: str, scope_id: str) -> list:
        """The runs a scope covers. project = the folder AND everything nested under it; task = same
        task_id; supertask = assigned to that super-task."""
        # The MEMBERSHIP projection, not the full /api/runs handler. That handler additionally runs
        # a per-run engine-liveness lock probe and the best-effort resume reconciler — which can
        # SPAWN an engine process — so every scope-report GET (the staleness check included) was
        # paying for live facts it never reads, and mutating the workspace to get them.
        summaries = srv.run_membership()   # a real method, not a build_router side effect
        if scope_type == "task":
            return [s["run_id"] for s in summaries if s.get("task_id") == scope_id]
        if scope_type == "supertask":
            return [s["run_id"] for s in summaries if s.get("supertask_id") == scope_id]
        if scope_type == "project":
            scopeset = {scope_id} | projects.descendants(scope_id)
            return [s["run_id"] for s in summaries if s.get("project_id") in scopeset]
        return []

    def _scope_context_digest(scope_type: str, scope_id: str, run_ids: list[str]) -> str:
        project_data = projects.load()
        labels = project_data.get("labels", {}) if isinstance(project_data, dict) else {}
        scoped_ids = sorted(set(run_ids))
        scope_metadata: dict[str, Any] = {}
        membership: dict[str, Any] = {}
        if scope_type == "project":
            # A project report's meaning includes the selected folder's ancestry and the exact
            # placement of its member runs, but not unrelated folders elsewhere in the workspace.
            # ``run_ids`` already binds the resulting membership set; assignments retain meaningful
            # moves between descendants even when that set happens to stay unchanged.
            rows = project_data.get("projects", []) if isinstance(project_data, dict) else []
            index = {
                row.get("id"): row for row in rows
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
            ancestry = []
            current = scope_id
            seen: set[str] = set()
            while current not in seen:
                seen.add(current)
                row = index.get(current)
                if row is None:
                    break
                ancestry.append({
                    "id": row.get("id"), "name": row.get("name"),
                    "parent_id": row.get("parent_id"),
                })
                parent = row.get("parent_id")
                if not isinstance(parent, str):
                    break
                current = parent
            scope_metadata["ancestry"] = list(reversed(ancestry))
            assignments = (
                project_data.get("assignments", {}) if isinstance(project_data, dict) else {})
            membership = {run_id: assignments.get(run_id) for run_id in scoped_ids}
        elif scope_type == "supertask":
            rows = project_data.get("supertasks", []) if isinstance(project_data, dict) else []
            selected = next((
                row for row in rows
                if isinstance(row, dict) and row.get("id") == scope_id
            ), None)
            if selected is not None:
                scope_metadata["supertask"] = {
                    "id": selected.get("id"), "name": selected.get("name"),
                    "task_id": selected.get("task_id"),
                }
            assignments = (
                project_data.get("supertask_assignments", {})
                if isinstance(project_data, dict) else {})
            membership = {run_id: assignments.get(run_id) for run_id in scoped_ids}
        context = {
            "scope": _scope_identity(scope_type, scope_id),
            "label": _scope_label_from_data(project_data, scope_type, scope_id),
            "scope_metadata": scope_metadata,
            "membership": membership,
            "run_labels": {rid: labels.get(rid) for rid in scoped_ids},
        }
        return hashlib.sha256(json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()

    def _run_brief(run_id: str, labels: dict, source: FrozenScopeSource) -> dict:
        events = source.events
        st = fold(events)
        finalize_incomplete = (
            incomplete_finalize_scope(events) is not None or st.finalization_pending())
        best = st.best()
        cfg = source.config_doc
        task_contract = None
        if source.task_doc is not None:
            task_contract = canonical_comparison_contract(
                source.task_doc.get("comparison_contract"))
        if task_contract is not None and task_contract["direction"] != st.direction:
            task_contract = None
        measurement = comparison_measurement(task_contract, best)
        # An explicit phase contract never falls back to the generic search metric.  Legacy runs
        # without a contract retain an unranked observation, while opted-in runs with missing/non-
        # finite phase evidence publish no measurement at all.
        best_metric = (measurement["value"] if measurement is not None else
                       finite_measurement(best.metric) if task_contract is None and best else None)
        return {"run_id": run_id, "label": labels.get(run_id), "task_id": st.task_id,
                "goal": st.goal, "direction": st.direction,
                "model": cfg.get("llm_model"), "policy": cfg.get("policy"),
                "best_metric": best_metric,
                "phase": _phase(st, finalize_incomplete=finalize_incomplete),
                "nodes": len(st.nodes),
                "report": st.report if isinstance(st.report, dict) else None,
                "comparison_contract": task_contract,
                # this single bounded receipt is the only cross-run numeric evidence.
                # Scope projection must copy it atomically; phase/source/uncertainty are inseparable.
                "comparison_measurement": measurement}

    def _scope_drill(frozen_runs: dict, run_id: str, node_id: int) -> str:
        """Project one frozen node without code, files, stdout/stderr, or raw tool output."""
        frozen = frozen_runs.get(run_id)
        if frozen is None:
            return "(drill unavailable)"
        try:
            if probe_scope_log_sig(srv.root, run_id) != frozen.revision["log_sig"]:
                return "(drill unavailable: frozen run changed)"
            st = fold(frozen.events)
            node = st.nodes.get(node_id)
            if node is None:
                return "(drill unavailable: no such node)"

            def _safe_text(value: object, cap: int) -> str:
                return redact_persisted_text(
                    value, max_chars=cap, entropy=True, single_line=True)

            idea = node.idea
            params = {
                _safe_text(key, 96): metric
                for key, metric in list((idea.params or {}).items())[:32]
                if _safe_text(key, 96) and finite_measurement(metric) is not None
            }
            trials = []
            for trial in list(node.trials or ())[:8]:
                trials.append({
                    "params": {
                        _safe_text(key, 96): metric
                        for key, metric in list((trial.params or {}).items())[:16]
                        if _safe_text(key, 96) and finite_measurement(metric) is not None
                    },
                    "metric": finite_measurement(trial.metric),
                    "seconds": finite_measurement(trial.seconds),
                })
            status = getattr(node.status, "value", node.status)
            projection = {
                "schema": 1,
                "run_id": _safe_text(run_id, 256),
                "node_id": node.id,
                "status": _safe_text(status, 64),
                "operator": _safe_text(node.operator, 128),
                "rationale": _safe_text(idea.rationale, 1_000),
                "params": params,
                "metric": finite_measurement(node.metric),
                "confirmed_mean": finite_measurement(node.confirmed_mean),
                "confirmed_std": finite_measurement(node.confirmed_std),
                "holdout_metric": finite_measurement(node.holdout_metric),
                "feasible": bool(node.feasible),
                "trials": trials,
                "trials_total": len(node.trials or ()),
                "trials_omitted": max(0, len(node.trials or ()) - len(trials)),
            }
            return json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:  # noqa: BLE001 - deep access is best-effort
            # This string becomes model input and may be echoed into the persisted/public report.
            # Run/tool exceptions can contain paths or provider metadata, so keep the diagnostic
            # deliberately generic. Detailed failures belong in server-side observability.
            return "(drill unavailable)"

    def _scope_sig(run_ids: list) -> list:
        """Reset-safe metadata fingerprint: generation, file identity, nanoseconds, and size."""
        sig: list = []
        for rid in sorted(set(run_ids)):
            try:
                sig.append(probe_scope_log_sig(srv.root, rid))
            except ScopeSourceError:
                sig.append([rid, "", 0, 0, 0, 0, 0])
        return sig

    def _scope_source_sizes(run_ids: list[str]) -> dict[str, int]:
        """Preflight every raw-file capacity bound before reserving background or provider work."""
        sizes: dict[str, int] = {}
        total = 0
        for run_id in sorted(set(run_ids)):
            # SEPARATE scopes: only an unreadable events.jsonl may yield size 0. One try around both
            # meant a present-but-unreadable SNAPSHOT (EACCES/EIO) raised ScopeSourceError after
            # `scope_event_size` had already produced the real byte count, clobbering it to 0 — which
            # both undercounted the MAX_SCOPE_TOTAL_EVENT_BYTES budget (a 30 MB log counting as 0)
            # and, if the snapshot error cleared between this preflight and `_compute`'s capture,
            # fired a spurious `scope_report_inputs_changed` on `event_bytes != expected_bytes(0)`.
            try:
                size = scope_event_size(srv.root, run_id)
            except ScopeSourceCapacityError:
                raise
            except ScopeSourceError:
                size = 0
            run_dir = Path(srv.root).absolute() / run_id
            for filename, limit in (
                ("task.snapshot.json", MAX_SCOPE_TASK_BYTES),
                ("config.snapshot.json", MAX_SCOPE_CONFIG_BYTES),
            ):
                try:
                    status = (run_dir / filename).lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    # An unreadable snapshot is a snapshot problem, not evidence that the run has no
                    # events. It is already re-checked (and fails closed) where it is actually READ.
                    continue
                if not stat.S_ISREG(status.st_mode) or _is_link_or_reparse(status):
                    continue
                if int(status.st_size) > limit:
                    raise ScopeSourceCapacityError(
                        f"{filename} exceeds its scope-report byte limit")
            total += size
            if total > MAX_SCOPE_TOTAL_EVENT_BYTES:
                raise ScopeSourceCapacityError("scope event evidence exceeds its byte limit")
            sizes[run_id] = size
        return sizes

    def _source_probe_key(run_id: str, log_sig: list) -> tuple:
        """Cheap identity for every file represented by a full source revision."""
        if (not _valid_scope_sig_row(log_sig) or len(log_sig) != 7
                or log_sig[0] != run_id or not run_id or run_id in {".", ".."}
                or "\x00" in run_id or "/" in run_id or "\\" in run_id or ":" in run_id
                or run_id.rstrip(" .") != run_id):
            raise ScopeSourceError("scope source identity is invalid")

        def observed(status: os.stat_result) -> tuple[int, ...]:
            ctime_ns = getattr(status, "st_ctime_ns", None)
            if ctime_ns is None:
                ctime_ns = int(status.st_ctime * 1_000_000_000)
            return (*_stat_identity(status), int(ctime_ns))

        def directory_identity(status: os.stat_result) -> tuple[int, ...]:
            # Child artifact creation changes directory timestamps but not report evidence. Bind the
            # container itself and let the three exact file observations own model-visible changes.
            return (
                int(status.st_dev), int(status.st_ino), int(status.st_mode),
                int(getattr(status, "st_file_attributes", 0) or 0),
            )

        def optional_file(path: Path) -> tuple:
            try:
                status = path.lstat()
            except FileNotFoundError:
                return ("missing",)
            if not stat.S_ISREG(status.st_mode) or _is_link_or_reparse(status):
                raise ScopeSourceError("scope snapshot is not a trusted regular file")
            return ("present", *observed(status))

        try:
            run_dir = Path(srv.root).absolute() / run_id
            run_status = run_dir.lstat()
            if not stat.S_ISDIR(run_status.st_mode) or _is_link_or_reparse(run_status):
                raise ScopeSourceError("scope run is not a trusted directory")
            return (
                tuple(log_sig), directory_identity(run_status),
                optional_file(run_dir / "events.jsonl"),
                optional_file(run_dir / "task.snapshot.json"),
                optional_file(run_dir / "config.snapshot.json"),
            )
        except ScopeSourceError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ScopeSourceError("scope source identity is unavailable") from exc

    def _source_probe_receipt(run_id: str, log_sig: list) -> tuple[str, tuple | None]:
        """Return a stable persisted digest even when the source itself is currently unprobeable."""
        try:
            key = _source_probe_key(run_id, log_sig)
            payload: object = ["observed", key]
        except ScopeSourceError:
            key = None
            payload = ["unavailable", run_id, log_sig]
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), key

    def _remember_revision(probe_key: tuple, revision: dict[str, Any]) -> None:
        with revision_cache_lock:
            omission_cache.pop(probe_key, None)
            revision_cache[probe_key] = (time.monotonic(), {
                **revision, "log_sig": list(revision["log_sig"]),
            })
            revision_cache.move_to_end(probe_key)
            while len(revision_cache) > _SCOPE_REVISION_CACHE_MAX:
                revision_cache.popitem(last=False)

    def _cached_revision(probe_key: tuple) -> dict[str, Any] | None:
        now = time.monotonic()
        with revision_cache_lock:
            cached = revision_cache.get(probe_key)
            if cached is None:
                return None
            captured_at, revision = cached
            if now - captured_at > _SCOPE_REVISION_CACHE_TTL_S:
                revision_cache.pop(probe_key, None)
                return None
            revision_cache.move_to_end(probe_key)
            return revision

    def _remember_omission(probe_key: tuple) -> None:
        with revision_cache_lock:
            omission_cache[probe_key] = time.monotonic()
            omission_cache.move_to_end(probe_key)
            while len(omission_cache) > _SCOPE_REVISION_CACHE_MAX:
                omission_cache.popitem(last=False)

    def _cached_omission(probe_key: tuple) -> bool:
        now = time.monotonic()
        with revision_cache_lock:
            captured_at = omission_cache.get(probe_key)
            if captured_at is None:
                return False
            if now - captured_at > _SCOPE_REVISION_CACHE_TTL_S:
                omission_cache.pop(probe_key, None)
                return False
            omission_cache.move_to_end(probe_key)
            return True

    def _revision_is_current(
            run_id: str, log_sig: list, expected: dict[str, Any],
            remaining_bytes: int) -> tuple[bool, int]:
        """Validate a persisted revision without reparsing an unchanged log on every GET."""
        if not _complete_source_revision(expected):
            return False, 0
        expected_bytes = expected["event_bytes"]
        if expected_bytes > remaining_bytes:
            return False, 0
        before = _source_probe_key(run_id, log_sig)
        cached = _cached_revision(before)
        if cached is not None:
            return cached == expected, expected_bytes
        if _cached_omission(before):
            return False, expected_bytes
        try:
            source = capture_scope_source(
                srv.root, run_id, event_budget_bytes=max(1, remaining_bytes))
        except ScopeSourceError:
            # Negative-cache an unchanged corrupt/inaccessible snapshot. It is already stale, and
            # reparsing the same bounded-but-large event log on every GET cannot improve that fact.
            _remember_omission(before)
            return False, expected_bytes
        after = _source_probe_key(run_id, source.revision["log_sig"])
        if before != after or source.revision["log_sig"] != log_sig:
            return False, source.event_bytes
        # ordinary rewrites invalidate dev/ino/ctime/size/mtime immediately. The bounded
        # TTL retains a periodic full-byte check for exotic filesystems that can preserve all of those
        # fields, while stable GETs reuse one parsed revision instead of rebuilding every Event object.
        _remember_revision(after, source.revision)
        return source.revision == expected, source.event_bytes

    def _omission_is_current(
            run_id: str, log_sig: list, expected_probe: str,
            remaining_bytes: int) -> tuple[bool, int]:
        """Keep an omitted source explicit, and notice when it becomes model-visible evidence."""
        event_bytes = int(log_sig[5]) if _valid_scope_sig_row(log_sig) and len(log_sig) == 7 else 0
        if event_bytes > remaining_bytes:
            return False, 0
        observed_probe, probe_key = _source_probe_receipt(run_id, log_sig)
        if observed_probe != expected_probe:
            return False, event_bytes
        if probe_key is not None and _cached_revision(probe_key) is not None:
            return False, event_bytes
        if probe_key is None:
            return True, event_bytes
        try:
            source = capture_scope_source(
                srv.root, run_id, event_budget_bytes=max(1, remaining_bytes))
        except ScopeSourceError:
            after_probe, after_key = _source_probe_receipt(run_id, log_sig)
            if after_probe != expected_probe:
                return False, event_bytes
            if after_key is not None:
                _remember_omission(after_key)
            return True, event_bytes
        _after_probe, after_key = _source_probe_receipt(
            run_id, source.revision["log_sig"])
        if after_key is not None:
            # The report is stale because a previously omitted source is now readable. Retain the
            # exact successful revision so subsequent GET observers do not repeatedly parse the same
            # unchanged event prefix while the operator decides whether to regenerate.
            _remember_revision(after_key, source.revision)
        # a negative cache may skip work only when the answer is already stale. An
        # omitted receipt needs a current failed-open observation before it can authorize
        # ``stale:false``: accessibility is not part of the cheap stat key, so a transient lock can
        # clear without changing that key. A formerly omitted run is therefore always new evidence.
        return False, event_bytes

    def _action_response(receipt: dict[str, Any]) -> dict[str, Any]:
        action_id = receipt["action_id"]
        if receipt["status"] == "running":
            return {"status": "running", "action_id": action_id,
                    "job_id": receipt["job_id"]}
        return {**receipt["result"], "status": receipt["status"], "action_id": action_id}

    def _indeterminate_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            **receipt,
            "status": "indeterminate",
            "updated_at": int(time.time() * 1000),
            "result": _scope_action_failure(
                _SCOPE_ACTION_INDETERMINATE, receipt["action_id"]),
        }

    def _reconcile_running_action(
            scope_type: str, scope_id: str,
            receipt: dict[str, Any]) -> dict[str, Any]:
        """Turn an orphaned/done-without-ledger running claim into a durable unknown terminal.

        The volatile result is deliberately ignored. A successful paid payload is authoritative only
        after the worker strictly published both the canonical report and the compact action terminal.
        """
        if receipt["status"] != "running":
            return receipt
        retained_leases = _scope_action_leases_are_retained(
            _reports_dir, receipt["action_id"])
        marker = _read_scope_action_lease_marker(
            _reports_dir, receipt["action_id"])
        expected_marker = _action_lease_marker(
            scope_type, scope_id, receipt["action_id"])
        if marker is None:
            fence = _read_scope_action_fence(_reports_dir, scope_type, scope_id)
            if (fence is None or fence["status"] != "active"
                    or fence["action_id"] != receipt["action_id"]):
                raise _ScopeReportStorageConflict(
                    "running scope report action marker disappeared without its exact fence")
            if _read_scope_lease_marker(
                    _reports_dir, scope_type, scope_id) is None:
                raise _ScopeReportStorageConflict(
                    "running scope report lease markers disappeared")
            if (not retained_leases
                    and _scope_action_scope_lease_is_live(
                        _reports_dir, scope_type, scope_id)):
                # The scope lease is the independent proof that a worker may still hold an unlinked
                # action-lock inode. Never recreate that marker while paid work can be live.
                return receipt
            _ensure_lease_marker(
                _scope_action_lease_path(_reports_dir, receipt["action_id"]),
                expected_marker,
            )
        elif marker != expected_marker:
            raise _ScopeReportActionConflict(
                "running scope report action marker belongs to another scope")
        if _read_scope_lease_marker(_reports_dir, scope_type, scope_id) is None:
            if (not retained_leases
                    and _scope_action_lease_is_live(
                        _reports_dir, receipt["action_id"])):
                return receipt
            fence = _read_scope_action_fence(_reports_dir, scope_type, scope_id)
            if (fence is None or fence["status"] != "active"
                    or fence["action_id"] != receipt["action_id"]):
                raise _ScopeReportStorageConflict(
                    "running scope report scope marker disappeared without its exact fence")
            _ensure_lease_marker(
                _scope_action_scope_lease_path(
                    _reports_dir, scope_type, scope_id),
                _scope_lease_marker(scope_type, scope_id),
            )
        if retained_leases:
            # The provider already returned, but neither its terminal nor fallback tombstone could
            # be strictly confirmed. A later exact reconciliation may retry the conservative write;
            # only after it succeeds are both quarantining handles released.
            reconciled = _write_scope_action_receipt(
                _reports_dir, scope_type, scope_id, _indeterminate_receipt(receipt))
            _release_retained_scope_action_leases(
                _reports_dir, receipt["action_id"])
            return reconciled
        # JobRegistry is process-local, but deployments may run multiple ASGI workers.
        # The OS lease is the cross-process liveness authority; a sibling must not orphan live paid
        # work merely because it cannot see this worker's in-memory job receipt.
        if _scope_action_lease_is_live(_reports_dir, receipt["action_id"]):
            return receipt
        # The existing marker was successfully locked, so no process owns the paid worker anymore.
        # Volatile JobRegistry state may itself be orphaned (for example a BaseException escaped its
        # worker); it cannot override the cross-process liveness authority.
        reconciled = _write_scope_action_receipt(
            _reports_dir, scope_type, scope_id, _indeterminate_receipt(receipt))
        srv.jobs.discard_orphaned_running(receipt["job_id"])
        return reconciled

    def _read_reconciled_action(
            scope_type: str, scope_id: str, action_id: str) -> dict[str, Any] | None:
        receipt = _read_scope_action_receipt(
            _reports_dir, scope_type, scope_id, action_id)
        if receipt is None:
            marker = _read_scope_action_lease_marker(_reports_dir, action_id)
            fence = _read_scope_action_fence(_reports_dir, scope_type, scope_id)
            retained_leases = _scope_action_leases_are_retained(
                _reports_dir, action_id)
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
                    _reports_dir, scope_type, scope_id)
                if scope_marker is None:
                    raise _ScopeReportStorageConflict(
                        "active scope report lease markers disappeared")
                if (not retained_leases
                        and _scope_action_scope_lease_is_live(
                            _reports_dir, scope_type, scope_id)):
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
                    _scope_action_lease_path(_reports_dir, action_id),
                    _action_lease_marker(scope_type, scope_id, action_id),
                )
                marker = _read_scope_action_lease_marker(_reports_dir, action_id)
            expected_marker = _action_lease_marker(scope_type, scope_id, action_id)
            if marker != expected_marker:
                raise _ScopeReportActionConflict(
                    "scope report action marker belongs to another scope")
            if fence is not None and not fence_binds_action:
                # Never replace another UUID's scope authority. A dead scope-bound marker can still
                # own its own conservative tombstone, which lets the stale tab reconcile/discard A
                # without touching a live or clear fence for B.
                retained_leases = _scope_action_leases_are_retained(
                    _reports_dir, action_id)
                if (not retained_leases
                        and _scope_action_lease_is_live(_reports_dir, action_id)):
                    raise _ScopeReportStorageConflict(
                        "live scope report action conflicts with another action fence")
                receipt = _write_scope_action_receipt(
                    _reports_dir, scope_type, scope_id,
                    _missing_scope_action_receipt(scope_type, scope_id, action_id))
                if retained_leases:
                    _release_retained_scope_action_leases(_reports_dir, action_id)
                return receipt
            if (not retained_leases
                    and _scope_action_lease_is_live(_reports_dir, action_id)):
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
                _reports_dir, scope_type, scope_id,
                _missing_scope_action_receipt(scope_type, scope_id, action_id))
            if retained_leases:
                _release_retained_scope_action_leases(_reports_dir, action_id)
            if fence is None:
                scope_marker = _read_scope_lease_marker(
                    _reports_dir, scope_type, scope_id)
                if scope_marker is None:
                    _ensure_lease_marker(
                        _scope_action_scope_lease_path(
                            _reports_dir, scope_type, scope_id),
                        _scope_lease_marker(scope_type, scope_id),
                    )
                elif _scope_action_scope_lease_is_live(
                        _reports_dir, scope_type, scope_id):
                    raise _ScopeReportStorageConflict(
                        "scope report action fence disappeared while a worker is live")
                _write_scope_action_fence(
                    _reports_dir, scope_type, scope_id, action_id, "active")
        if (receipt["status"] == "indeterminate"
                and _scope_action_leases_are_retained(_reports_dir, action_id)):
            # The fallback replace may itself have become visible before parent sync failed. Strictly
            # confirm that conservative tombstone before releasing the quarantining handles.
            receipt = _write_scope_action_receipt(
                _reports_dir, scope_type, scope_id, receipt)
            _release_retained_scope_action_leases(_reports_dir, action_id)
        if receipt["status"] in {"done", "abandoned"}:
            marker = _read_scope_action_lease_marker(_reports_dir, action_id)
            if _scope_action_leases_are_retained(_reports_dir, action_id):
                receipt = _write_scope_action_receipt(
                    _reports_dir, scope_type, scope_id,
                    _indeterminate_receipt(receipt))
                _release_retained_scope_action_leases(
                    _reports_dir, action_id)
            elif marker is not None:
                if marker != _action_lease_marker(scope_type, scope_id, action_id):
                    raise _ScopeReportActionConflict(
                        "scope report action marker belongs to another scope")
                if _scope_action_lease_is_live(_reports_dir, action_id):
                    # The worker writes terminal before releasing its leases. Treat a visible terminal
                    # as running until the cross-process hand-off completes.
                    return {**receipt, "status": "running", "result": None}
            else:
                fence = _read_scope_action_fence(
                    _reports_dir, scope_type, scope_id)
                scope_marker = _read_scope_lease_marker(
                    _reports_dir, scope_type, scope_id)
                if (fence is not None and fence["status"] == "active"
                        and fence["action_id"] == action_id
                        and scope_marker is not None
                        and _scope_action_scope_lease_is_live(
                            _reports_dir, scope_type, scope_id)):
                    # A worker writes terminal before releasing both handles. If its action marker
                    # was unlinked, the independent scope lease still quarantines that visible file.
                    return {**receipt, "status": "running", "result": None}
            # Confirmation cannot be process-local: a strict replace may be visible even though its
            # parent sync failed. Re-publish every authority-granting terminal before returning it.
            try:
                receipt = _write_scope_action_receipt(
                    _reports_dir, scope_type, scope_id, receipt)
            except _ScopeReportStorageConflict:
                return _indeterminate_receipt(receipt)
        receipt = _reconcile_running_action(scope_type, scope_id, receipt)
        if receipt["status"] in {"done", "abandoned", "indeterminate"}:
            # A strict read-back is itself sufficient durable confirmation. This also closes the
            # narrow window where publication succeeded but the worker was interrupted before it
            # could flip the process-local receipt's consumption policy.
            srv.jobs.mark_consumable(receipt["job_id"])
            srv.jobs.poll(receipt["job_id"])
        return receipt

    def _active_scope_action(
            scope_type: str, scope_id: str) -> dict[str, Any] | None:
        scope_marker = _read_scope_lease_marker(
            _reports_dir, scope_type, scope_id)
        fence = _read_scope_action_fence(_reports_dir, scope_type, scope_id)
        if scope_marker is None:
            if fence is None:
                return None
            if fence["status"] != "clear":
                raise _ScopeReportStorageConflict(
                    "active scope report scope lease marker disappeared")
            action_marker = _read_scope_action_lease_marker(
                _reports_dir, fence["action_id"])
            if (action_marker != _action_lease_marker(
                    scope_type, scope_id, fence["action_id"])
                    or _scope_action_lease_is_live(
                        _reports_dir, fence["action_id"])):
                raise _ScopeReportStorageConflict(
                    "scope report scope lease marker cannot be safely reconstructed")
            # A clear exact fence plus the dead immutable action marker proves no paid worker can
            # still own this scope. Strictly reconstruct the deterministic scope marker so one lost
            # metadata file does not brick all future actions.
            _ensure_lease_marker(
                _scope_action_scope_lease_path(
                    _reports_dir, scope_type, scope_id),
                _scope_lease_marker(scope_type, scope_id),
            )
            scope_marker = _read_scope_lease_marker(
                _reports_dir, scope_type, scope_id)
            if scope_marker is None:
                raise _ScopeReportStorageConflict(
                    "scope report scope lease marker reconstruction failed")
        scope_live = _scope_action_scope_lease_is_live(
            _reports_dir, scope_type, scope_id)
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
        receipt = _read_reconciled_action(
            scope_type, scope_id, fence["action_id"])
        if receipt is None:
            raise _ScopeReportStorageConflict(
                "active scope report action receipt disappeared")
        if receipt["status"] in {"done", "abandoned"}:
            _write_scope_action_fence(
                _reports_dir, scope_type, scope_id,
                fence["action_id"], "clear")
            return None
        return receipt

    # action observation has its own namespace because scope ids are opaque paths. A
    # suffix route under ``/scope-report/...`` would steal a legitimate scope such as
    # ``family/actions/<uuid>`` from the catch-all report GET. The expected scope stays explicit in
    # the query and is verified against the durable receipt before any result is disclosed.
    @router.get("/api/scope-report-actions/{action_id}")
    def get_scope_report_action(scope_type: str, scope_id: str, action_id: str):
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
        normalized = _scope_action_id(action_id)
        if normalized is None:
            raise HTTPException(400, "bad scope report action id")
        try:
            with _scope_store_lock(_reports_dir):
                receipt = _read_reconciled_action(scope_type, scope_id, normalized)
        except _ScopeReportActionConflict as exc:
            raise HTTPException(409, _SCOPE_ACTION_CONFLICT) from exc
        except _ScopeReportStorageConflict as exc:
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
        if receipt is None:
            # unknown means no durable claim exists. The client may safely retry only the
            # same UUID; it must never mint a replacement action merely because a volatile job vanished.
            return {"status": "unknown", "action_id": normalized}
        return _action_response(receipt)

    @router.post("/api/scope-report-actions/{action_id}/abandon")
    def abandon_scope_report_action(scope_type: str, scope_id: str, action_id: str):
        """Explicitly release an indeterminate paid-action fence without erasing its identity.

        Abandon is intentionally never automatic: after a process crash the provider outcome cannot
        be proven. The old UUID remains a durable tombstone, and only an explicit new UUID may bill a
        new attempt. A process-local running worker always wins the race and makes abandon a conflict.
        """
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
        normalized = _scope_action_id(action_id)
        if normalized is None:
            raise HTTPException(400, "bad scope report action id")
        try:
            with _scope_store_lock(_reports_dir):
                fence = _read_scope_action_fence(_reports_dir, scope_type, scope_id)
                receipt = _read_scope_action_receipt(
                    _reports_dir, scope_type, scope_id, normalized)
                fence_active_exact = (
                    fence is not None and fence["status"] == "active"
                    and fence["action_id"] == normalized)
                scope_worker_live = False
                if fence_active_exact:
                    scope_marker = _read_scope_lease_marker(
                        _reports_dir, scope_type, scope_id)
                    if scope_marker is None:
                        action_marker = _read_scope_action_lease_marker(
                            _reports_dir, normalized)
                        if action_marker != _action_lease_marker(
                                scope_type, scope_id, normalized):
                            raise _ScopeReportStorageConflict(
                                "active scope report lease markers disappeared")
                        if _scope_action_lease_is_live(_reports_dir, normalized):
                            raise HTTPException(409, {
                                **_SCOPE_ACTION_ACTIVE,
                                "action_id": normalized,
                            })
                        _ensure_lease_marker(
                            _scope_action_scope_lease_path(
                                _reports_dir, scope_type, scope_id),
                            _scope_lease_marker(scope_type, scope_id),
                        )
                    scope_worker_live = _scope_action_scope_lease_is_live(
                        _reports_dir, scope_type, scope_id)
                if receipt is None:
                    marker = _read_scope_action_lease_marker(
                        _reports_dir, normalized)
                    expected_marker = _action_lease_marker(
                        scope_type, scope_id, normalized)
                    fence_binds_action = (
                        fence is not None and fence["action_id"] == normalized)
                    marker_lease: _ScopeActionLease | None = None
                    repair_missing_fence = False
                    retained_leases = _scope_action_leases_are_retained(
                        _reports_dir, normalized)
                    if marker is not None:
                        if marker != expected_marker:
                            raise _ScopeReportActionConflict(
                                "scope report action marker belongs to another scope")
                        if (not retained_leases
                                and _scope_action_lease_is_live(
                                    _reports_dir, normalized)):
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
                                _reports_dir, scope_type, scope_id)
                            if (scope_marker is not None
                                    and _scope_action_scope_lease_is_live(
                                        _reports_dir, scope_type, scope_id)):
                                raise HTTPException(409, {
                                    **_SCOPE_ACTION_ACTIVE,
                                    "action_id": normalized,
                                })
                            if scope_marker is None:
                                _ensure_lease_marker(
                                    _scope_action_scope_lease_path(
                                        _reports_dir, scope_type, scope_id),
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
                                _scope_action_lease_path(_reports_dir, normalized),
                                expected_marker,
                            )
                        else:
                            marker_lease = _acquire_scope_action_lease(
                                _reports_dir, scope_type, scope_id, normalized)
                            if marker_lease is None:
                                raise HTTPException(409, {
                                    **_SCOPE_ACTION_ACTIVE,
                                    "action_id": normalized,
                                })
                    base = _missing_scope_action_receipt(
                        scope_type, scope_id, normalized)
                    try:
                        receipt = _write_scope_action_receipt(
                            _reports_dir, scope_type, scope_id, {
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
                            _reports_dir, scope_type, scope_id, normalized, "clear")
                    if retained_leases:
                        _release_retained_scope_action_leases(
                            _reports_dir, normalized)
                    return _action_response(receipt)
                receipt = _read_reconciled_action(scope_type, scope_id, normalized)
                assert receipt is not None
                repair_missing_fence = False
                if fence is None:
                    scope_marker = _read_scope_lease_marker(
                        _reports_dir, scope_type, scope_id)
                    if (scope_marker is not None
                            and _scope_action_scope_lease_is_live(
                                _reports_dir, scope_type, scope_id)):
                        raise HTTPException(409, {
                            **_SCOPE_ACTION_ACTIVE,
                            "action_id": normalized,
                        })
                    if scope_marker is None:
                        _ensure_lease_marker(
                            _scope_action_scope_lease_path(
                                _reports_dir, scope_type, scope_id),
                            _scope_lease_marker(scope_type, scope_id),
                        )
                    repair_missing_fence = True
                if receipt["status"] in {"done", "abandoned"}:
                    if (repair_missing_fence
                            or (fence is not None and fence["status"] == "active"
                                and fence["action_id"] == normalized)):
                        _write_scope_action_fence(
                            _reports_dir, scope_type, scope_id, normalized, "clear")
                    return _action_response(receipt)
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
                    _reports_dir, scope_type, scope_id, abandoned)
                if (repair_missing_fence
                        or (fence is not None and fence["status"] == "active"
                            and fence["action_id"] == normalized)):
                    _write_scope_action_fence(
                        _reports_dir, scope_type, scope_id, normalized, "clear")
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
        return _action_response(receipt)

    # scope ids are opaque persisted identities, so the route must preserve legal
    # task/project ids containing ``/`` instead of truncating or rejecting them at the HTTP boundary.
    @router.get("/api/scope-report/{scope_type}/{scope_id:path}")
    def get_scope_report(scope_type: str, scope_id: str):
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
        cur_ids = _scope_run_ids(scope_type, scope_id)
        publication_quarantined = False
        try:
            with _scope_store_lock(_reports_dir):
                rec = _read_or_migrate_scope_record(
                    _reports_dir, scope_type, scope_id)
                if (rec is not None
                        and not _action_bound_scope_record_is_confirmed(
                            _reports_dir, rec, scope_type, scope_id)):
                    # Never expose uncommitted paid prose, but keep the endpoint usable after reload:
                    # a safe logical-missing projection preserves the Generate affordance. The old
                    # canonical bytes stay quarantined until a later confirmed action replaces them.
                    publication_quarantined = True
                    rec = None
        except _ScopeReportStorageConflict as exc:
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
        if rec is None:
            response = {"exists": False, "run_count": len(cur_ids),
                        "label": _scope_label(scope_type, scope_id)}
            if publication_quarantined:
                response.update({
                    "quarantined": True,
                    "stale": True,
                    **_SCOPE_PUBLICATION_UNCONFIRMED,
                })
            return response
        added = sorted(set(cur_ids) - set(rec.get("run_ids", [])))
        rec, legacy_authority = _public_scope_record(rec)
        current_sig = _scope_sig(cur_ids)
        stale_reason = "report_authority_upgrade" if legacy_authority else None
        stale = legacy_authority
        source_revisions = rec.get("source_revisions")
        omitted_runs = rec.get("omitted_runs")
        omitted_source_probes = rec.get("omitted_source_probes")
        expected_context = rec.get("context_digest")
        if rec.get("context_schema") != _SCOPE_CONTEXT_SCHEMA:
            # Schema 1 digested the workspace-global projects store; digestless records predate even
            # that receipt. Neither can prove the new scope-local semantic slice, so retire them with
            # an explicit one-time migration reason instead of claiming that this scope changed.
            stale = True
            stale_reason = stale_reason or "report_format_upgrade"
        elif (not isinstance(expected_context, str)
                or _RUN_GENERATION_RE.fullmatch(expected_context) is None):
            stale = True
            stale_reason = stale_reason or "report_format_upgrade"
        elif _scope_context_digest(scope_type, scope_id, cur_ids) != expected_context:
            stale = True
            stale_reason = stale_reason or "scope_context_changed"
        if current_sig != rec.get("sig"):
            stale = True
            stale_reason = stale_reason or "scope_evidence_changed"
        if not stale and not isinstance(source_revisions, list):
            # Pre-v2 records did not bind task/config snapshots or the full event prefix.
            stale = True
            stale_reason = "report_source_receipt_upgrade"
        elif not stale:
            try:
                remaining = MAX_SCOPE_TOTAL_EVENT_BYTES
                sig_by_id = {row[0]: row for row in current_sig}
                revision_by_id = {row["run_id"]: row for row in source_revisions}
                omitted = set(omitted_runs or ())
                if (not isinstance(omitted_source_probes, dict)
                        or set(omitted_source_probes) != omitted):
                    stale = True
                for run_id in rec.get("run_ids", []):
                    if stale:
                        break
                    if run_id in revision_by_id:
                        matches, consumed = _revision_is_current(
                            run_id, sig_by_id[run_id], revision_by_id[run_id], remaining)
                    elif run_id in omitted:
                        matches, consumed = _omission_is_current(
                            run_id, sig_by_id[run_id], omitted_source_probes[run_id], remaining)
                    else:
                        matches, consumed = False, 0
                    remaining -= consumed
                    if not matches:
                        stale = True
                        stale_reason = "scope_evidence_changed"
                        break
            except ScopeSourceError:
                stale = True
                stale_reason = "scope_evidence_changed"
        return {**rec, "exists": True, "stale": stale,
                "stale_reason": stale_reason,
                "current_run_count": len(cur_ids), "added": added}

    @router.post("/api/scope-report/{scope_type}/{scope_id:path}/generate")
    async def generate_scope_report_ep(
            scope_type: str, scope_id: str,
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        """Generate (or regenerate) the cross-run report for a scope. On-demand only — the agent reads
        a bounded/redacted projection of at most ``MAX_SCOPE_REPORT_RUNS`` runs and may request a
        bounded node drill. Degrades to a metrics rollup offline. Runs as a BACKGROUND JOB: reading +
        synthesizing over many runs can outlast a UI proxy's gateway timeout, so a slow synthesis hands
        back a job_id the UI polls (a fast/offline one still returns inline within the wait — no 504)."""
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
        action_id = _scope_action_id(idempotency_key)
        if action_id is None:
            raise HTTPException(428 if idempotency_key is None else 400, _SCOPE_ACTION_REQUIRED)
        # OFF the event loop, ahead of the job hand-off. `_scope_store_lock` is a GLOBAL thread lock
        # plus an interprocess file lock, `_read_reconciled_action` does strict lease/fence file I/O
        # and re-publication, `_scope_run_ids` re-runs the whole runs-list fold, `_scope_sig` and
        # `_scope_source_sizes` stat every run, and `_scope_context_digest` reloads the project store.
        # Run inline on this `async def` handler's loop, contention on that single store lock — one
        # other generation publishing — stalled the ENTIRE event loop, not just this request.
        # Returns `(early_response, preflight)`; exactly one is non-None. HTTPExceptions raised inside
        # propagate out of the worker unchanged.
        def _preflight():
            try:
                with _scope_store_lock(_reports_dir):
                    existing_action = _read_reconciled_action(
                        scope_type, scope_id, action_id)
            except _ScopeReportActionConflict as exc:
                raise HTTPException(409, _SCOPE_ACTION_CONFLICT) from exc
            except _ScopeReportStorageConflict as exc:
                raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
            if existing_action is not None:
                # Durable replay happens before current-scope preflight: an old action remains observable
                # after its inputs change, its report is superseded, or its volatile job receipt is consumed.
                return _action_response(existing_action), None
            run_ids = sorted(set(_scope_run_ids(scope_type, scope_id)))
            if not run_ids:
                raise HTTPException(400, "no runs in this scope")
            if len(run_ids) > MAX_SCOPE_REPORT_RUNS:
                raise HTTPException(413, {
                    "code": "scope_report_too_large",
                    "message": (
                        f"This scope has {len(run_ids)} runs; paid synthesis is limited to "
                        f"{MAX_SCOPE_REPORT_RUNS} model-visible runs."
                    ),
                    "run_count": len(run_ids),
                    "max_runs": MAX_SCOPE_REPORT_RUNS,
                    "remediation": "Generate reports for narrower child scopes.",
                })
            try:
                requested_source_sizes = _scope_source_sizes(run_ids)
            except ScopeSourceCapacityError as exc:
                raise HTTPException(413, _SCOPE_SOURCE_TOO_LARGE) from exc
            requested_scope_ids = list(run_ids)
            requested_scope_sig = _scope_sig(requested_scope_ids)
            requested_context_digest = _scope_context_digest(
                scope_type, scope_id, requested_scope_ids)
            requested_probe_receipts = {
                run_id: _source_probe_receipt(run_id, row)[0]
                for run_id, row in ((row[0], row) for row in requested_scope_sig)
            }
            generation_identity = "scope-report:" + hashlib.sha256(json.dumps(
                {
                    "scope": _scope_identity(scope_type, scope_id),
                    "run_ids": requested_scope_ids,
                    "sig": requested_scope_sig,
                    "source_sizes": requested_source_sizes,
                    "context_digest": requested_context_digest,
                    "source_probes": requested_probe_receipts,
                },
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            try:
                with _scope_store_lock(_reports_dir):
                    _read_or_migrate_scope_record(_reports_dir, scope_type, scope_id)
            except _ScopeReportStorageConflict as exc:
                # Never enqueue paid work that cannot safely publish its result afterward.
                raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
            return None, (run_ids, requested_scope_ids, requested_scope_sig,
                          requested_source_sizes, requested_context_digest,
                          requested_probe_receipts, generation_identity)

        early, preflight = await anyio.to_thread.run_sync(_preflight)
        if early is not None:
            return early
        (run_ids, requested_scope_ids, requested_scope_sig, requested_source_sizes,
         requested_context_digest, requested_probe_receipts, generation_identity) = preflight

        def _stamp_scope_action_usage(usage: dict[str, Any]) -> bool:
            """Fold one paid-call observation into this worker's durable action receipt, in place.

            Returns False when the ledger row is no longer exactly the running claim this worker
            wrote — another process reconciled it, or durable storage went away. The caller must then
            refuse to publish and refuse to claim success: an action that cannot record what it spent
            must never reach an authoritative terminal.
            """
            nonlocal running_receipt
            if not _valid_scope_action_usage(usage):
                return False
            updated = {**running_receipt, "usage": usage,
                       "updated_at": int(time.time() * 1000)}
            try:
                with _scope_store_lock(_reports_dir):
                    current = _read_scope_action_receipt(
                        _reports_dir, scope_type, scope_id, action_id)
                    # Same exact-identity rule `_persist_terminal` uses: a worker may only edit the
                    # running claim it wrote itself, never a row someone else has since moved.
                    if current is None or current != running_receipt:
                        return False
                    _write_scope_action_receipt(
                        _reports_dir, scope_type, scope_id, updated)
            except (_ScopeReportActionConflict, _ScopeReportStorageConflict):
                return False
            # Only after the strict write survived: `_persist_terminal` derives its terminal from this
            # exact dict, so the local copy and the durable row must never diverge.
            running_receipt = updated
            return True

        # False once a provider call happened whose spend could NOT be written to the ledger. Read by
        # `_persist_terminal`, which then withholds the authoritative terminal.
        usage_recorded = True

        def _compute() -> dict:
            nonlocal usage_recorded
            frozen_scope_ids = requested_scope_ids
            current_ids = sorted(set(_scope_run_ids(scope_type, scope_id)))
            if (current_ids != frozen_scope_ids
                    or _scope_sig(current_ids) != requested_scope_sig
                    or _scope_context_digest(scope_type, scope_id, current_ids)
                    != requested_context_digest):
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            try:
                frozen_source_sizes = _scope_source_sizes(frozen_scope_ids)
            except ScopeSourceCapacityError:
                return {"ok": False, **_SCOPE_SOURCE_TOO_LARGE}
            if frozen_source_sizes != requested_source_sizes:
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            frozen_scope_sig = requested_scope_sig
            frozen_sig_by_id = {row[0]: row for row in frozen_scope_sig}
            frozen_context_digest = requested_context_digest
            labels = projects.load().get("labels", {})
            briefs = []
            frozen_runs: dict[str, FrozenScopeSource] = {}
            frozen_probe_keys: dict[str, tuple] = {}
            frozen_probe_receipts: dict[str, str] = {}
            consumed_event_bytes = 0
            for rid in frozen_scope_ids:
                expected_bytes = frozen_source_sizes.get(rid, 0)
                before_probe, before_key = _source_probe_receipt(rid, frozen_sig_by_id[rid])
                # the reservation owns the source probe observed by the POST handler.
                # Task/config snapshots are intentionally absent from the event-log signature and
                # size map, so accepting a different first worker probe would silently rebase this
                # paid job and let a later request reserve a second identity for the same evidence.
                if before_probe != requested_probe_receipts[rid]:
                    return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                frozen_probe_receipts[rid] = before_probe
                try:
                    source = capture_scope_source(
                        srv.root, rid,
                        event_budget_bytes=max(
                            1, MAX_SCOPE_TOTAL_EVENT_BYTES - consumed_event_bytes),
                    )
                    after_probe, after_key = _source_probe_receipt(
                        rid, source.revision["log_sig"])
                    if (source.event_bytes != expected_bytes or before_probe != after_probe
                            or before_key is None or after_key is None
                            or source.revision["log_sig"] != frozen_sig_by_id[rid]):
                        return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                    briefs.append(_run_brief(rid, labels, source))
                    frozen_runs[rid] = source
                    frozen_probe_keys[rid] = after_key
                    _remember_revision(after_key, source.revision)
                except ScopeSourceCapacityError:
                    return {"ok": False, **_SCOPE_SOURCE_TOO_LARGE}
                except ScopeSourceError:
                    if before_key is not None:
                        _remember_omission(before_key)
                    continue
                finally:
                    consumed_event_bytes += expected_bytes
            scope = {
                "type": scope_type,
                "id": scope_id,
                "label": _scope_label(scope_type, scope_id),
                # preserve honest scope coverage even when a corrupt/unreadable run cannot
                # contribute a brief. The model sees only frozen briefs; the receipt counts the omission.
                "source_run_count": len(frozen_scope_ids),
            }
            brief_ids = [brief["run_id"] for brief in briefs]
            source_revisions = [frozen_runs[rid].revision for rid in brief_ids]
            omitted = sorted(set(frozen_scope_ids) - set(brief_ids))

            def _inputs_unchanged() -> bool:
                current_ids = sorted(set(_scope_run_ids(scope_type, scope_id)))
                current_sig = _scope_sig(current_ids)
                if current_ids != frozen_scope_ids or current_sig != frozen_scope_sig:
                    return False
                if (_scope_context_digest(scope_type, scope_id, current_ids)
                        != frozen_context_digest):
                    return False
                try:
                    current_sizes = _scope_source_sizes(current_ids)
                    if current_sizes != frozen_source_sizes:
                        return False
                    current_sig_by_id = {row[0]: row for row in current_sig}
                    for rid in frozen_scope_ids:
                        current_probe, _current_key = _source_probe_receipt(
                            rid, current_sig_by_id[rid])
                        if current_probe != frozen_probe_receipts[rid]:
                                return False
                    # A cheap identity can stay unchanged when transient access is repaired. Re-open
                    # every omitted source at each paid/publication fence; newly capturable evidence
                    # invalidates this incomplete snapshot before it can spend or publish.
                    remaining = MAX_SCOPE_TOTAL_EVENT_BYTES
                    for rid in frozen_scope_ids:
                        if rid in frozen_runs:
                            remaining -= frozen_source_sizes.get(rid, 0)
                            continue
                        try:
                            capture_scope_source(
                                srv.root, rid, event_budget_bytes=max(1, remaining))
                        except ScopeSourceError:
                            pass
                        else:
                            return False
                        remaining -= frozen_source_sizes.get(rid, 0)
                    return True
                except ScopeSourceError:
                    return False

            # the capture already bound every model-visible byte. Re-check its complete
            # cheap identity before client construction/publication so ordinary races consume no paid
            # call, without reparsing the same event log three more times inside one generation job.
            if not _inputs_unchanged():
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            from looplab.serve.scope_report import generate_scope_report as _gen
            s = srv.llm_settings(None)
            try:
                from looplab.core.llm import resolve_llm_target
                scope_model = resolve_llm_target(s).model
            except Exception:  # noqa: BLE001 - construction below owns the public soft-failure path
                scope_model = s.llm_model
            # No provider call without a durable "a paid attempt starts here" row: a kill between
            # acceptance and this process's next write would otherwise leave an action ledger that
            # cannot say whether anything was ever billed for this scope.
            if not _stamp_scope_action_usage(_attempted_scope_usage(scope_model)):
                return {"ok": False, **_SCOPE_STORAGE_ERROR}
            client = None
            try:
                from looplab.core.llm import make_llm_client_for
                client = make_llm_client_for(s, factory=srv.make_llm_client)
                # Paid cross-run synthesis is an interactive bounded operation. Global agent settings
                # may be unlimited for autonomous engine work; this endpoint supplies finite defaults,
                # and generate_scope_report independently enforces hard maxima.
                drill = lambda run_id, node_id: _scope_drill(  # noqa: E731
                    frozen_runs, run_id, node_id)
                content = _gen(scope, briefs, client, parser=s.llm_parser, drill=drill,
                               max_turns=(getattr(s, "agent_max_turns", 0)
                                          or DEFAULT_SCOPE_REPORT_TURNS),
                               time_budget_s=(getattr(s, "agent_time_budget_s", 0.0)
                                              or DEFAULT_SCOPE_REPORT_TIME_S))
            except Exception:  # noqa: BLE001 - offline -> deterministic rollup still persists
                content = _gen(scope, briefs, None)
            finally:
                # A tool loop that failed HALFWAY still spent every call it made before raising, so
                # the observation belongs in the `finally`, not the success path.
                usage = _observed_scope_usage(client, scope_model)
            # Spend joins the ledger BEFORE the report may be published. Unlike the pre-call stamp
            # this must NOT abandon the run: the model has already been paid for, and throwing the
            # generated report away would waste that spend on top of failing to record it. Instead
            # the failure is remembered, and `_persist_terminal` refuses to turn an unrecorded paid
            # call into an authoritative `done`.
            if not _stamp_scope_action_usage(usage):
                usage_recorded = False
            if not _inputs_unchanged():
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            rec = {"scope_identity": _scope_identity(scope_type, scope_id), "scope": scope,
                   "action_id": action_id,
                   "generated_at": int(time.time() * 1000), "run_ids": frozen_scope_ids,
                   # sig and run_ids use the complete scope vocabulary even when one
                   # source is unreadable. omitted_runs says exactly which members supplied no brief.
                   "sig": frozen_scope_sig,
                   "source_revisions": source_revisions,
                   "omitted_runs": omitted,
                   "omitted_source_probes": {
                       rid: frozen_probe_receipts[rid] for rid in omitted},
                   "context_schema": _SCOPE_CONTEXT_SCHEMA,
                   "context_digest": frozen_context_digest,
                   "model": scope_model, "content": content}
            try:
                with _scope_store_lock(_reports_dir):
                    # Narrow the optimistic-check window at the actual publication boundary.
                    if not _inputs_unchanged():
                        return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                    for rid in frozen_runs:
                        _remember_revision(frozen_probe_keys[rid], frozen_runs[rid].revision)
                    # Revalidate the lexical store and re-derive the destination immediately before
                    # publication. A directory/file swapped during the slow model call is refused.
                    _validated_reports_dir(_reports_dir, create=True)
                    _read_or_migrate_scope_record(_reports_dir, scope_type, scope_id)
                    dst = _scope_report_path(_reports_dir, scope_type, scope_id)
                    # The canonical report is the sole paid payload. Confirm its contents and first
                    # directory publication before the action ledger may claim terminal success.
                    strict_atomic_write_text(dst, _serialize_scope_record(rec))
                    dst = _scope_report_path(_reports_dir, scope_type, scope_id)
                    if _read_scope_record(dst, scope_type, scope_id) != rec:
                        raise _ScopeReportStorageConflict(
                            "scope report changed during strict publication")
            except (OSError, RuntimeError, _ScopeReportStorageConflict):
                return {"ok": False, **_SCOPE_STORAGE_ERROR}
            return {"ok": True, **rec, "authoritative": True,
                    "stale": False, "added": []}

        job_identity = "scope-report-action:" + hashlib.sha256(
            action_id.encode("ascii")).hexdigest()
        reservation: dict[str, Any] | None = None
        action_lease: _ScopeActionLease | None = None
        scope_lease: _ScopeActionLease | None = None

        def _cleanup_workerless_claim() -> None:
            nonlocal action_lease, scope_lease
            if reservation is not None and isinstance(reservation.get("job_id"), str):
                srv.jobs.discard_reservation(reservation["job_id"])
            if scope_lease is not None:
                scope_lease.release()
                scope_lease = None
            if action_lease is not None:
                action_lease.release()
                action_lease = None

        try:
            # reserve and durably claim under the same interprocess store fence. No
            # worker can start before this receipt exists, so a lost initial POST can always rejoin
            # by UUID and never needs to guess whether paid work was accepted.
            with _scope_store_lock(_reports_dir):
                existing_action = _read_reconciled_action(
                    scope_type, scope_id, action_id)
                if existing_action is not None:
                    return _action_response(existing_action)
                active = _active_scope_action(scope_type, scope_id)
                if active is not None:
                    raise HTTPException(409, {
                        **_SCOPE_ACTION_ACTIVE,
                        "action_id": active["action_id"],
                    })
                # Check volatile capacity before creating a permanent UUID marker. A rejected fresh
                # action must not leave an orphan identity merely because the shared job pool is full.
                reservation = srv.jobs.reserve(job_identity, consume_on_poll=False)
                if reservation.get("status") != "running":
                    return {**reservation, "action_id": action_id}
                action_lease = _acquire_scope_action_lease(
                    _reports_dir, scope_type, scope_id, action_id)
                if action_lease is None:
                    raise HTTPException(409, {
                        **_SCOPE_ACTION_ACTIVE,
                        "action_id": action_id,
                    })
                scope_lease = _acquire_scope_action_scope_lease(
                    _reports_dir, scope_type, scope_id)
                if scope_lease is None:
                    raise HTTPException(409, {
                        **_SCOPE_ACTION_ACTIVE,
                        "action_id": action_id,
                    })
                running_receipt = {
                    "schema": _SCOPE_ACTION_SCHEMA,
                    "scope_identity": _scope_identity(scope_type, scope_id),
                    "action_id": action_id,
                    "generation_identity": generation_identity,
                    "job_id": reservation["job_id"],
                    "status": "running",
                    "updated_at": int(time.time() * 1000),
                    "result": None,
                }
                _write_scope_action_receipt(
                    _reports_dir, scope_type, scope_id, running_receipt)
                # The root-level per-scope fence is strict before the worker starts. It survives a
                # regular ``reports/`` directory replacement and blocks every different UUID.
                _write_scope_action_fence(
                    _reports_dir, scope_type, scope_id, action_id, "active")
        except HTTPException:
            _cleanup_workerless_claim()
            raise
        except _ScopeReportActionConflict as exc:
            _cleanup_workerless_claim()
            raise HTTPException(409, _SCOPE_ACTION_CONFLICT) from exc
        except _ScopeReportStorageConflict as exc:
            _cleanup_workerless_claim()
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
        except BaseException:
            _cleanup_workerless_claim()
            raise

        def _indeterminate_response() -> dict[str, Any]:
            return {
                **_scope_action_failure(_SCOPE_ACTION_INDETERMINATE, action_id),
                "status": "indeterminate",
            }

        lease_release_allowed = True

        def _persist_terminal(public_result: dict[str, Any]) -> dict[str, Any]:
            nonlocal lease_release_allowed
            durable_result = (_scope_action_success(action_id)
                              if public_result.get("ok") is True
                              else _scope_action_failure(public_result, action_id))
            done_receipt = {
                **running_receipt,
                "status": "done",
                "updated_at": int(time.time() * 1000),
                "result": durable_result,
            }

            def _persist_indeterminate_or_retain() -> None:
                nonlocal lease_release_allowed
                try:
                    _write_scope_action_receipt(
                        _reports_dir, scope_type, scope_id,
                        _indeterminate_receipt(running_receipt))
                except _ScopeReportStorageConflict:
                    lease_release_allowed = False
                    assert action_lease is not None and scope_lease is not None
                    _retain_scope_action_leases(
                        _reports_dir, action_id, action_lease, scope_lease)
                    raise
                srv.jobs.mark_consumable(running_receipt["job_id"])

            try:
                with _scope_store_lock(_reports_dir):
                    current = _read_scope_action_receipt(
                        _reports_dir, scope_type, scope_id, action_id)
                    if current is None:
                        return _indeterminate_response()
                    if (current["job_id"] != running_receipt["job_id"]
                            or (current["status"] == "running"
                                and current != running_receipt)):
                        _persist_indeterminate_or_retain()
                        return _indeterminate_response()
                    if current["status"] == "done":
                        if current != done_receipt:
                            _persist_indeterminate_or_retain()
                            return _indeterminate_response()
                        # Re-confirm even an exact visible terminal across the parent-sync failure
                        # window before it can clear the scope or disagree with durable replay.
                        _write_scope_action_receipt(
                            _reports_dir, scope_type, scope_id, current)
                        _write_scope_action_fence(
                            _reports_dir, scope_type, scope_id, action_id, "clear")
                        srv.jobs.mark_consumable(running_receipt["job_id"])
                        return public_result
                    if current["status"] != "running":
                        if current["status"] in {"indeterminate", "abandoned"}:
                            srv.jobs.mark_consumable(running_receipt["job_id"])
                        return _indeterminate_response()
                    if not usage_recorded:
                        # A provider call was made and its cost could not be attributed to this
                        # action. Publishing any exact terminal here — success OR a clean failure —
                        # would assert a settled outcome for paid work whose spend is not
                        # reconstructible. The honest state is "unknown"; the operator reviews the
                        # ambiguous attempt instead of reading a terminal that hides a charge.
                        _persist_indeterminate_or_retain()
                        return _indeterminate_response()
                    # The durable terminal is committed before JobRegistry may expose a consumable
                    # terminal. Lost inline bodies and one-shot job polls replay from this ledger.
                    try:
                        _write_scope_action_receipt(
                            _reports_dir, scope_type, scope_id, done_receipt)
                    except _ScopeReportStorageConflict:
                        # The replace may be visible even if strict parent sync failed. Publish a
                        # conservative strict tombstone before either lease can be released.
                        _persist_indeterminate_or_retain()
                        raise
                    _write_scope_action_fence(
                        _reports_dir, scope_type, scope_id, action_id, "clear")
                    srv.jobs.mark_consumable(running_receipt["job_id"])
            except _ScopeReportStorageConflict:
                # Keep the strict running claim as a no-rebill fence. Once JobRegistry becomes done,
                # reconciliation turns it into an explicit indeterminate state; its volatile payload
                # is neither returned as authoritative nor consumed as if it were durable.
                #
                # But when compute ITSELF refused to publish because the durable store was swapped or
                # otherwise unavailable, that same unavailability is why the terminal cannot be
                # written here — no external side effect occurred and nothing was billed. Surface the
                # honest storage conflict (restore durable storage and reconcile the same UUID) rather
                # than an indeterminate outcome that would nudge the operator toward abandoning a UUID
                # that never billed. The running claim is still retained for later reconciliation.
                if public_result.get("code") == _SCOPE_STORAGE_ERROR["code"]:
                    return public_result
                return _indeterminate_response()
            return public_result

        def _compute_durable() -> dict[str, Any]:
            try:
                try:
                    result = _compute()
                except Exception:  # noqa: BLE001 - never persist raw provider/internal detail
                    result = {"ok": False, "code": "job_failed", "error_kind": "internal",
                              "error": "background job failed"}
                public_result = ({**result, "action_id": action_id}
                                 if isinstance(result, dict) and result.get("ok") is True
                                 else _scope_action_failure(result, action_id))
                return _persist_terminal(public_result)
            finally:
                assert action_lease is not None
                if lease_release_allowed:
                    assert scope_lease is not None
                    scope_lease.release()
                    action_lease.release()

        def _spawn_failure_terminal() -> dict[str, Any]:
            # Thread creation failed before ``_compute`` could run, so this path must never construct
            # a provider client. It still owns an exact strict terminal for safe replay.
            try:
                return _persist_terminal(_scope_action_failure({}, action_id))
            finally:
                assert action_lease is not None
                if lease_release_allowed:
                    assert scope_lease is not None
                    scope_lease.release()
                    action_lease.release()

        assert (reservation is not None and isinstance(reservation.get("job_id"), str)
                and action_lease is not None and scope_lease is not None)
        response = await srv.jobs.run_as_job(
            _compute_durable, reserved_job_id=reservation["job_id"],
            consume_inline_result=False,
            on_start_failure=_spawn_failure_terminal)
        if response.get("code") == "job_unknown":
            response = _indeterminate_response()
        elif response.get("status") != "running":
            if response.get("action_id") != action_id:
                # A generic JobRegistry fallback is volatile and cannot become an exact paid-action
                # terminal merely because this endpoint can echo the UUID. Reconcile the ledger after
                # the worker released its leases; BaseException or spawn-callback failures become a
                # durable indeterminate action, never a definitive client-side clear.
                try:
                    with _scope_store_lock(_reports_dir):
                        durable = _read_reconciled_action(
                            scope_type, scope_id, action_id)
                except (_ScopeReportActionConflict, _ScopeReportStorageConflict):
                    durable = None
                response = (_action_response(durable) if durable is not None
                            else _indeterminate_response())
            # Inline terminals have no future UI poll. Retire only when ``_persist_terminal`` marked
            # them consumable after strict publication; a failed durable terminal stays inspectable.
            srv.jobs.poll(reservation["job_id"])
        # ``run_as_job`` owns only the generic job vocabulary. Echo the durable endpoint action on
        # every initial response too, especially the running hand-off that has no compute result yet.
        return {**response, "action_id": action_id}

    return router
