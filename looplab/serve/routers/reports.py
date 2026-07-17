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
from collections import OrderedDict
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
from looplab.serve.scope_report import (
    DEFAULT_SCOPE_REPORT_TIME_S,
    DEFAULT_SCOPE_REPORT_TURNS,
    MAX_SCOPE_REPORT_RUNS,
)
from looplab.serve.scope_sources import (
    MAX_SCOPE_CONFIG_BYTES,
    MAX_SCOPE_EVENT_BYTES,
    MAX_SCOPE_TASK_BYTES,
    MAX_SCOPE_TOTAL_EVENT_BYTES,
    FrozenScopeSource,
    ScopeSourceCapacityError,
    ScopeSourceError,
    capture_scope_source,
    probe_scope_log_sig,
    scope_event_size,
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
_SCOPE_SOURCE_TOO_LARGE = {
    "code": "scope_report_source_too_large",
    "error_kind": "capacity",
    "message": "The scope's event evidence exceeds the bounded cross-run report limit.",
    "error": "Scope event evidence is too large for one bounded report snapshot.",
    "max_run_bytes": MAX_SCOPE_EVENT_BYTES,
    "max_scope_bytes": MAX_SCOPE_TOTAL_EVENT_BYTES,
    "remediation": "Generate a narrower scope report or compact oversized run history.",
}
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVER_VERDICT_AUTHORITY = "server-derived-v3"
_SERVER_CONTENT_SCHEMA = 5
_SCOPE_STORE_THREAD_LOCK = threading.Lock()
_PRIOR_REPORT_MAX_FILES = 256
_PRIOR_REPORT_MAX_RECORDS = 20
_PRIOR_REPORT_MAX_NEXT_DIRECTIONS = 2
_PRIOR_REPORT_MAX_BYTES = 8 * 1024
_PRIOR_REPORT_PARSE_MAX_BYTES = 16 * 1024 * 1024
_SCOPE_REPORT_RECORD_MAX_BYTES = 512 * 1024
_SCOPE_REVISION_CACHE_MAX = 256
_SCOPE_REVISION_CACHE_TTL_S = 60.0
_SCOPE_CONTEXT_SCHEMA = 2


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


def _stat_identity(entry: os.stat_result) -> tuple[int, ...]:
    return (
        int(entry.st_mode), int(entry.st_dev), int(entry.st_ino),
        int(entry.st_mtime_ns), int(entry.st_size),
        int(getattr(entry, "st_file_attributes", 0) or 0),
    )


def _read_bounded_report_bytes(path: Path) -> bytes | None:
    """Read one immutable regular-file snapshot without following a swapped link."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _ScopeReportStorageConflict("scope report could not be inspected") from exc
    if (not stat.S_ISREG(before.st_mode) or _is_link_or_reparse(before)
            or before.st_size > _SCOPE_REPORT_RECORD_MAX_BYTES):
        raise _ScopeReportStorageConflict("scope report is not a bounded regular file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or _is_link_or_reparse(opened)
                    or opened.st_size > _SCOPE_REPORT_RECORD_MAX_BYTES
                    or _stat_identity(opened) != _stat_identity(before)):
                raise _ScopeReportStorageConflict("scope report changed before it was read")
            chunks: list[bytes] = []
            remaining = _SCOPE_REPORT_RECORD_MAX_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except _ScopeReportStorageConflict:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise _ScopeReportStorageConflict("scope report could not be read safely") from exc
    raw = b"".join(chunks)
    if (not stat.S_ISREG(after.st_mode) or _is_link_or_reparse(after)
            or len(raw) != opened.st_size
            or len(raw) > _SCOPE_REPORT_RECORD_MAX_BYTES
            or _stat_identity(after) != _stat_identity(opened)):
        raise _ScopeReportStorageConflict("scope report changed or exceeded its byte limit")
    return raw


def _serialize_scope_record(record: dict[str, Any]) -> str:
    """Serialize only records that can be read back inside the same storage budget."""
    try:
        encoded = json.dumps(record, indent=2)
    except (TypeError, ValueError) as exc:
        raise _ScopeReportStorageConflict("scope report is not serializable") from exc
    # CODEX AGENT: this is the persisted-report resource boundary. Check encoded bytes, not Python
    # characters, before atomic replacement so a model result can never create an unreadable record.
    if len(encoded.encode("utf-8")) > _SCOPE_REPORT_RECORD_MAX_BYTES:
        raise _ScopeReportStorageConflict("scope report exceeds its persisted byte limit")
    return encoded


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
    event_bytes = revision.get("event_bytes")
    if event_bytes is not None and (type(event_bytes) is not int or event_bytes < 0):
        return False
    return True


def _complete_source_revision(revision: object) -> bool:
    """A v2 revision can prove every model-visible file, not only the event-log stat."""
    return (
        _valid_source_revision(revision)
        and all(isinstance(revision.get(field), str) for field in (
            "events_digest", "task_snapshot_digest", "config_snapshot_digest",
        ))
        and type(revision.get("event_bytes")) is int
    )


def _valid_source_receipt(
        run_ids: object, sig: object, source_revisions: object,
        omitted_runs: object, omitted_source_probes: object) -> bool:
    """Validate both legacy all-readable records and the explicit partial-source receipt."""
    if not isinstance(run_ids, list) or not isinstance(sig, list):
        return False
    if source_revisions is None:
        return omitted_runs is None and omitted_source_probes is None
    if (not isinstance(source_revisions, list)
            or not all(_valid_source_revision(row) for row in source_revisions)):
        return False
    revision_ids = [row["run_id"] for row in source_revisions]
    if omitted_runs is None:
        # Legacy v2 records represented only fully captured scopes.
        return revision_ids == run_ids and [row["log_sig"] for row in source_revisions] == sig
    if (not isinstance(omitted_runs, list)
            or not all(isinstance(run_id, str) for run_id in omitted_runs)
            or len(omitted_runs) != len(set(omitted_runs))
            or len(revision_ids) != len(set(revision_ids))):
        return False
    sig_by_id = {row[0]: row for row in sig}
    if len(sig_by_id) != len(sig) or set(sig_by_id) != set(run_ids):
        return False
    captured = set(revision_ids)
    omitted = set(omitted_runs)
    probes_valid = (
        omitted_source_probes is None
        or (
            isinstance(omitted_source_probes, dict)
            and set(omitted_source_probes) == omitted
            and all(
                isinstance(digest, str) and _RUN_GENERATION_RE.fullmatch(digest)
                for digest in omitted_source_probes.values()
            )
        )
    )
    return (
        not captured & omitted
        and captured | omitted == set(run_ids)
        and all(sig_by_id.get(row["run_id"]) == row["log_sig"] for row in source_revisions)
        and probes_valid
    )


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
        and len(run_ids) == len(set(run_ids))
        and isinstance(sig, list)
        and all(_valid_scope_sig_row(row) for row in sig)
        and _valid_source_receipt(
            run_ids, sig, source_revisions, rec.get("omitted_runs"),
            rec.get("omitted_source_probes"))
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
        encoded = _read_bounded_report_bytes(path)
        if encoded is None:
            return None
        raw = encoded.decode("utf-8")
    except UnicodeError as exc:
        raise _ScopeReportStorageConflict("scope report could not be read") from exc
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError) as exc:
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
    atomic_write_text(canonical, _serialize_scope_record(migrated))
    canonical = _scope_report_path(reports_dir, scope_type, scope_id)
    return _read_scope_record(canonical, scope_type, scope_id)


def _valid_observational_groups(rec: dict[str, Any], groups: object) -> bool:
    """Validate schema-v5's server-owned, outcome-free comparison projection."""
    run_ids = rec.get("run_ids")
    if (not isinstance(run_ids, list)
            or not all(isinstance(run_id, str) for run_id in run_ids)
            or len(run_ids) != len(set(run_ids))
            or not isinstance(groups, list) or len(groups) > MAX_SCOPE_REPORT_RUNS):
        return False
    scope_run_ids = set(run_ids)
    seen_contracts: set[str] = set()
    seen_measurements: set[str] = set()
    sources = {
        "search": "best.metric",
        "confirmed": "best.confirmed_mean",
        "holdout": "best.holdout_metric",
    }
    allowed_reasons = {
        "incomplete_measurements",
        "incomplete_runs",
        "insufficient_population",
        "point_estimates_only",
        "minimum_effect_not_declared",
        "incomplete_population",
    }

    for group in groups:
        if not isinstance(group, dict):
            return False
        contract_id = group.get("contract_id")
        direction = group.get("direction")
        phase = group.get("measurement_phase")
        protocol = group.get("uncertainty_protocol")
        reason = group.get("indeterminate")
        measurements = group.get("measurements")
        unavailable = group.get("unavailable_measurements")
        incomplete = group.get("incomplete_runs")
        if (
            not isinstance(contract_id, str)
            or _RUN_GENERATION_RE.fullmatch(contract_id) is None
            or contract_id in seen_contracts
            or direction not in {"min", "max"}
            or phase not in sources
            or not isinstance(protocol, str)
            or not protocol
            or group.get("contract_authority") != "declared"
            or group.get("outcome_policy") != "observations-only-v1"
            or group.get("winner") is not None
            or group.get("tied_winners") != []
            or reason not in allowed_reasons
            or not isinstance(measurements, list)
            or len(measurements) > MAX_SCOPE_REPORT_RUNS
            or not isinstance(unavailable, list)
            or not isinstance(incomplete, list)
        ):
            return False
        seen_contracts.add(contract_id)
        measured_ids: set[str] = set()
        for row in measurements:
            if not isinstance(row, dict):
                return False
            run_id = row.get("run_id")
            uncertainty = row.get("uncertainty")
            if (
                not isinstance(run_id, str)
                or run_id not in scope_run_ids
                or run_id in seen_measurements
                or row.get("authority") != "declared"
                or finite_measurement(row.get("metric")) is None
                or row.get("direction") != direction
                or row.get("phase") != phase
                or row.get("source") != sources[phase]
                or not isinstance(uncertainty, dict)
                or uncertainty.get("protocol") != protocol
            ):
                return False
            if phase == "confirmed":
                if set(uncertainty) != {
                    "protocol", "std", "std_source", "seeds", "seeds_source",
                }:
                    return False
                if (
                    finite_measurement(uncertainty.get("std")) is None
                    or uncertainty["std"] < 0
                    or type(uncertainty.get("seeds")) is not int
                    or uncertainty["seeds"] <= 0
                    or uncertainty.get("std_source") != "best.confirmed_std"
                    or uncertainty.get("seeds_source") != "best.confirmed_seeds"
                ):
                    return False
            elif set(uncertainty) != {"protocol"}:
                return False
            measured_ids.add(run_id)
            seen_measurements.add(run_id)

        def valid_id_list(value: list) -> bool:
            return (
                all(isinstance(run_id, str) and run_id in scope_run_ids for run_id in value)
                and len(value) == len(set(value))
            )

        if (not valid_id_list(unavailable) or not valid_id_list(incomplete)
                or measured_ids & set(unavailable) or not set(incomplete) <= measured_ids):
            return False
        if reason == "incomplete_measurements" and not unavailable:
            return False
        if reason == "incomplete_runs" and not incomplete:
            return False
        if reason == "insufficient_population" and len(measurements) >= 2:
            return False
        if reason == "point_estimates_only" and (
                phase == "confirmed" or len(measurements) < 2 or unavailable or incomplete):
            return False
        if reason == "minimum_effect_not_declared" and (
                phase != "confirmed" or len(measurements) < 2 or unavailable or incomplete):
            return False
    return True


def _valid_metric_observations(rec: dict[str, Any], observations: object) -> bool:
    if not isinstance(observations, list) or len(observations) > MAX_SCOPE_REPORT_RUNS:
        return False
    run_ids = set(rec.get("run_ids") or ())
    allowed = {
        "uncontracted",
        "no_valid_comparison_measurement",
        "contracted_measurement_unavailable",
        "contracted_group_omitted",
    }
    for row in observations:
        if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                or row["run_id"] not in run_ids
                or row.get("comparison_status") not in allowed):
            return False
        if "metric" in row and finite_measurement(row.get("metric")) is None:
            return False
        contract_id = row.get("contract_id")
        if (contract_id is not None and (
                not isinstance(contract_id, str)
                or _RUN_GENERATION_RE.fullmatch(contract_id) is None)):
            return False
    return True


def _public_scope_record(rec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Never present a self-asserted persisted outcome as server-derived authority."""
    content = dict(rec.get("content") or {})
    groups = content.get("comparison_groups")
    if (content.get("schema") == _SERVER_CONTENT_SCHEMA
            and content.get("verdict_authority") == _SERVER_VERDICT_AUTHORITY
            and content.get("narrative_authority") == "model-advisory"
            and _valid_observational_groups(rec, groups)
            and _valid_metric_observations(rec, content.get("metric_observations"))):
        return {**rec, "authoritative": True}, False
    content["verdict"] = "No authoritative verdict is available for this legacy report; regenerate it."
    content["verdict_authority"] = "legacy-unavailable"
    content["requires_regeneration"] = True
    content["headline"] = "Legacy scope report requires regeneration"
    content["narrative_authority"] = "legacy-quarantined"
    for field in (
        "best_runs", "comparison_groups", "metric_observations", "what_worked", "what_didnt",
        "learnings", "next_directions", "caveats",
    ):
        content[field] = []
    # CODEX AGENT: outcome-bearing legacy narrative is quarantined, not merely relabelled. Renaming
    # an invented winner would still let a client accidentally render it as trusted prose.
    return {**rec, "content": content, "authoritative": False}, True


def _prior_learnings_index(reports_dir: Path) -> str:
    """Return a bounded JSON projection of untrusted prior-report evidence for Genesis."""
    try:
        base = _validated_reports_dir(reports_dir)
    except _ScopeReportStorageConflict:
        return ""
    if not base.exists():
        return ""

    inspected_files = 0
    discovered_names: list[str] = []
    try:
        with os.scandir(base) as entries:
            # CODEX AGENT: this is a prompt-input authority boundary. Bound directory work before
            # inspecting names, then revalidate every selected path and redact every copied string.
            while inspected_files < _PRIOR_REPORT_MAX_FILES:
                try:
                    entry = next(entries)
                except StopIteration:
                    break
                inspected_files += 1
                if entry.name.endswith(".json"):
                    discovered_names.append(entry.name)
    except OSError:
        return ""

    def _safe_text(value: object, max_chars: int) -> str:
        clean = redact_persisted_text(
            value, max_chars=max_chars, entropy=True, single_line=True)
        return " ".join(clean.split())

    records: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    parsed_bytes = 0
    parse_limited = False
    for filename in sorted(discovered_names):
        try:
            p = _confined_report_path(base, filename)
            candidate_bytes = int(p.lstat().st_size)
            if candidate_bytes < 0 or (
                    parsed_bytes + candidate_bytes > _PRIOR_REPORT_PARSE_MAX_BYTES):
                parse_limited = True
                continue
            # Count attempted bytes too: a directory full of corrupt bounded JSON must not bypass
            # the aggregate work budget merely because none of it becomes prompt evidence.
            parsed_bytes += candidate_bytes
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
            content = rec.get("content") or {}
            raw_directions = content.get("next_directions")
            directions = (
                raw_directions if isinstance(raw_directions, (list, tuple)) else ())
            projection = {
                "scope": {
                    "type": _safe_text(scope_type, 32),
                    "id": _safe_text(scope_id, 160),
                    "label": _safe_text(
                        (rec.get("scope") or {}).get("label") or "scope report", 200),
                },
                "headline": _safe_text(content.get("headline") or "", 500),
                "next_directions": [
                    _safe_text(value, 300)
                    for value in directions[:_PRIOR_REPORT_MAX_NEXT_DIRECTIONS]
                ],
            }
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError,
                _ScopeReportStorageConflict):
            continue
        key = (scope_type, scope_id)
        if key not in records or priority > records[key][0]:
            # Keep only the already-redacted compact projection. Retaining the full parsed record for
            # every eligible file would amplify a 16 MiB byte budget into far larger Python objects.
            records[key] = (priority, projection)

    projected = [row for _key, (_priority, row) in sorted(records.items())]

    if not projected:
        return ""

    def _encoded(rows: list[dict[str, Any]]) -> str:
        payload = {
            "schema": "looplab.untrusted_prior_reports.v1",
            "trust": "untrusted_model_authored_advisory",
            "records": rows,
            "receipt": {
                "inspected_files": inspected_files,
                "included_records": len(rows),
                "eligible_records": len(projected),
                "omitted_records": len(projected) - len(rows),
                # Reaching the scan ceiling is conservatively reported as limited without reading a
                # 257th entry solely to discover whether it exists.
                "scan_limited": inspected_files >= _PRIOR_REPORT_MAX_FILES,
                "parse_limited": parse_limited,
                "parsed_bytes": parsed_bytes,
                "limits": {
                    "max_files": _PRIOR_REPORT_MAX_FILES,
                    "max_records": _PRIOR_REPORT_MAX_RECORDS,
                    "max_next_directions": _PRIOR_REPORT_MAX_NEXT_DIRECTIONS,
                    "max_bytes": _PRIOR_REPORT_MAX_BYTES,
                    "max_parse_bytes": _PRIOR_REPORT_PARSE_MAX_BYTES,
                },
            },
        }
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    included: list[dict[str, Any]] = []
    for row in projected[:_PRIOR_REPORT_MAX_RECORDS]:
        candidate = _encoded([*included, row])
        if len(candidate.encode("utf-8")) > _PRIOR_REPORT_MAX_BYTES:
            break
        included.append(row)
    encoded = _encoded(included)
    # The fixed envelope is comfortably below 8 KiB, but keep this fail-closed invariant local so a
    # future metadata addition cannot silently create an unbounded prompt fragment.
    return encoded if len(encoded.encode("utf-8")) <= _PRIOR_REPORT_MAX_BYTES else ""


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _phase = srv.phase
    projects = srv.projects
    _reports_dir = srv.reports_dir
    revision_cache_lock = threading.Lock()
    revision_cache: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
    omission_cache: OrderedDict[tuple, float] = OrderedDict()
    generation_jobs_lock = threading.Lock()
    generation_jobs: dict[str, str] = {}

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
        summaries = srv.list_runs_fn()
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
                # CODEX AGENT: this single bounded receipt is the only cross-run numeric evidence.
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
            try:
                size = scope_event_size(srv.root, run_id)
                run_dir = Path(srv.root).absolute() / run_id
                for filename, limit in (
                    ("task.snapshot.json", MAX_SCOPE_TASK_BYTES),
                    ("config.snapshot.json", MAX_SCOPE_CONFIG_BYTES),
                ):
                    try:
                        status = (run_dir / filename).lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise ScopeSourceError(
                            f"{filename} could not be inspected") from exc
                    if not stat.S_ISREG(status.st_mode) or _is_link_or_reparse(status):
                        raise ScopeSourceError(
                            f"{filename} is not a trusted regular file")
                    if int(status.st_size) > limit:
                        raise ScopeSourceCapacityError(
                            f"{filename} exceeds its scope-report byte limit")
            except ScopeSourceCapacityError:
                raise
            except ScopeSourceError:
                size = 0
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
        # CODEX AGENT: ordinary rewrites invalidate dev/ino/ctime/size/mtime immediately. The bounded
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
        # CODEX AGENT: a negative cache may skip work only when the answer is already stale. An
        # omitted receipt needs a current failed-open observation before it can authorize
        # ``stale:false``: accessibility is not part of the cheap stat key, so a transient lock can
        # clear without changing that key. A formerly omitted run is therefore always new evidence.
        return False, event_bytes

    # CODEX AGENT: scope ids are opaque persisted identities, so the route must preserve legal
    # task/project ids containing ``/`` instead of truncating or rejecting them at the HTTP boundary.
    @router.get("/api/scope-report/{scope_type}/{scope_id:path}")
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

        def _compute() -> dict:
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
                # CODEX AGENT: the reservation owns the source probe observed by the POST handler.
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
                # CODEX AGENT: preserve honest scope coverage even when a corrupt/unreadable run cannot
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

            # CODEX AGENT: the capture already bound every model-visible byte. Re-check its complete
            # cheap identity before client construction/publication so ordinary races consume no paid
            # call, without reparsing the same event log three more times inside one generation job.
            if not _inputs_unchanged():
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
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
            if not _inputs_unchanged():
                return {"ok": False, **_SCOPE_INPUTS_CHANGED, "stale": True}
            rec = {"scope_identity": _scope_identity(scope_type, scope_id), "scope": scope,
                   "generated_at": int(time.time() * 1000), "run_ids": frozen_scope_ids,
                   # CODEX AGENT: sig and run_ids use the complete scope vocabulary even when one
                   # source is unreadable. omitted_runs says exactly which members supplied no brief.
                   "sig": frozen_scope_sig,
                   "source_revisions": source_revisions,
                   "omitted_runs": omitted,
                   "omitted_source_probes": {
                       rid: frozen_probe_receipts[rid] for rid in omitted},
                   "context_schema": _SCOPE_CONTEXT_SCHEMA,
                   "context_digest": frozen_context_digest,
                   "model": s.llm_model, "content": content}
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
                    atomic_write_text(dst, _serialize_scope_record(rec))
                    dst = _scope_report_path(_reports_dir, scope_type, scope_id)
                    _read_scope_record(dst, scope_type, scope_id)
            except (OSError, _ScopeReportStorageConflict):
                return {"ok": False, **_SCOPE_STORAGE_ERROR}
            return {"ok": True, **rec, "authoritative": True,
                    "stale": False, "added": []}

        # Synthesis is paid. Coalesce only the exact work that is still RUNNING, while retaining each
        # terminal process receipt for the registry's bounded observer window. This separates two
        # contracts that a consume-on-first-poll identity cannot satisfy at once: every joined tab can
        # observe the shared terminal, and a later explicit Regenerate over unchanged evidence is a
        # genuinely new action rather than a replay of an old narrative.
        with generation_jobs_lock:
            for identity, job_id in list(generation_jobs.items()):
                job = srv.jobs.get(job_id)
                if job is None or job.get("status") != "running":
                    generation_jobs.pop(identity, None)
            joined_job_id = generation_jobs.get(generation_identity)
            if joined_job_id is not None:
                reservation = {"status": "running", "job_id": joined_job_id}
            else:
                reservation = srv.jobs.reserve(consume_on_poll=False)
                if reservation.get("status") == "running":
                    generation_jobs[generation_identity] = reservation["job_id"]
        if reservation.get("status") != "running":
            return reservation
        return await srv.jobs.run_as_job(
            _compute, reserved_job_id=reservation["job_id"], consume_inline_result=False)

    return router
