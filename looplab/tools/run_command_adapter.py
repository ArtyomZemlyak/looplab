"""The narrow seam between the assistant's run-control tools and the server-owned command service.

Split out of `machine_runs_tools.py` on 2026-09-08 (doc 25 TO-02). Everything here answers one
question — what did the command service actually do with the intent we handed it — and the answers
are deliberately paranoid: a transport failure after acceptance, a differently-keyed conflict, and
an unobserved terminal status are three different outcomes and the model is told which one it got.

`_local_run_generation` stays a local re-derivation of `serve/run_commands.py::run_generation_token`
rather than an import, because `tools` sits below `serve` in the package graph (doc 25 TO-03).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from looplab.tools.turn_mutation_fence import _exact_run_generation, _MutationRecoveryBlocked


_COMMAND_PENDING = frozenset({"accepted", "executing"})
_COMMAND_FAILED = frozenset({"failed", "rejected", "timed_out"})


def _local_run_generation(rd: Path) -> str:
    """Compute the same first-event identity as RunCommandService without a tools -> serve import."""
    from looplab.events.eventstore import EventStore

    events = EventStore(rd / "events.jsonl").read_all()
    if not events:
        return ""
    first = events[0]
    raw = json.dumps({
        "seq": first.seq,
        "ts": first.ts,
        "type": first.type,
        "run_id": (first.data or {}).get("run_id"),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _deletion_operation_id(key: str) -> str:
    if not key:
        return str(uuid.uuid4())
    digest = hashlib.sha256(("looplab-delete-v1\0" + key).encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _render_deletion_result(result: dict, rid: str) -> str:
    operation_id = str(result.get("operation_id") or "")
    if result.get("status") == "succeeded" and result.get("ok") is True:
        return f"(deleted run {rid} and all its artifacts; operation {operation_id})"
    phase = str(result.get("phase") or "pending")
    code = str(result.get("code") or "delete_pending")
    return (
        f"(run {rid} deletion is pending at {phase}; code={code}; retry only exact operation "
        f"{operation_id})")


def _command_record(value) -> dict:
    """Coerce the command service's record/model to the small mapping this tool consumes."""
    if isinstance(value, dict):
        return dict(value)
    for method in ("model_dump", "to_dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            out = fn()
            if isinstance(out, dict):
                return dict(out)
    if value is not None and hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _safe_command_text(value, limit: int = 300) -> str:
    """Bound one server-owned display field; never stringify arbitrary exception payloads."""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return " ".join(str(value).split())[:limit]


def _safe_command_error(record: dict) -> dict:
    """Return only the command contract's public error fields (never raw internals/tracebacks)."""
    error = (record or {}).get("error")
    if not isinstance(error, dict):
        return {
            "code": "command_failed",
            "message": "The run command did not complete.",
            "retryable": False,
            "remediation": "Review the run state before retrying.",
        }
    code = _safe_command_text(error.get("code"), 80) or "command_failed"
    code = re.sub(r"[^a-zA-Z0-9_.-]", "_", code)
    return {
        "code": code,
        "message": _safe_command_text(error.get("message")) or "The run command did not complete.",
        "retryable": bool(error.get("retryable", False)),
        "remediation": _safe_command_text(error.get("remediation")),
    }


class _RunCommandAdapter:
    """Narrow seam around the server-owned run-command service."""

    def __init__(self, service, *, key_namespace: str = ""):
        self.service = service
        self._pending_by_run: dict[str, dict] = {}
        self._key_namespace = str(key_namespace or "")
        self._intent_occurrences: dict[str, int] = {}

    def _observe(self, rd: Path, record: dict) -> dict:
        """Briefly observe an accepted command; observation failure leaves it honestly pending."""
        status = record.get("status")
        command_id = record.get("id")
        if status not in _COMMAND_PENDING or not command_id:
            return record
        try:
            get = getattr(self.service, "get", None)
            value = get(rd, command_id) if callable(get) else None
        except Exception:  # noqa: BLE001 — accepted is durable; a failed observation is not command failure
            return record
        observed = _command_record(value)
        return observed or record

    def run_generation(self, rd: Path) -> str:
        """Capture the service's current durable generation before fresh intent staging."""
        getter = getattr(self.service, "run_generation", None)
        if not callable(getter):
            return _exact_run_generation(_local_run_generation(rd))
        try:
            value = getter(rd)
        except Exception as exc:
            raise _MutationRecoveryBlocked(
                "run_generation_unavailable",
                "The run generation could not be read; no run mutation was attempted.") from exc
        return _exact_run_generation(value)

    def submit(self, rd: Path, event_type: str, data: dict, *, idempotency_key: str = "",
               expected_generation: str = "") -> dict:
        if self.service is None or not callable(getattr(self.service, "submit", None)):
            return {"status": "failed", "event_type": event_type, "error": {
                "code": "command_service_unavailable",
                "message": "Run commands are temporarily unavailable.",
                "retryable": True,
                "remediation": "Retry after the control service is available.",
            }}
        run_key = str(rd.resolve())
        pending = self._pending_by_run.get(run_key)
        if pending is not None:
            pending = self._observe(rd, pending)
            if pending.get("status") in _COMMAND_PENDING:
                command_id = _safe_command_text(pending.get("id"), 100)
                return {"status": "rejected", "event_type": event_type,
                        "error": {
                            "code": "command_in_progress",
                            "message": "A prior run command is still pending; no conflicting command was submitted.",
                            "retryable": False,
                            "remediation": (f"Observe command {command_id} to a terminal status first."
                                            if command_id else "Observe the prior command first."),
                        }}
            self._pending_by_run.pop(run_key, None)

        if idempotency_key:
            key = str(idempotency_key)
        elif self._key_namespace:
            # The user turn is durably staged before the model runs. Replaying that dangling turn
            # after a server crash reconstructs the same ordered tool keys, so a succeeded additive
            # budget/fork cannot be submitted again merely because the reply was never persisted.
            raw = json.dumps({"type": event_type, "data": data}, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            intent = f"{run_key}\0{event_type}\0{raw}"
            occurrence = self._intent_occurrences.get(intent, 0)
            self._intent_occurrences[intent] = occurrence + 1
            material = f"{self._key_namespace}\0{intent}\0{occurrence}"
            key = "asst_" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        else:
            key = str(uuid.uuid4())          # compatibility for direct/test construction
        generation = _exact_run_generation(expected_generation)
        predicted_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        try:
            record = _command_record(self.service.submit(
                rd, key, event_type, data, expected_generation=generation))
        except Exception as exc:  # noqa: BLE001 — never expose internals or retry a possibly accepted submission
            # HTTP 409 from the service names the already-authoritative command. A transport failure
            # may have happened after acceptance, in which case the id is deterministic from our key.
            detail = getattr(exc, "detail", "")
            detail_payload = detail if isinstance(detail, dict) else {}
            conflict_code = _safe_command_text(detail_payload.get("code"), 80)
            match = re.search(r"cmd_[0-9a-f]{32}", str(detail))
            command_id = match.group(0) if match else predicted_id
            uncertain = {"id": command_id, "status": "executing", "event_type": event_type}
            observed = self._observe(rd, uncertain)
            # A different active command is only a serialization conflict, never the outcome of this
            # requested action. Even if GET races and finds that old command succeeded, reporting it as
            # this action's success would be a dangerous false positive (e.g. stop vs prior resume).
            if conflict_code == "command_in_progress":
                if observed.get("status") in _COMMAND_PENDING:
                    self._pending_by_run[run_key] = observed
                message = (_safe_command_text(detail_payload.get("message"))
                           or "Another run command was in progress; this action was not submitted.")
                remediation = (_safe_command_text(detail_payload.get("remediation"))
                               or f"Observe command {command_id}, then submit this action again.")
                return {"status": "rejected", "event_type": event_type, "error": {
                    "code": "command_in_progress", "message": message, "retryable": False,
                    "remediation": remediation,
                }}
            # Only the server's explicit identical-intent code may safely attach this invocation to a
            # differently-keyed existing command. Unknown structured conflicts stay rejected below.
            if detail_payload and conflict_code != "retry_existing_command":
                return {"status": "rejected", "event_type": event_type, "error": {
                    "code": conflict_code or "command_submit_conflict",
                    "message": (_safe_command_text(detail_payload.get("message"))
                                or "The run command was not submitted."),
                    "retryable": False,
                    "remediation": (_safe_command_text(detail_payload.get("remediation"))
                                    or f"Inspect command {command_id} before trying again."),
                }}
            if conflict_code == "retry_existing_command":
                if observed.get("status") in _COMMAND_PENDING:
                    self._pending_by_run[run_key] = observed
                return observed
            if observed.get("status") in _COMMAND_PENDING:
                self._pending_by_run[run_key] = observed
            else:
                return observed
            return {"id": command_id, "status": "failed", "event_type": event_type, "error": {
                "code": "command_status_uncertain",
                "message": "The submission outcome is uncertain; no blind duplicate will be sent.",
                "retryable": False,
                "remediation": f"Observe command {command_id} before retrying or issuing another control.",
            }}
        record = self._observe(rd, record)
        if record.get("status") in _COMMAND_PENDING:
            self._pending_by_run[run_key] = record
        else:
            self._pending_by_run.pop(run_key, None)
        return record

    def _require_generation(self, rd: Path, expected_generation: str) -> None:
        expected = _exact_run_generation(expected_generation)
        current = self.run_generation(rd)
        if current != expected:
            raise _MutationRecoveryBlocked(
                "run_generation_changed",
                "The run was reset or replaced after this mutation was formed; no mutation was applied.")

    @staticmethod
    def _reject_unresolved_reset(rd: Path, operation: str) -> None:
        from looplab.core.run_deletion import (
            RunDeletionStorageError, load_run_deletion_fence)
        from looplab.core.run_reset import RunResetStorageError, load_run_reset_marker
        try:
            deletion = load_run_deletion_fence(rd)
        except RunDeletionStorageError as exc:
            raise _MutationRecoveryBlocked(
                "run_deletion_fence_unavailable",
                f"Cannot {operation} because deletion ownership cannot be verified.") from exc
        if deletion is not None:
            raise _MutationRecoveryBlocked(
                "run_deletion_in_progress",
                f"Cannot {operation} while deletion {deletion['operation_id']} is unresolved.")
        try:
            marker = load_run_reset_marker(rd)
        except RunResetStorageError as exc:
            raise _MutationRecoveryBlocked(
                "run_reset_fence_unavailable",
                f"Cannot {operation} because Replay ownership cannot be verified.") from exc
        if marker is not None:
            raise _MutationRecoveryBlocked(
                "run_reset_in_progress",
                f"Cannot {operation} while Replay {marker['operation_id']} is unresolved.")

    @contextmanager
    def destructive_guard(self, rd: Path, operation: str, *, expected_generation: str):
        """Use the server's per-run command sequencer when this provider runs in the UI server."""
        guard = getattr(self.service, "destructive_guard", None)
        if callable(guard):
            with guard(rd, operation) as canonical:
                self._reject_unresolved_reset(canonical, operation)
                self._require_generation(canonical, expected_generation)
                yield canonical
            return
        # Standalone/unit-tool use has no AppState command coordinator. Preserve the historical tool
        # surface there; the live check below remains mandatory and is re-run immediately before I/O.
        self._reject_unresolved_reset(rd, operation)
        self._require_generation(rd, expected_generation)
        yield rd

    @contextmanager
    def mutation_guard(self, rd: Path, operation: str, *, expected_generation: str):
        """Serialize a direct non-registry event/snapshot mutation with run commands and deletion."""
        sequence = getattr(self.service, "sequence", None)
        validate = getattr(self.service, "validate_paths", None)
        reject = getattr(self.service, "reject_if_active", None)
        if callable(sequence) and callable(validate):
            with sequence(rd):
                canonical = validate(rd)
                if callable(reject):
                    reject(canonical, operation)
                self._require_generation(canonical, expected_generation)
                yield canonical
            return
        # Standalone compatibility: at least re-check existence immediately before the write. The UI
        # server always supplies the real sequencer above.
        if not (rd / "events.jsonl").exists():
            raise RuntimeError("run disappeared before mutation")
        self._require_generation(rd, expected_generation)
        yield rd

    @property
    def durable_deletion_available(self) -> bool:
        return callable(getattr(self.service, "begin_or_resume_deletion", None))

    def begin_or_resume_deletion(
            self, rd: Path, *, operation_id: str, expected_generation: str,
            expected_seq: int) -> dict:
        delete = getattr(self.service, "begin_or_resume_deletion", None)
        if not callable(delete):
            raise _MutationRecoveryBlocked(
                "run_deletion_service_unavailable",
                "Durable run deletion is unavailable; no run files were modified.")
        try:
            value = delete(
                rd, operation_id=operation_id,
                expected_generation=expected_generation, expected_seq=expected_seq)
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            if isinstance(detail, dict):
                raise _MutationRecoveryBlocked(
                    str(detail.get("code") or "run_deletion_failed"),
                    str(detail.get("message") or "Run deletion did not complete.")) from exc
            raise
        return dict(value) if isinstance(value, dict) else {}

def _render_command_result(record: dict, *, name: str, run_id: str, completed: str) -> str:
    """Render an honest, bounded tool result for the model and eventual user-facing answer."""
    status = _safe_command_text((record or {}).get("status"), 40)
    command_id = _safe_command_text((record or {}).get("id"), 100)
    command = f"; command {command_id}" if command_id else ""
    if status == "succeeded":
        return f"(completed: {completed}{command})"
    if status == "noop":
        return f"(completed/no-op: {completed} was already satisfied{command})"
    if status in _COMMAND_PENDING:
        return (f"(requested/pending: {name} for {run_id}{command}; the server accepted the command "
                "but has not observed its postcondition yet)")
    if status not in _COMMAND_FAILED:
        record = {**(record or {}), "error": {"code": "unexpected_command_status",
                  "message": "The run command returned an unknown status.", "retryable": False,
                  "remediation": "Inspect the run before retrying."}}
    error = _safe_command_error(record)
    tail = f"; remediation={error['remediation']}" if error["remediation"] else ""
    return (f"(command failed: {name} for {run_id}; code={error['code']}; "
            f"message={error['message']}; retryable={'yes' if error['retryable'] else 'no'}{tail}{command})")
