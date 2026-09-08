"""The paid scope-report GENERATION protocol, its scope projections and the source-probe cache.

This is the arm doc 25 SR-02 left open after SR-12 moved the store (`serve/scope_report_store.py`)
and SR-02 itself moved the ACTION protocol (`serve/scope_actions.py`): the 548-line
`generate_scope_report_ep` with its five nested closures (`_stamp_scope_action_usage`, `_compute`,
`_inputs_unchanged`, `_persist_terminal`, `_compute_durable`) and the ~210-line source-probe
staleness cache both stayed inside `routers/reports.py::build_router`, which meant every branch of a
crash-recovery state machine over a durable ledger, two OS byte-range leases and a paid provider
call was reachable ONLY by building the ASGI app and driving HTTP. `tests/test_scope_generate.py`
is what this move exists for; it calls the pieces with a stub `srv` and no app at all.

Three kinds of thing live here, in the order the endpoints use them:

* the scope PROJECTIONS (`scope_label` … `scope_source_sizes`) — what a scope covers, what its
  evidence currently looks like, and the bounded/redacted briefs the model is allowed to read.
  `routers/reports.py` keeps the staleness GET and calls these; SR-02 asked for exactly that shape.
* `ScopeSourceProbes` — the staleness cache. It is a CLASS rather than a set of functions because
  the closures captured three mutable `build_router` locals (a lock and two `OrderedDict`s) whose
  lifetime is the app's, not a request's: a per-request cache would re-parse every event log on
  every GET, which is the cost the cache was added to remove.
* `durable_generate_scope_report` — the endpoint body.

The moved bodies are VERBATIM. The mechanical edits are the ones the two earlier extractions
made and wrote down: `srv` threaded explicitly where it was captured, `srv.reports_dir` in place of
the captured `_reports_dir` (`AppState.__init__` assigns it once and nothing in the tree ever
reassigns it), `srv.projects`/`srv.phase` in place of the two other captures, the eight projections
renamed to public spellings because the router calls them, and the cache closures becoming methods.

**Refusals are NOT translated at the boundary.** `durable_generate_scope_report` raises
`HTTPException` from inside the storage and capacity case analysis, exactly as the route body did.
This is the same decision `scope_actions.py` recorded and for the same reason: the ordering of
`except HTTPException: raise` ahead of the two store-conflict handlers in the claim ladder is
load-bearing, and a new exception type re-entering that ladder is a behaviour change dressed as a
refactor. What matters is the line this module does not cross — it builds no router object and
carries no route decorator, and a guard asserts both (which is why neither is spelled out here: the
guard is a negative substring pin, and the name in a comment would satisfy it).

**A patch seam, like both of its predecessors.** The star import below binds the store's names BY
VALUE, so this module is a FIFTH independent patch site for every seam it names — including
`strict_atomic_write_text`, which the publication path here is the only reader of outside the store.
`tests/test_report.py::_STORE_PATCH_MODULE_PATHS` must sweep it or a test that injects a write
failure exercises nothing; `tests/test_scope_actions_service.py` fails if it does not.
"""
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
from fastapi import HTTPException

from looplab.core.atomicio import strict_atomic_write_text
from looplab.core.comparison import (
    canonical_comparison_contract,
    comparison_measurement,
    finite_measurement,
)
from looplab.core.redact import redact_persisted_text
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.replay import fold
from looplab.serve.scope_report import (
    DEFAULT_SCOPE_REPORT_TIME_S,
    DEFAULT_SCOPE_REPORT_TURNS,
    MAX_SCOPE_REPORT_RUNS,
)
from looplab.serve.scope_sources import (
    MAX_SCOPE_CONFIG_BYTES, MAX_SCOPE_TASK_BYTES, MAX_SCOPE_TOTAL_EVENT_BYTES, FrozenScopeSource,
    ScopeSourceCapacityError, ScopeSourceError, capture_scope_source, probe_scope_log_sig,
    scope_event_size)

# Star-imported for the same reason `routers/reports.py` does it: the generation path reads roughly
# forty store names and an explicit list would go stale on the next store change. See the module
# docstring — a star import binds BY VALUE, so this module is its own patch site.
from looplab.serve.scope_report_store import *  # noqa: F401,F403

# The paid ACTION protocol is `serve/scope_actions.py` (doc 25 SR-02). Imported by NAME: these are
# this module's own call sites, and a star import would re-export the store's names a second time
# under a different binding.
from looplab.serve.scope_actions import (
    action_response,
    active_scope_action,
    indeterminate_receipt,
    read_reconciled_action,
)


# --------------------------------------------------------------------------- scope projections
def scope_label_from_data(data: dict[str, Any], scope_type: str, scope_id: str) -> str:
    if scope_type == "project":
        p = next((x for x in data["projects"] if x["id"] == scope_id), None)
        return f"project “{p['name']}”" if p else f"project {scope_id}"
    if scope_type == "supertask":
        s = next((x for x in data["supertasks"] if x["id"] == scope_id), None)
        return f"super-task “{s['name']}”" if s else f"super-task {scope_id}"
    return f"task {scope_id}"


def scope_label(srv, scope_type: str, scope_id: str) -> str:
    return scope_label_from_data(srv.projects.load(), scope_type, scope_id)


def scope_run_ids(srv, scope_type: str, scope_id: str) -> list:
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
        scopeset = {scope_id} | srv.projects.descendants(scope_id)
        return [s["run_id"] for s in summaries if s.get("project_id") in scopeset]
    return []


def scope_context_digest(srv, scope_type: str, scope_id: str, run_ids: list[str]) -> str:
    project_data = srv.projects.load()
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
        "label": scope_label_from_data(project_data, scope_type, scope_id),
        "scope_metadata": scope_metadata,
        "membership": membership,
        "run_labels": {rid: labels.get(rid) for rid in scoped_ids},
    }
    return hashlib.sha256(json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def run_brief(srv, run_id: str, labels: dict, source: FrozenScopeSource) -> dict:
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
            "phase": srv.phase(st, finalize_incomplete=finalize_incomplete),
            "nodes": len(st.nodes),
            "report": st.report if isinstance(st.report, dict) else None,
            "comparison_contract": task_contract,
            # this single bounded receipt is the only cross-run numeric evidence.
            # Scope projection must copy it atomically; phase/source/uncertainty are inseparable.
            "comparison_measurement": measurement}


def scope_drill(srv, frozen_runs: dict, run_id: str, node_id: int) -> str:
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


def scope_sig(srv, run_ids: list) -> list:
    """Reset-safe metadata fingerprint: generation, file identity, nanoseconds, and size."""
    sig: list = []
    for rid in sorted(set(run_ids)):
        try:
            sig.append(probe_scope_log_sig(srv.root, rid))
        except ScopeSourceError:
            sig.append([rid, "", 0, 0, 0, 0, 0])
    return sig


def scope_source_sizes(srv, run_ids: list[str]) -> dict[str, int]:
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


# ------------------------------------------------------------------- the source-probe cache
class ScopeSourceProbes:
    """The staleness cache behind the scope-report GET and the generation fences.

    One instance per app (built in `routers/reports.py::build_router`), because the three pieces of
    state below are exactly the three mutable `build_router` locals the closures used to capture.
    Every method is a verbatim move of the closure of the same name; `self.srv` replaces the capture.
    """

    def __init__(self, srv):
        self.srv = srv
        self._revision_cache_lock = threading.Lock()
        self._revision_cache: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
        self._omission_cache: OrderedDict[tuple, float] = OrderedDict()

    def probe_key(self, run_id: str, log_sig: list) -> tuple:
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
            run_dir = Path(self.srv.root).absolute() / run_id
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

    def probe_receipt(self, run_id: str, log_sig: list) -> tuple[str, tuple | None]:
        """Return a stable persisted digest even when the source itself is currently unprobeable."""
        try:
            key = self.probe_key(run_id, log_sig)
            payload: object = ["observed", key]
        except ScopeSourceError:
            key = None
            payload = ["unavailable", run_id, log_sig]
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), key

    def remember_revision(self, probe_key: tuple, revision: dict[str, Any]) -> None:
        with self._revision_cache_lock:
            self._omission_cache.pop(probe_key, None)
            self._revision_cache[probe_key] = (time.monotonic(), {
                **revision, "log_sig": list(revision["log_sig"]),
            })
            self._revision_cache.move_to_end(probe_key)
            while len(self._revision_cache) > _SCOPE_REVISION_CACHE_MAX:
                self._revision_cache.popitem(last=False)

    def cached_revision(self, probe_key: tuple) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._revision_cache_lock:
            cached = self._revision_cache.get(probe_key)
            if cached is None:
                return None
            captured_at, revision = cached
            if now - captured_at > _SCOPE_REVISION_CACHE_TTL_S:
                self._revision_cache.pop(probe_key, None)
                return None
            self._revision_cache.move_to_end(probe_key)
            return revision

    def remember_omission(self, probe_key: tuple) -> None:
        with self._revision_cache_lock:
            self._omission_cache[probe_key] = time.monotonic()
            self._omission_cache.move_to_end(probe_key)
            while len(self._omission_cache) > _SCOPE_REVISION_CACHE_MAX:
                self._omission_cache.popitem(last=False)

    def cached_omission(self, probe_key: tuple) -> bool:
        now = time.monotonic()
        with self._revision_cache_lock:
            captured_at = self._omission_cache.get(probe_key)
            if captured_at is None:
                return False
            if now - captured_at > _SCOPE_REVISION_CACHE_TTL_S:
                self._omission_cache.pop(probe_key, None)
                return False
            self._omission_cache.move_to_end(probe_key)
            return True

    def revision_is_current(
            self, run_id: str, log_sig: list, expected: dict[str, Any],
            remaining_bytes: int) -> tuple[bool, int]:
        """Validate a persisted revision without reparsing an unchanged log on every GET."""
        if not _complete_source_revision(expected):
            return False, 0
        expected_bytes = expected["event_bytes"]
        if expected_bytes > remaining_bytes:
            return False, 0
        before = self.probe_key(run_id, log_sig)
        cached = self.cached_revision(before)
        if cached is not None:
            return cached == expected, expected_bytes
        if self.cached_omission(before):
            return False, expected_bytes
        try:
            source = capture_scope_source(
                self.srv.root, run_id, event_budget_bytes=max(1, remaining_bytes))
        except ScopeSourceError:
            # Negative-cache an unchanged corrupt/inaccessible snapshot. It is already stale, and
            # reparsing the same bounded-but-large event log on every GET cannot improve that fact.
            self.remember_omission(before)
            return False, expected_bytes
        after = self.probe_key(run_id, source.revision["log_sig"])
        if before != after or source.revision["log_sig"] != log_sig:
            return False, source.event_bytes
        # ordinary rewrites invalidate dev/ino/ctime/size/mtime immediately. The bounded
        # TTL retains a periodic full-byte check for exotic filesystems that can preserve all of those
        # fields, while stable GETs reuse one parsed revision instead of rebuilding every Event object.
        self.remember_revision(after, source.revision)
        return source.revision == expected, source.event_bytes

    def omission_is_current(
            self, run_id: str, log_sig: list, expected_probe: str,
            remaining_bytes: int) -> tuple[bool, int]:
        """Keep an omitted source explicit, and notice when it becomes model-visible evidence."""
        event_bytes = int(log_sig[5]) if _valid_scope_sig_row(log_sig) and len(log_sig) == 7 else 0
        if event_bytes > remaining_bytes:
            return False, 0
        observed_probe, probe_key = self.probe_receipt(run_id, log_sig)
        if observed_probe != expected_probe:
            return False, event_bytes
        if probe_key is not None and self.cached_revision(probe_key) is not None:
            return False, event_bytes
        if probe_key is None:
            return True, event_bytes
        try:
            source = capture_scope_source(
                self.srv.root, run_id, event_budget_bytes=max(1, remaining_bytes))
        except ScopeSourceError:
            after_probe, after_key = self.probe_receipt(run_id, log_sig)
            if after_probe != expected_probe:
                return False, event_bytes
            if after_key is not None:
                self.remember_omission(after_key)
            return True, event_bytes
        _after_probe, after_key = self.probe_receipt(
            run_id, source.revision["log_sig"])
        if after_key is not None:
            # The report is stale because a previously omitted source is now readable. Retain the
            # exact successful revision so subsequent GET observers do not repeatedly parse the same
            # unchanged event prefix while the operator decides whether to regenerate.
            self.remember_revision(after_key, source.revision)
        # a negative cache may skip work only when the answer is already stale. An
        # omitted receipt needs a current failed-open observation before it can authorize
        # ``stale:false``: accessibility is not part of the cheap stat key, so a transient lock can
        # clear without changing that key. A formerly omitted run is therefore always new evidence.
        return False, event_bytes


# ----------------------------------------------------------- the paid generation protocol
async def durable_generate_scope_report(
        srv, probes: "ScopeSourceProbes", scope_type: str, scope_id: str,
        idempotency_key: str | None) -> dict:
    """Claim, compute and durably settle ONE paid scope-report generation.

    ``probes`` is the caller's long-lived `ScopeSourceProbes`: the cache is per-app, not per-request,
    and a fresh one per call would re-parse every event log on every generation. Raises
    ``HTTPException`` exactly where the route body did — see this module's docstring for why the
    refusals were not translated at the boundary.
    """
    if scope_type not in _SCOPE_TYPES:
        raise HTTPException(400, "bad scope type")
    action_id = _scope_action_id(idempotency_key)
    if action_id is None:
        raise HTTPException(428 if idempotency_key is None else 400, _SCOPE_ACTION_REQUIRED)
    # OFF the event loop, ahead of the job hand-off. `_scope_store_lock` is a GLOBAL thread lock
    # plus an interprocess file lock, `read_reconciled_action` does strict lease/fence file I/O
    # and re-publication, `scope_run_ids` re-runs the whole runs-list fold, `scope_sig` and
    # `scope_source_sizes` stat every run, and `scope_context_digest` reloads the project store.
    # Run inline on this `async def` handler's loop, contention on that single store lock — one
    # other generation publishing — stalled the ENTIRE event loop, not just this request.
    # Returns `(early_response, preflight)`; exactly one is non-None. HTTPExceptions raised inside
    # propagate out of the worker unchanged.
    def _preflight():
        try:
            with _scope_store_lock(srv.reports_dir):
                existing_action = read_reconciled_action(
                    srv, scope_type, scope_id, action_id)
        except _ScopeReportActionConflict as exc:
            raise HTTPException(409, _SCOPE_ACTION_CONFLICT) from exc
        except _ScopeReportStorageConflict as exc:
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
        if existing_action is not None:
            # Durable replay happens before current-scope preflight: an old action remains observable
            # after its inputs change, its report is superseded, or its volatile job receipt is consumed.
            return action_response(existing_action), None
        run_ids = sorted(set(scope_run_ids(srv, scope_type, scope_id)))
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
            requested_source_sizes = scope_source_sizes(srv, run_ids)
        except ScopeSourceCapacityError as exc:
            raise HTTPException(413, _SCOPE_SOURCE_TOO_LARGE) from exc
        requested_scope_ids = list(run_ids)
        requested_scope_sig = scope_sig(srv, requested_scope_ids)
        requested_context_digest = scope_context_digest(
            srv, scope_type, scope_id, requested_scope_ids)
        requested_probe_receipts = {
            run_id: probes.probe_receipt(run_id, row)[0]
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
            with _scope_store_lock(srv.reports_dir):
                _read_or_migrate_scope_record(srv.reports_dir, scope_type, scope_id)
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
            with _scope_store_lock(srv.reports_dir):
                current = _read_scope_action_receipt(
                    srv.reports_dir, scope_type, scope_id, action_id)
                # Same exact-identity rule `_persist_terminal` uses: a worker may only edit the
                # running claim it wrote itself, never a row someone else has since moved.
                if current is None or current != running_receipt:
                    return False
                _write_scope_action_receipt(
                    srv.reports_dir, scope_type, scope_id, updated)
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
        current_ids = sorted(set(scope_run_ids(srv, scope_type, scope_id)))
        if (current_ids != frozen_scope_ids
                or scope_sig(srv, current_ids) != requested_scope_sig
                or scope_context_digest(srv, scope_type, scope_id, current_ids)
                != requested_context_digest):
            return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
        try:
            frozen_source_sizes = scope_source_sizes(srv, frozen_scope_ids)
        except ScopeSourceCapacityError:
            return {"ok": False, **_SCOPE_SOURCE_TOO_LARGE}
        if frozen_source_sizes != requested_source_sizes:
            return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
        frozen_scope_sig = requested_scope_sig
        frozen_sig_by_id = {row[0]: row for row in frozen_scope_sig}
        frozen_context_digest = requested_context_digest
        labels = srv.projects.load().get("labels", {})
        briefs = []
        frozen_runs: dict[str, FrozenScopeSource] = {}
        frozen_probe_keys: dict[str, tuple] = {}
        frozen_probe_receipts: dict[str, str] = {}
        consumed_event_bytes = 0
        for rid in frozen_scope_ids:
            expected_bytes = frozen_source_sizes.get(rid, 0)
            before_probe, before_key = probes.probe_receipt(rid, frozen_sig_by_id[rid])
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
                after_probe, after_key = probes.probe_receipt(
                    rid, source.revision["log_sig"])
                if (source.event_bytes != expected_bytes or before_probe != after_probe
                        or before_key is None or after_key is None
                        or source.revision["log_sig"] != frozen_sig_by_id[rid]):
                    return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                briefs.append(run_brief(srv, rid, labels, source))
                frozen_runs[rid] = source
                frozen_probe_keys[rid] = after_key
                probes.remember_revision(after_key, source.revision)
            except ScopeSourceCapacityError:
                return {"ok": False, **_SCOPE_SOURCE_TOO_LARGE}
            except ScopeSourceError:
                if before_key is not None:
                    probes.remember_omission(before_key)
                continue
            finally:
                consumed_event_bytes += expected_bytes
        scope = {
            "type": scope_type,
            "id": scope_id,
            "label": scope_label(srv, scope_type, scope_id),
            # preserve honest scope coverage even when a corrupt/unreadable run cannot
            # contribute a brief. The model sees only frozen briefs; the receipt counts the omission.
            "source_run_count": len(frozen_scope_ids),
        }
        brief_ids = [brief["run_id"] for brief in briefs]
        source_revisions = [frozen_runs[rid].revision for rid in brief_ids]
        omitted = sorted(set(frozen_scope_ids) - set(brief_ids))

        def _inputs_unchanged() -> bool:
            current_ids = sorted(set(scope_run_ids(srv, scope_type, scope_id)))
            current_sig = scope_sig(srv, current_ids)
            if current_ids != frozen_scope_ids or current_sig != frozen_scope_sig:
                return False
            if (scope_context_digest(srv, scope_type, scope_id, current_ids)
                    != frozen_context_digest):
                return False
            try:
                current_sizes = scope_source_sizes(srv, current_ids)
                if current_sizes != frozen_source_sizes:
                    return False
                current_sig_by_id = {row[0]: row for row in current_sig}
                for rid in frozen_scope_ids:
                    current_probe, _current_key = probes.probe_receipt(
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
            drill = lambda run_id, node_id: scope_drill(  # noqa: E731
                srv, frozen_runs, run_id, node_id)
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
            with _scope_store_lock(srv.reports_dir):
                # Narrow the optimistic-check window at the actual publication boundary.
                if not _inputs_unchanged():
                    return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                for rid in frozen_runs:
                    probes.remember_revision(frozen_probe_keys[rid], frozen_runs[rid].revision)
                # Revalidate the lexical store and re-derive the destination immediately before
                # publication. A directory/file swapped during the slow model call is refused.
                _validated_reports_dir(srv.reports_dir, create=True)
                _read_or_migrate_scope_record(srv.reports_dir, scope_type, scope_id)
                dst = _scope_report_path(srv.reports_dir, scope_type, scope_id)
                # The canonical report is the sole paid payload. Confirm its contents and first
                # directory publication before the action ledger may claim terminal success.
                strict_atomic_write_text(dst, _serialize_scope_record(rec))
                dst = _scope_report_path(srv.reports_dir, scope_type, scope_id)
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
        with _scope_store_lock(srv.reports_dir):
            existing_action = read_reconciled_action(
                srv, scope_type, scope_id, action_id)
            if existing_action is not None:
                return action_response(existing_action)
            active = active_scope_action(srv, scope_type, scope_id)
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
                srv.reports_dir, scope_type, scope_id, action_id)
            if action_lease is None:
                raise HTTPException(409, {
                    **_SCOPE_ACTION_ACTIVE,
                    "action_id": action_id,
                })
            scope_lease = _acquire_scope_action_scope_lease(
                srv.reports_dir, scope_type, scope_id)
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
                srv.reports_dir, scope_type, scope_id, running_receipt)
            # The root-level per-scope fence is strict before the worker starts. It survives a
            # regular ``reports/`` directory replacement and blocks every different UUID.
            _write_scope_action_fence(
                srv.reports_dir, scope_type, scope_id, action_id, "active")
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
                    srv.reports_dir, scope_type, scope_id,
                    indeterminate_receipt(running_receipt))
            except _ScopeReportStorageConflict:
                lease_release_allowed = False
                assert action_lease is not None and scope_lease is not None
                _retain_scope_action_leases(
                    srv.reports_dir, action_id, action_lease, scope_lease)
                raise
            srv.jobs.mark_consumable(running_receipt["job_id"])

        try:
            with _scope_store_lock(srv.reports_dir):
                current = _read_scope_action_receipt(
                    srv.reports_dir, scope_type, scope_id, action_id)
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
                        srv.reports_dir, scope_type, scope_id, current)
                    _write_scope_action_fence(
                        srv.reports_dir, scope_type, scope_id, action_id, "clear")
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
                        srv.reports_dir, scope_type, scope_id, done_receipt)
                except _ScopeReportStorageConflict:
                    # The replace may be visible even if strict parent sync failed. Publish a
                    # conservative strict tombstone before either lease can be released.
                    _persist_indeterminate_or_retain()
                    raise
                _write_scope_action_fence(
                    srv.reports_dir, scope_type, scope_id, action_id, "clear")
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
                with _scope_store_lock(srv.reports_dir):
                    durable = read_reconciled_action(
                        srv, scope_type, scope_id, action_id)
            except (_ScopeReportActionConflict, _ScopeReportStorageConflict):
                durable = None
            response = (action_response(durable) if durable is not None
                        else _indeterminate_response())
        # Inline terminals have no future UI poll. Retire only when ``_persist_terminal`` marked
        # them consumable after strict publication; a failed durable terminal stays inspectable.
        srv.jobs.poll(reservation["job_id"])
    # ``run_as_job`` owns only the generic job vocabulary. Echo the durable endpoint action on
    # every initial response too, especially the running hand-off that has no compute result yet.
    return {**response, "action_id": action_id}
