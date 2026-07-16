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
import re
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from looplab.core.atomicio import atomic_write_text
from looplab.core.comparison import (
    canonical_comparison_contract,
    comparison_measurement,
    finite_measurement,
)
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.eventstore import EventStoreLockError, _interprocess_lock
from looplab.events.replay import fold
from looplab.serve.run_commands import run_generation_token
from looplab.serve.scope_report import (
    DEFAULT_SCOPE_REPORT_TIME_S,
    DEFAULT_SCOPE_REPORT_TURNS,
    MAX_SCOPE_REPORT_RUNS,
)
from looplab.trust.redact import redact_persisted_text


_SCOPE_TYPES = frozenset({"project", "task", "supertask"})
_SCOPE_STORAGE_ERROR = {
    "code": "scope_report_storage_conflict",
    "message": "The stored scope report has no matching scope identity.",
    "error": "Scope report storage is unavailable or belongs to another scope.",
    "remediation": "Quarantine the conflicting report file before generating this scope again.",
}
_SCOPE_INPUTS_CHANGED = {
    "code": "scope_report_inputs_changed",
    "error_kind": "conflict",
    "error": "Scope runs changed while the report was being generated. The previous report was kept.",
    "remediation": "Retry generation from the current scope snapshot.",
}
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVER_VERDICT_AUTHORITY = "server-derived-v1"
_SCOPE_STORE_THREAD_LOCK = threading.Lock()


class _ScopeReportStorageConflict(RuntimeError):
    """An existing scope-report path cannot be proven to belong to the requested scope."""


def _scope_identity(scope_type: str, scope_id: str) -> dict[str, str]:
    return {"type": str(scope_type), "id": str(scope_id)}


def _is_link_or_reparse(entry: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(entry.st_mode) or bool(reparse_flag and attributes & reparse_flag)


def _validated_reports_dir(reports_dir: Path, *, create: bool = False) -> Path:
    """Return the lexical report directory only when it remains inside its canonical parent.

    ``Path.resolve()`` must never establish the authority boundary here: resolving a hostile
    ``reports`` symlink/junction would bless its external target as the store. The application root
    is canonicalized at startup, so its direct lexical child is the only valid report directory.
    """
    base = Path(os.path.abspath(os.fspath(reports_dir)))
    try:
        if base.parent.resolve(strict=True) != base.parent:
            raise _ScopeReportStorageConflict("scope report parent is not canonical")
        if create:
            base.mkdir(exist_ok=True)
        entry = base.lstat()
    except FileNotFoundError:
        if create:
            raise _ScopeReportStorageConflict("scope report directory disappeared")
        return base
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict("scope report directory could not be validated") from exc
    if (not stat.S_ISDIR(entry.st_mode) or _is_link_or_reparse(entry)):
        raise _ScopeReportStorageConflict("scope report directory is not a trusted directory")
    try:
        if base.resolve(strict=True) != base:
            raise _ScopeReportStorageConflict("scope report directory escaped its parent")
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict("scope report directory could not be resolved") from exc
    return base


def _confined_report_path(reports_dir: Path, filename: str) -> Path:
    base = _validated_reports_dir(reports_dir)
    candidate = base / filename
    try:
        entry = candidate.lstat()
    except FileNotFoundError:
        return candidate
    except OSError as exc:
        raise _ScopeReportStorageConflict("scope report path could not be inspected") from exc
    if not stat.S_ISREG(entry.st_mode) or _is_link_or_reparse(entry):
        raise _ScopeReportStorageConflict("scope report path is not a trusted regular file")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _ScopeReportStorageConflict("scope report path escaped its store")
    except _ScopeReportStorageConflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _ScopeReportStorageConflict("scope report path could not be resolved") from exc
    return candidate


@contextmanager
def _scope_store_lock(reports_dir: Path):
    """Serialize migration/publication outside the replaceable report directory itself."""
    base = _validated_reports_dir(reports_dir)
    lock_path = base.parent / ".scope-reports.lock"
    try:
        entry = lock_path.lstat()
    except FileNotFoundError:
        entry = None
    except OSError as exc:
        raise _ScopeReportStorageConflict("scope report lock could not be inspected") from exc
    if entry is not None and (not stat.S_ISREG(entry.st_mode) or _is_link_or_reparse(entry)):
        raise _ScopeReportStorageConflict("scope report lock is not a trusted regular file")
    try:
        with _SCOPE_STORE_THREAD_LOCK, _interprocess_lock(lock_path, required=True):
            _validated_reports_dir(reports_dir)
            yield
    except EventStoreLockError as exc:
        raise _ScopeReportStorageConflict("scope report lock is unavailable") from exc


def _scope_report_path(reports_dir: Path, scope_type: str, scope_id: str) -> Path:
    """Map one exact scope identity to a confined, collision-resistant report path.

    The readable prefix is diagnostic only. The full SHA-256 suffix owns uniqueness, so lossy
    sanitization and truncation can never alias two different scope ids. Resolving the candidate
    also rejects a pre-existing symlink that would redirect reads outside (or elsewhere inside)
    the report store.
    """
    identity_bytes = json.dumps(
        _scope_identity(scope_type, scope_id), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity_bytes).hexdigest()
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", f"{scope_type}-{scope_id}")[:48]
    return _confined_report_path(
        reports_dir, f"{readable or 'scope'}-{digest}.json")


def _legacy_scope_report_path(reports_dir: Path, scope_type: str, scope_id: str) -> Path:
    """The pre-hash filename, used only for exact-identity upgrade reads."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{scope_type}-{scope_id}")[:120]
    return _confined_report_path(reports_dir, safe + ".json")


def _valid_scope_sig_row(row: object) -> bool:
    """Accept legacy second-resolution rows for migration and the reset-safe v2 shape."""
    if not isinstance(row, list):
        return False
    if len(row) == 3:
        return (isinstance(row[0], str) and type(row[1]) is int and row[1] >= 0
                and type(row[2]) is int and row[2] >= 0)
    return (
        len(row) == 7
        and isinstance(row[0], str)
        and isinstance(row[1], str)
        and (not row[1] or _RUN_GENERATION_RE.fullmatch(row[1]) is not None)
        and all(type(value) is int and value >= 0 for value in row[2:])
    )


def _valid_source_revision(revision: object) -> bool:
    if not isinstance(revision, dict):
        return False
    generation = revision.get("generation")
    digest = revision.get("tail_digest")
    log_sig = revision.get("log_sig")
    base_valid = (
        isinstance(revision.get("run_id"), str)
        and isinstance(generation, str)
        and _RUN_GENERATION_RE.fullmatch(generation) is not None
        and type(revision.get("tail_seq")) is int and revision["tail_seq"] >= -1
        and type(revision.get("event_count")) is int and revision["event_count"] >= 1
        and isinstance(digest, str) and _RUN_GENERATION_RE.fullmatch(digest) is not None
        and _valid_scope_sig_row(log_sig)
        and len(log_sig) == 7
        and log_sig[0] == revision["run_id"]
        and log_sig[1] == generation
    )
    if not base_valid:
        return False
    for field in ("events_digest", "task_snapshot_digest", "config_snapshot_digest"):
        value = revision.get(field)
        if value is not None and (
                not isinstance(value, str) or _RUN_GENERATION_RE.fullmatch(value) is None):
            return False
    return True


def _record_payload_matches_scope(rec: object, scope_type: str, scope_id: str) -> bool:
    """Validate the historical report payload and its exact embedded display scope."""
    if not isinstance(rec, dict):
        return False
    expected = _scope_identity(scope_type, scope_id)
    scope = rec.get("scope")
    run_ids = rec.get("run_ids")
    sig = rec.get("sig")
    source_revisions = rec.get("source_revisions")
    return (
        isinstance(scope, dict)
        and scope.get("type") == expected["type"]
        and scope.get("id") == expected["id"]
        and isinstance(scope.get("label"), str)
        and type(rec.get("generated_at")) is int
        and rec["generated_at"] >= 0
        and isinstance(run_ids, list)
        and all(isinstance(run_id, str) for run_id in run_ids)
        and isinstance(sig, list)
        and all(_valid_scope_sig_row(row) for row in sig)
        and (
            source_revisions is None
            or (
                isinstance(source_revisions, list)
                and all(_valid_source_revision(row) for row in source_revisions)
                and [row["run_id"] for row in source_revisions] == run_ids
                and [row["log_sig"] for row in source_revisions] == sig
            )
        )
        and isinstance(rec.get("content"), dict)
    )


def _record_matches_scope(rec: object, scope_type: str, scope_id: str) -> bool:
    """Require both immutable storage identity and display scope to name the requested scope."""
    return (
        isinstance(rec, dict)
        and rec.get("scope_identity") == _scope_identity(scope_type, scope_id)
        and _record_payload_matches_scope(rec, scope_type, scope_id)
    )


def _read_json_record(path: Path) -> dict[str, Any] | None:
    """Read one already-confined regular file; missing is distinct from corrupt."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise _ScopeReportStorageConflict("scope report could not be read") from exc
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _ScopeReportStorageConflict("scope report is not valid JSON") from exc
    if not isinstance(rec, dict):
        raise _ScopeReportStorageConflict("scope report is not a JSON object")
    return rec


def _read_scope_record(path: Path, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    """Read only a record proven to own this exact path; missing is distinct from corrupt."""
    rec = _read_json_record(path)
    if rec is None:
        return None
    if not _record_matches_scope(rec, scope_type, scope_id):
        raise _ScopeReportStorageConflict("scope report identity does not match its path")
    return rec


def _read_or_migrate_scope_record(
        reports_dir: Path, scope_type: str, scope_id: str) -> dict[str, Any] | None:
    """Read the canonical report or safely copy an exact pre-hash record into canonical storage.

    The lossy legacy filename is never accepted as identity. Its embedded scope must exactly match
    the request, and a legacy ``scope_identity`` (if present) must also agree. The old file is kept:
    deleting it could destroy the only evidence for another id that collided on the old filename.
    """
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    current = _read_scope_record(canonical, scope_type, scope_id)
    if current is not None:
        return current
    legacy = _legacy_scope_report_path(reports_dir, scope_type, scope_id)
    old = _read_json_record(legacy)
    if old is None:
        return None
    expected = _scope_identity(scope_type, scope_id)
    legacy_identity = old.get("scope_identity")
    if (legacy_identity not in (None, expected)
            or not _record_payload_matches_scope(old, scope_type, scope_id)):
        raise _ScopeReportStorageConflict("legacy scope report identity is ambiguous")
    migrated = {**old, "scope_identity": expected}
    _validated_reports_dir(reports_dir, create=True)
    # Re-derive and re-read under the caller's store lock: another process may have migrated first.
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    current = _read_scope_record(canonical, scope_type, scope_id)
    if current is not None:
        return current
    atomic_write_text(canonical, json.dumps(migrated, indent=2))
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    return _read_scope_record(canonical, scope_type, scope_id)


def _public_scope_record(rec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Never present a pre-authority model verdict as a server-derived comparison outcome."""
    content = dict(rec.get("content") or {})
    if content.get("verdict_authority") == _SERVER_VERDICT_AUTHORITY:
        return rec, False
    content["verdict"] = "No authoritative verdict is available for this legacy report; regenerate it."
    content["verdict_authority"] = "legacy-unavailable"
    content["requires_regeneration"] = True
    # CODEX AGENT: the old model-authored verdict is deliberately not copied to another public field;
    # renaming it would still let clients accidentally display an invented winner as trusted prose.
    return {**rec, "content": content}, True


def _prior_learnings_index(reports_dir: Path) -> str:
    """Compact index of stored cross-run reports (scope label + headline + a couple of next-
    directions) so the genesis boss can bootstrap a new run informed by prior portfolios."""
    try:
        base = _validated_reports_dir(reports_dir)
    except _ScopeReportStorageConflict:
        return ""
    if not base.exists():
        return ""
    records: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for discovered in sorted(base.glob("*.json")):
        try:
            p = _confined_report_path(base, discovered.name)
            rec = _read_json_record(p)
            if rec is None:
                continue
            identity = rec.get("scope_identity") if isinstance(rec, dict) else None
            scope_type = identity.get("type") if isinstance(identity, dict) else None
            scope_id = identity.get("id") if isinstance(identity, dict) else None
            priority = 1
            if scope_type in _SCOPE_TYPES and isinstance(scope_id, str):
                valid = (_record_matches_scope(rec, scope_type, scope_id)
                         and _scope_report_path(base, scope_type, scope_id) == p)
            else:
                scope = rec.get("scope") if isinstance(rec, dict) else None
                scope_type = scope.get("type") if isinstance(scope, dict) else None
                scope_id = scope.get("id") if isinstance(scope, dict) else None
                priority = 0
                valid = (
                    scope_type in _SCOPE_TYPES
                    and isinstance(scope_id, str)
                    and _record_payload_matches_scope(rec, scope_type, scope_id)
                    and _legacy_scope_report_path(base, scope_type, scope_id) == p
                )
            if not valid:
                continue
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError,
                _ScopeReportStorageConflict):
            continue
        key = (scope_type, scope_id)
        if key not in records or priority > records[key][0]:
            records[key] = (priority, rec)
    lines = []
    for _priority, rec in records.values():
        c = rec.get("content") or {}
        lbl = (rec.get("scope") or {}).get("label") or "scope report"
        line = f"- {lbl}: {c.get('headline') or ''}"
        nd = c.get("next_directions") or []
        if nd:
            line += " | next: " + "; ".join(str(x) for x in nd[:2])
        lines.append(line)
    return "\n".join(lines[:20])


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, _phase = srv.run_dir, srv.phase
    projects = srv.projects
    _reports_dir = srv.reports_dir

    def _scope_label(scope_type: str, scope_id: str) -> str:
        data = projects.load()
        if scope_type == "project":
            p = next((x for x in data["projects"] if x["id"] == scope_id), None)
            return f"project “{p['name']}”" if p else f"project {scope_id}"
        if scope_type == "supertask":
            s = next((x for x in data["supertasks"] if x["id"] == scope_id), None)
            return f"super-task “{s['name']}”" if s else f"super-task {scope_id}"
        return f"task {scope_id}"

    def _scope_run_ids(scope_type: str, scope_id: str) -> list:
        """The runs a scope covers. project = the folder AND everything nested under it; task = same
        task_id; supertask = assigned to that super-task."""
        summaries = srv.list_runs_fn()
        if scope_type == "task":
            return [s["run_id"] for s in summaries if s.get("task_id") == scope_id]
        if scope_type == "supertask":
            return [s["run_id"] for s in summaries if s.get("supertask_id") == scope_id]
        if scope_type == "project":
            scopeset = {scope_id} | projects.descendants(scope_id)
            return [s["run_id"] for s in summaries if s.get("project_id") in scopeset]
        return []

    def _log_sig(run_id: str, rd: Path, generation: str | None = None) -> list:
        """Cheap reset-safe log identity used by GET staleness checks."""
        try:
            resolved_generation = generation if generation is not None else srv.commands.run_generation(rd)
            stt = (rd / "events.jsonl").stat()
            return [
                run_id, resolved_generation,
                int(stt.st_dev), int(stt.st_ino), int(stt.st_ctime_ns),
                int(stt.st_size), int(stt.st_mtime_ns),
            ]
        except (OSError, RuntimeError):
            return [run_id, "", 0, 0, 0, 0, 0]

    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except FileNotFoundError:
            return hashlib.sha256(b"<missing>").hexdigest()
        return digest.hexdigest()

    def _scope_context_digest(scope_type: str, scope_id: str, run_ids: list[str]) -> str:
        project_data = projects.load()
        labels = project_data.get("labels", {}) if isinstance(project_data, dict) else {}
        context = {
            "scope": _scope_identity(scope_type, scope_id),
            "label": _scope_label(scope_type, scope_id),
            "run_labels": {rid: labels.get(rid) for rid in sorted(set(run_ids))},
            # Project rename/reparent and super-task assignment can change report meaning even when
            # the resulting membership happens to contain the same run ids.
            "projects_revision": project_data,
        }
        return hashlib.sha256(json.dumps(
            context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()

    def _capture_source(run_id: str, rd: Path | None = None) -> tuple[Path, list, dict]:
        """Read one exact event prefix and bind it to generation, tail, and file identity."""
        rd = rd if rd is not None else _run_dir(run_id)
        events = srv.events(rd)
        generation = run_generation_token(events)
        if not events or _RUN_GENERATION_RE.fullmatch(generation) is None:
            raise ValueError("run has no durable generation")
        tail_payload = json.dumps(
            events[-1].model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        log_sig = _log_sig(run_id, rd, generation)
        if log_sig[1] != generation:
            raise OSError("event log disappeared while freezing scope evidence")
        events_hash = hashlib.sha256()
        for event in events:
            events_hash.update(json.dumps(
                event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8"))
            events_hash.update(b"\n")
        revision = {
            "run_id": run_id,
            "generation": generation,
            "tail_seq": int(events[-1].seq),
            "event_count": len(events),
            "tail_digest": hashlib.sha256(tail_payload).hexdigest(),
            "log_sig": log_sig,
            "events_digest": events_hash.hexdigest(),
            "task_snapshot_digest": _file_digest(rd / "task.snapshot.json"),
            "config_snapshot_digest": _file_digest(rd / "config.snapshot.json"),
        }
        return rd, events, revision

    def _run_brief(run_id: str, labels: dict, rd: Path, events: list) -> dict:
        st = fold(events)
        finalize_incomplete = (
            incomplete_finalize_scope(events) is not None or st.finalization_pending())
        best = st.best()
        cfg = {}
        snap = rd / "config.snapshot.json"
        if snap.exists():
            try:
                cfg = json.loads(snap.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}
        task_contract = None
        task_snap = rd / "task.snapshot.json"
        try:
            # Task snapshots are operator-authored and can be arbitrarily large. Comparison metadata
            # is useful only when the whole bounded JSON document is readable and explicitly valid.
            if task_snap.stat().st_size <= 1_000_000:
                task_doc = json.loads(task_snap.read_text(encoding="utf-8"))
                if isinstance(task_doc, dict):
                    task_contract = canonical_comparison_contract(
                        task_doc.get("comparison_contract"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            task_contract = None
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
                # CODEX AGENT: this single bounded receipt is the only cross-run numeric evidence.
                # Scope projection must copy it atomically; phase/source/uncertainty are inseparable.
                "comparison_measurement": measurement}

    def _scope_drill(frozen_runs: dict, run_id: str, node_id: int) -> str:
        """Project one frozen node without code, files, stdout/stderr, or raw tool output."""
        frozen = frozen_runs.get(run_id)
        if frozen is None:
            return "(drill unavailable)"
        try:
            _rd, events, revision = _capture_source(run_id, frozen["run_dir"])
            if revision != frozen["revision"]:
                return "(drill unavailable: frozen run changed)"
            st = fold(events)
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
                sig.append(_log_sig(rid, _run_dir(rid)))
            except Exception:  # noqa: BLE001 - a vanished run is a stable stale marker
                sig.append([rid, "", 0, 0, 0, 0, 0])
        return sig

    @router.get("/api/scope-report/{scope_type}/{scope_id}")
    def get_scope_report(scope_type: str, scope_id: str):
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
        cur_ids = _scope_run_ids(scope_type, scope_id)
        try:
            with _scope_store_lock(_reports_dir):
                rec = _read_or_migrate_scope_record(
                    _reports_dir, scope_type, scope_id)
        except _ScopeReportStorageConflict as exc:
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc
        if rec is None:
            return {"exists": False, "run_count": len(cur_ids),
                    "label": _scope_label(scope_type, scope_id)}
        added = sorted(set(cur_ids) - set(rec.get("run_ids", [])))
        rec, legacy_authority = _public_scope_record(rec)
        stale = legacy_authority or _scope_sig(cur_ids) != rec.get("sig")
        source_revisions = rec.get("source_revisions")
        if not stale and isinstance(source_revisions, list):
            try:
                current_revisions = [
                    _capture_source(run_id)[2] for run_id in rec.get("run_ids", [])]
                # A pre-v2 revision lacks task/config/full-prefix digests. Treat it as stale rather
                # than claiming that a newly model-visible input stayed unchanged.
                stale = current_revisions != source_revisions
            except Exception:  # noqa: BLE001 - uncertain input identity is stale, never current
                stale = True
        return {**rec, "exists": True, "stale": stale,
                "current_run_count": len(cur_ids), "added": added}

    @router.post("/api/scope-report/{scope_type}/{scope_id}/generate")
    async def generate_scope_report_ep(scope_type: str, scope_id: str):
        """Generate (or regenerate) the cross-run report for a scope. On-demand only — the agent reads
        a bounded/redacted projection of at most ``MAX_SCOPE_REPORT_RUNS`` runs and may request a
        bounded node drill. Degrades to a metrics rollup offline. Runs as a BACKGROUND JOB: reading +
        synthesizing over many runs can outlast a UI proxy's gateway timeout, so a slow synthesis hands
        back a job_id the UI polls (a fast/offline one still returns inline within the wait — no 504)."""
        if scope_type not in _SCOPE_TYPES:
            raise HTTPException(400, "bad scope type")
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
            with _scope_store_lock(_reports_dir):
                _read_or_migrate_scope_record(_reports_dir, scope_type, scope_id)
        except _ScopeReportStorageConflict as exc:
            # Never enqueue paid work that cannot safely publish its result afterward.
            raise HTTPException(409, _SCOPE_STORAGE_ERROR) from exc

        def _compute() -> dict:
            frozen_scope_ids = sorted(set(_scope_run_ids(scope_type, scope_id)))
            if not frozen_scope_ids:
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            if len(frozen_scope_ids) > MAX_SCOPE_REPORT_RUNS:
                return {
                    "ok": False,
                    "code": "scope_report_too_large",
                    "error_kind": "capacity",
                    "error": "The scope grew beyond the bounded report input limit.",
                    "run_count": len(frozen_scope_ids),
                    "max_runs": MAX_SCOPE_REPORT_RUNS,
                }
            frozen_scope_sig = _scope_sig(frozen_scope_ids)
            frozen_context_digest = _scope_context_digest(
                scope_type, scope_id, frozen_scope_ids)
            labels = projects.load().get("labels", {})
            briefs = []
            frozen_runs: dict[str, dict] = {}
            for rid in frozen_scope_ids:
                try:
                    rd, events, revision = _capture_source(rid)
                    briefs.append(_run_brief(rid, labels, rd, events))
                    frozen_runs[rid] = {"run_dir": rd, "revision": revision}
                except Exception:  # noqa: BLE001 - a half-written run shouldn't block the report
                    continue
            scope = {
                "type": scope_type,
                "id": scope_id,
                "label": _scope_label(scope_type, scope_id),
                # CODEX AGENT: preserve honest scope coverage even when a corrupt/unreadable run cannot
                # contribute a brief. The model sees only frozen briefs; the receipt counts the omission.
                "source_run_count": len(frozen_scope_ids),
            }
            from looplab.serve.scope_report import generate_scope_report as _gen
            s = srv.llm_settings(None)
            try:
                client = srv.make_llm_client(s)
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
            # Record coverage + staleness over the runs that ACTUALLY contributed a brief — a run skipped
            # above (corrupt log) wasn't read, so it must not count toward "over N runs" or the sig.
            brief_ids = [b["run_id"] for b in briefs]
            source_revisions = [frozen_runs[rid]["revision"] for rid in brief_ids]

            def _inputs_unchanged() -> bool:
                try:
                    current_ids = sorted(set(_scope_run_ids(scope_type, scope_id)))
                    if current_ids != frozen_scope_ids or _scope_sig(current_ids) != frozen_scope_sig:
                        return False
                    if (_scope_context_digest(scope_type, scope_id, current_ids)
                            != frozen_context_digest):
                        return False
                    for rid in brief_ids:
                        _rd, _events, revision = _capture_source(
                            rid, frozen_runs[rid]["run_dir"])
                        if revision != frozen_runs[rid]["revision"]:
                            return False
                    return True
                except Exception:  # noqa: BLE001 - uncertainty must preserve the last-good report
                    return False

            if not _inputs_unchanged():
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            rec = {"scope_identity": _scope_identity(scope_type, scope_id), "scope": scope,
                   "generated_at": int(time.time() * 1000), "run_ids": brief_ids,
                   "sig": [row["log_sig"] for row in source_revisions],
                   "source_revisions": source_revisions,
                   "model": s.llm_model, "content": content}
            try:
                with _scope_store_lock(_reports_dir):
                    # Narrow the optimistic-check window at the actual publication boundary.
                    if (sorted(set(_scope_run_ids(scope_type, scope_id))) != frozen_scope_ids
                            or _scope_sig(frozen_scope_ids) != frozen_scope_sig):
                        return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                    if (_scope_context_digest(scope_type, scope_id, frozen_scope_ids)
                            != frozen_context_digest):
                        return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                    for rid in brief_ids:
                        _rd, _events, revision = _capture_source(
                            rid, frozen_runs[rid]["run_dir"])
                        if revision != frozen_runs[rid]["revision"]:
                            return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
                    # Revalidate the lexical store and re-derive the destination immediately before
                    # publication. A directory/file swapped during the slow model call is refused.
                    _validated_reports_dir(_reports_dir, create=True)
                    _read_or_migrate_scope_record(_reports_dir, scope_type, scope_id)
                    dst = _scope_report_path(_reports_dir, scope_type, scope_id)
                    atomic_write_text(dst, json.dumps(rec, indent=2))
                    dst = _scope_report_path(_reports_dir, scope_type, scope_id)
                    _read_scope_record(dst, scope_type, scope_id)
            except (OSError, _ScopeReportStorageConflict):
                return {"ok": False, **_SCOPE_STORAGE_ERROR}
            omitted = sorted(set(frozen_scope_ids) - set(brief_ids))
            return {"ok": True, **rec, "stale": bool(omitted), "added": omitted}

        return await srv.jobs.run_as_job(_compute)

    return router
