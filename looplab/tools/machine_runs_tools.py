"""Cross-run introspection tools for the assistant (ADR-7 tool protocol).

Where `RunTools` reads the ONE live run bound to it and `SiblingRunTools` reads other runs of the
SAME task, `MachineRunsTools` gives the general-purpose assistant a richer view over every run under
its configured run root — so it can reference an existing run, report which ones are live, and read
logs/traces before steering or fixing it. It is not a host-wide filesystem scanner. Same
`.specs()`/`.execute()` shape as the other providers; every `execute` returns a string and soft-fails
(a junk tool call must never crash the loop).

Runs are folded from disk on demand and cached by each event log's (size, mtime) fingerprint, so
repeated turns don't re-fold unchanged runs. Liveness (`engine_running`) is injected as a callable
by the server (`_engine_alive`) to avoid a circular import and to reuse the one race-free lock probe.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Optional

from looplab.events import digest
from looplab.core.models import RunState
from looplab.tools.run_tools import ForeignRunReader
from looplab.tools._base import RESULT_CAP, fn_spec

# A trace is a whole conversation, but the shared tool loop HEAD-truncates every tool result to
# RESULT_CAP chars (agent.drive_tool_loop), so a larger budget would be silently cut there (losing
# the tail with no marker). Stay under that cap (-400 headroom for the header + our truncation hint)
# so our own truncation + the "narrow with `stage`" hint engage first.
_TRACE_CHARS = RESULT_CAP - 400


_COMMAND_PENDING = frozenset({"accepted", "executing"})
_COMMAND_FAILED = frozenset({"failed", "rejected", "timed_out"})
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


def _exact_run_generation(value: object) -> str:
    if not isinstance(value, str) or _RUN_GENERATION_RE.fullmatch(value) is None:
        raise _MutationRecoveryBlocked(
            "run_generation_unavailable",
            "The run generation is missing or invalid; no run mutation was attempted.")
    return value


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


class _MutationRecoveryBlocked(RuntimeError):
    """Fail-closed signal for a mutation that a recovered assistant turn may not issue."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _TurnMutationFence:
    """Durable, ordered mutation journal for one assistant user turn.

    A process crash loses the model/tool trace, so replaying the dangling user turn can produce a
    different sequence or different payload.  Fresh turns stage every mutation intent here *before*
    touching the run.  A recovered turn may consume only the exact entries that were already staged;
    once those entries are exhausted, or when the next intent differs, it fails closed.  Command-backed
    entries reuse the journaled key and can therefore safely observe/re-submit the same command.  Direct
    storage mutations are not replayed because their crash point cannot be proven from this journal.
    """

    _VERSION = 2

    def __init__(self, path: Path, namespace: str, *, recovering: bool):
        self.path = Path(path)
        self.namespace = str(namespace or "")
        self.recovering = bool(recovering)
        self._namespace_digest = hashlib.sha256(self.namespace.encode("utf-8")).hexdigest()
        self._cursor = 0
        self._invalid = ""
        self._entries: list[dict] = []
        self._load()
        # A server-created turn id is unique.  Finding a pre-existing journal while the router says
        # this is a fresh turn means ownership/recovery state is inconsistent; never append through it.
        if not self.recovering and self._entries:
            self._invalid = "a mutation journal already exists for a fresh assistant turn"

    @staticmethod
    def _canonical(intent: dict) -> str:
        return json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)

    def _key(self, index: int, raw: str, expected_generation: str) -> str:
        material = f"{self.namespace}\0mutation\0{index}\0{expected_generation}\0{raw}"
        return "asst_" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != self._VERSION:
                raise ValueError("unsupported mutation journal")
            if payload.get("namespace_digest") != self._namespace_digest:
                raise ValueError("mutation journal belongs to another turn")
            entries = payload.get("entries")
            if not isinstance(entries, list):
                raise ValueError("mutation journal entries are malformed")
            checked = []
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict) or entry.get("index") != index:
                    raise ValueError("mutation journal ordering is malformed")
                intent = entry.get("intent")
                if not isinstance(intent, dict) or not isinstance(entry.get("command_backed"), bool):
                    raise ValueError("mutation journal intent is malformed")
                generation = entry.get("expected_generation")
                if not isinstance(generation, str) or _RUN_GENERATION_RE.fullmatch(generation) is None:
                    raise ValueError("mutation journal generation is malformed")
                raw = self._canonical(intent)
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if (entry.get("intent_digest") != digest
                        or entry.get("idempotency_key") != self._key(index, raw, generation)):
                    raise ValueError("mutation journal integrity check failed")
                checked.append(dict(entry))
            self._entries = checked
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._invalid = str(exc) or "mutation journal is unreadable"
            self._entries = []

    def _persist(self) -> None:
        from looplab.core.atomicio import atomic_write_text
        payload = {"version": self._VERSION, "namespace_digest": self._namespace_digest,
                   "entries": self._entries}
        atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def claim(self, intent: dict, *, command_backed: bool,
              expected_generation: Optional[str] = None) -> tuple[str, str]:
        if self._invalid:
            raise _MutationRecoveryBlocked(
                "assistant_turn_journal_unavailable",
                "The durable mutation journal is unavailable; no run mutation was attempted.")
        try:
            raw = self._canonical(intent)
        except (TypeError, ValueError):
            raise _MutationRecoveryBlocked(
                "assistant_turn_intent_invalid",
                "The mutation intent is not durably serializable; no run mutation was attempted.")

        if self.recovering:
            if self._cursor >= len(self._entries):
                raise _MutationRecoveryBlocked(
                    "assistant_turn_recovery_fenced",
                    "This recovered turn may not introduce a new run mutation. Start a new turn after reviewing recovery.")
            entry = self._entries[self._cursor]
            if entry.get("intent_digest") != hashlib.sha256(raw.encode("utf-8")).hexdigest() \
                    or entry.get("intent") != intent \
                    or bool(entry.get("command_backed")) != bool(command_backed):
                raise _MutationRecoveryBlocked(
                    "assistant_turn_recovery_conflict",
                    "The recovered mutation differs from the durable original intent; no run mutation was attempted.")
            self._cursor += 1
            if not command_backed:
                raise _MutationRecoveryBlocked(
                    "assistant_turn_direct_mutation_uncertain",
                    "The original direct mutation may already have completed; inspect its state before a new turn.")
            return str(entry["idempotency_key"]), str(entry["expected_generation"])

        generation = _exact_run_generation(expected_generation)
        index = len(self._entries)
        key = self._key(index, raw, generation)
        entry = {
            "index": index,
            "intent": intent,
            "intent_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "idempotency_key": key,
            "expected_generation": generation,
            "command_backed": bool(command_backed),
        }
        self._entries.append(entry)
        try:
            self._persist()
        except OSError:
            self._entries.pop()
            raise _MutationRecoveryBlocked(
                "assistant_turn_journal_unavailable",
                "The mutation could not be staged durably; no run mutation was attempted.")
        return key, generation

    def claim_recovery(self, tool: str, run_id: str) -> tuple[str, str, dict]:
        """Consume the exact next durable deletion intent after its run directory disappeared."""
        if self._invalid:
            raise _MutationRecoveryBlocked(
                "assistant_turn_journal_unavailable",
                "The durable mutation journal is unavailable; no recovery was attempted.")
        if not self.recovering or self._cursor >= len(self._entries):
            raise _MutationRecoveryBlocked(
                "assistant_turn_recovery_fenced",
                "No matching durable run deletion is available to recover.")
        entry = self._entries[self._cursor]
        intent = entry.get("intent")
        if (not isinstance(intent, dict) or intent.get("tool") != tool
                or intent.get("run_id") != run_id
                or entry.get("command_backed") is not True
                or not isinstance(intent.get("data"), dict)):
            raise _MutationRecoveryBlocked(
                "assistant_turn_recovery_conflict",
                "The recovered deletion differs from the durable original intent.")
        self._cursor += 1
        return (
            str(entry["idempotency_key"]), str(entry["expected_generation"]),
            dict(intent["data"]),
        )


def _node_subtree(state: RunState, root_id: int) -> set[int]:
    """``root_id`` plus every descendant reachable through ``parent_ids``.

    Deleting a node alone would orphan its children's parent links, so every delete path resolves
    the whole subtree first — and all three of them (the plan, the commit, and the purge's
    re-verification) were carrying their own verbatim copy of this walk. They are not free to
    disagree: the purge compares its own answer against the approved one and refuses on a
    mismatch, so a copy that drifted would turn a correct approval into a permanent refusal.

    The graph is a DAG, not a tree — a merge node has several parents — so this is a fixpoint
    sweep rather than a recursive descent, and a node joins as soon as ANY parent is inside.
    """
    found = {root_id}
    changed = True
    while changed:
        changed = False
        for node in state.nodes.values():
            if node.id not in found and any(parent in found for parent in node.parent_ids):
                found.add(node.id)
                changed = True
    return found


def _node_lifecycle_unchanged(store, *, node_id: int, expected_tail: int,
                              generation: int) -> bool:
    """Whether the exact node lifecycle a permission card was formed against is still current.

    A confirm card can stay open indefinitely while another control resets, re-tags or tombstones
    the node underneath it, so approval is re-checked against the log immediately BEFORE the
    mutation is submitted. The fence is the whole log TAIL, not just the node: the operator
    approved an action against a run they were shown, and a sibling append changed that run.
    """
    from looplab.events.replay import fold

    events = store.read_all()
    tail = events[-1].seq if events else -1
    node = fold(events).nodes.get(node_id)
    return (tail == expected_tail and node is not None and not node.tombstoned
            and node.attempt == generation)


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


# How many episodes a map prints at EACH END before the middle is elided. Both ends on purpose (the
# same rule `tools/log_tools.py::_render_search` keeps): a node's first episodes are where a bug
# first showed and its last are where it died, and a reader shown only one end cannot tell an
# always-broken node from a just-broken one. The elided middle is COUNTED and reachable by ordinal,
# so nothing is silently dropped.
_EPISODE_ENDS = 25
_EPISODE_PAGE = 60


def _render_episodes(payload: dict, run_id, nid, from_index, limit, max_chars: int) -> str:
    """One line per episode: its POSITION, label, when, how long, how many spans, and its `anchor`.

    A map is chosen FROM, not read from, so every row is identity plus the seek key and nothing
    else. It always states the node's TOTAL episode count, because a bounded list that does not is
    the "no record matches" answer this repo has now paid for twice.

    Paging is by POSITION in this map (`#1`, `#2`, …) and deliberately NOT by the row's own
    `ordinal`: that field is the engine's inline-repair counter, which is absent on every band that
    is not a repair — a pager keyed on it would silently skip the plan, the build and the training.
    The repair ordinal is still SHOWN, where it exists, because it is what the operator's own
    question ("the third repair") names.
    """
    episodes = list(payload.get("episodes") or [])
    projection = payload.get("projection") or {}
    if not episodes:
        return f"(run {run_id} node #{nid}: no trace episodes recorded for its current attempt.)"
    total = len(episodes)
    rows = list(enumerate(episodes, start=1))
    shown, elided = rows, 0
    if from_index is not None:
        try:
            start = int(from_index)
        except (TypeError, ValueError):
            return f"(from_index must be a whole number, got {from_index!r})"
        try:
            page = max(1, int(limit)) if limit is not None else _EPISODE_PAGE
        except (TypeError, ValueError):
            page = _EPISODE_PAGE
        shown = [row for row in rows if row[0] >= start][:page]
        head = f"from #{start}"
    elif total > 2 * _EPISODE_ENDS:
        shown = rows[:_EPISODE_ENDS] + rows[-_EPISODE_ENDS:]
        elided = total - 2 * _EPISODE_ENDS
        head = "first and last"
    else:
        head = "all"
    lines = [f"run {run_id} · node #{nid} · {total} episode(s) in this attempt ({head} shown; "
             f"pass an `anchor` as `before` to `read_run_trace` to read one):"]
    if projection.get("omitted_episodes"):
        lines.append(f"  [{projection['omitted_episodes']} older episode(s) are past the map's own "
                     f"ceiling and are not listed]")
    if not shown:
        lines.append(f"  (no episode at or after #{from_index} — this node has {total})")
    for position, (index, episode) in enumerate(shown):
        if elided and position == _EPISODE_ENDS:
            lines.append(f"  … {elided} episode(s) elided — pass from_index=<n> to read from there")
        seconds = episode.get("seconds")
        span_count = episode.get("spans")
        lines.append(
            f"  #{index} {episode.get('label') or episode.get('band') or '(unnamed)'}"
            + (f" · repair {episode['ordinal']}" if episode.get("ordinal") is not None else "")
            + (f" · {float(seconds):.0f}s" if isinstance(seconds, (int, float)) else "")
            + (f" · {span_count} span(s)" if span_count else "")
            + (f" · {episode.get('status')}" if episode.get("status") else "")
            + f" · anchor={episode.get('anchor')}")
    text = "\n".join(lines)
    budget = max(max_chars, _TRACE_CHARS)
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + (f"\n…[+{len(text) - budget} chars truncated — page with "
                                     f"from_index]")


def _render_conversation(convo: dict, run_id, nid, stage: Optional[str], max_chars: int,
                         *, before: Optional[str] = None) -> str:
    """Render `traceview.build_conversation` output as a readable linear thread. One block per stage
    (create_node / evaluate / …); within a stage, requests show the prompt, generations show
    thinking + output + which tools were called, tool turns show input→output. Filtered to one stage
    when `stage` is given (substring match on its label). Bounded to a generous trace budget.

    The window this rendered is a TAIL unless `before` anchored it, and a reader that cannot see
    that reads a bounded window as the whole node — the omission is therefore stated in the header
    from the projection's own receipt, beside the control that moves it."""
    stages = convo.get("stages") or []
    if stage:
        s = str(stage).lower()
        stages = [st for st in stages if s in str(st.get("label") or "").lower()]
    if not stages:
        which = f" matching {stage!r}" if stage else ""
        anchored = f" ending at {before}" if before else ""
        return f"(run {run_id} node #{nid}: no trace stages{which}{anchored} recorded)"
    projection = convo.get("projection") or {}
    omitted = projection.get("omitted_spans") or 0
    reach = ""
    if before:
        reach += f" · window ends at {before}"
    if omitted:
        reach += (f" · {omitted} earlier span(s) of this node are outside this window — call "
                  f"`read_run_trace_episodes` and re-read with `before=<anchor>`")
    lines = [f"run {run_id} · node #{nid} · trace ({len(stages)} stage(s){reach}):"]
    for st in stages:
        roll = st.get("rollup") or {}
        tok = (roll.get("tokens") or {}).get("total")
        meta = f"{roll.get('generations', 0)} gen · {roll.get('tools', 0)} tool"
        meta += f" · {tok} tok" if tok else ""
        lines.append(f"\n══ stage: {st.get('label') or '(unnamed)'} · {meta} ══")
        for t in st.get("turns") or []:
            kind = t.get("type")
            if kind == "request":
                lines.append("▶ REQUEST" + (f" [{t['label']}]" if t.get("label") else ""))
                for m in t.get("messages") or []:
                    body = str(m.get("content") or "").strip()
                    if body:
                        lines.append(f"  [{m.get('role')}] {body}")
            elif kind == "generation":
                if t.get("think"):
                    lines.append(f"🧠 {str(t['think']).strip()}")
                if str(t.get("output") or "").strip():
                    lines.append(f"💬 {str(t['output']).strip()}")
                calls = [c for c in (t.get("tool_calls") or []) if c]
                if calls:
                    lines.append(f"  → called {', '.join(str(c) for c in calls)}")
            elif kind == "tool":
                head = f"⚙ {t.get('name') or 'tool'}"
                if t.get("status") and t["status"] != "OK":
                    head += f" ({t['status']})"
                lines.append(head)
                if str(t.get("input") or "").strip():
                    lines.append(f"    in:  {str(t['input']).strip()}")
                if str(t.get("output") or "").strip():
                    lines.append(f"    out: {str(t['output']).strip()}")
    text = "\n".join(lines)
    budget = max(max_chars, _TRACE_CHARS)
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + f"\n…[+{len(text) - budget} chars truncated — narrow with `stage`]"


@dataclass(frozen=True)
class RunLifecycleFns:
    """The `serve`-owned primitives a run-MUTATING tool needs, as an explicit contract.

    Every field is a fence, not a convenience: the lifecycle lock is the only thing standing between
    a delete and a resume that has been claimed but has not yet taken `engine.lock`, and the two
    launch-pending predicates are what make that window observable. Naming them here means a
    caller can substitute them (a test, a different host) without `tools/` reaching upward into
    `serve/` — see doc 25 XP-03.
    """
    engine_alive: Callable
    fresh_resume_launch_pending: Callable
    fresh_run_launch_pending: Callable
    run_lifecycle_lock: Callable
    run_config_write_lock: Callable


@dataclass(frozen=True)
class TraceRewriteFns:
    """Injected serving-layer transaction primitives used by irreversible node purge.

    ``tools`` is below ``serve`` in the package graph.  The assistant composition root supplies
    these callables so the tool can reuse trace-clear's descriptor-first filter and durable publish
    transaction without reaching upward into a private serving module.
    """
    prepare_filtered_snapshot: Callable
    digest_snapshot: Callable
    publish_prepared_snapshot: Callable


class MachineRunsTools(ForeignRunReader):
    """Read-only view over ALL runs under the run-root (for the assistant).

    Cache/reader composition and the delegate-with-receipt shape come from `ForeignRunReader`
    (doc 25 TO-05); this provider adds the LIVENESS column and the trace projection, and — like
    `AllRunsTools` — deliberately applies no task scope."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 max_chars: int = 3500):
        super().__init__(run_root, max_chars=max_chars)
        self.alive_fn = alive_fn

    # MachineRunsTools is not bound to a single run; accept bind_state for CompositeTools symmetry (no-op).
    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_runs",
                # Scoped to THIS run root, not the machine: `_run_ids()` iterates `self.run_root`
                # only. The old "EVERY run on this machine" wording made an absent result read as
                # portfolio-wide evidence that nobody had tried something, which is exactly the
                # inference that causes a repeated experiment. Factual correction of a described
                # scope, not a prompt rewrite.
                "List every LoopLab run under this run root with its goal, phase, best metric, node count "
                "and whether its engine is LIVE right now. Use to reference an existing run, see what "
                "is running, or pick one to inspect/steer.",
                {"only_live": {"type": "boolean",
                               "description": "if true, list only runs whose engine is currently live"}}),
            fn_spec("read_run",
                "Read ONE run in detail: goal, direction, phase, best experiment and its top "
                "experiments. Use a run_id from list_runs before steering or fixing it.",
                {"run_id": {"type": "string"},
                 "sort": {"type": "string", "enum": ["best", "worst", "recent"]},
                 "limit": {"type": "integer"}},
                ["run_id"]),
            fn_spec("read_run_experiment",
                "Read one experiment of a run in full detail (params, metric, robustness, rationale, "
                "failure, sweep trials). Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_logs",
                "Read one experiment's EXECUTION LOGS: the captured stdout/stderr TAILS as recorded "
                "in the event log (bounded, not the raw full stream — the tail end holds the error "
                "and the final metric line). Far more than the short failure summary. Use to see what "
                "a node printed while training, or why it failed. Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_trace",
                "Read one experiment's bounded CAPTURED AGENT TRACE as a linear, de-duplicated "
                "conversation: recorded requests, model output/reasoning fields, tool calls and tool "
                "results. Capture may be disabled, redacted or truncated; this is not proof of the "
                "model's complete internal reasoning. The window is the node's LATEST steps unless "
                "you pass `before`; a long node (thousands of steps over hours) does not fit in one "
                "window, and the answer says how many earlier steps it left out. To read those, call "
                "read_run_trace_episodes and pass an episode's `anchor` as `before`. "
                "Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "stage": {"type": "string", "description": "optional: only the stage whose label "
                                                            "contains this text (e.g. 'repair')"},
                 "before": {"type": "string", "description": "optional: an `anchor` from "
                                                             "read_run_trace_episodes — the window "
                                                             "then ENDS at that step instead of at "
                                                             "the node's newest one"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_trace_episodes",
                "Map one experiment's trace: every episode it recorded (plan, build, train, each "
                "repair…) with its ordinal, label, duration, span count and the `anchor` that seeks "
                "read_run_trace to it — and none of their contents. Cheap. Use this FIRST when a "
                "node ran long or was repaired many times, to find the part worth reading.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "from_index": {"type": "integer",
                                "description": "optional: list from this episode POSITION onward "
                                               "(the `#n` in each row); the default shows both ends "
                                               "and counts the elided middle"},
                 "limit": {"type": "integer",
                           "description": "optional: how many rows to list from `from_index`"}},
                ["run_id", "node_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "list_runs":
                return self._list_runs(bool(args.get("only_live")))
            if name == "read_run":
                return self._read_run(args.get("run_id"), args.get("sort"), args.get("limit"))
            if name == "read_run_experiment":
                return self._read_experiment(args.get("run_id"), int(args.get("node_id")),
                                             args.get("trials"))
            if name == "read_run_logs":
                return self._read_logs(args.get("run_id"), int(args.get("node_id")))
            if name == "read_run_trace":
                return self._read_trace(args.get("run_id"), int(args.get("node_id")),
                                        args.get("stage"), args.get("before"))
            if name == "read_run_trace_episodes":
                return self._read_trace_episodes(args.get("run_id"), int(args.get("node_id")),
                                                 args.get("from_index"), args.get("limit"))
            return f"(unknown tool: {name})"
        # BROAD on purpose — the module docstring's "soft-fails, never raises" contract. A narrower
        # tuple missed AttributeError and everything else that folding a foreign log or building a
        # trace conversation can raise, and `drive_tool_loop` does NOT guard `tools.execute`
        # (cross_run_tools.py documents that), so one odd shape killed the whole assistant turn.
        # `RunControlTools` below already catches this way.
        except Exception as e:  # noqa: BLE001 - a tool must not be able to end the turn
            return f"(tool error: {e})"

    # --- machine-readable summaries (also reused by the /api/assistant run-ref expansion) ------------
    def summaries(self, only_live: bool = False) -> list[dict]:
        """Structured per-run summary for EVERY run (used by the tool AND by @run-mention expansion)."""
        out = []
        for rid in self._run_ids():
            # A SWEEP over every run under the root — its folds must not evict the runs the turn is
            # actually working with (`tools/_runcache.py::_cache_max`).
            st = self._state(rid, scan=True)
            if st is None:
                continue
            live = self._alive(rid)
            if only_live and not live:
                continue
            best = st.best()
            out.append({
                "run_id": rid, "goal": st.goal or st.task_id, "direction": st.direction,
                "phase": ("finished" if st.finished else ("live" if live else "idle")),
                "nodes": len(st.nodes),
                "best_metric": (digest.node_metric(best) if best else None),
                "best_node_id": (best.id if best else None),
                "engine_running": live, "finished": st.finished,
            })
        return out

    # --- internals -----------------------------------------------------------
    def _run_ids(self) -> list[str]:
        return self._runs.run_ids()

    def _safe_dir(self, run_id: Optional[str]) -> Optional[Path]:
        return self._runs.safe_dir(run_id)

    def _alive(self, run_id: str) -> bool:
        if self.alive_fn is None:
            return False
        rd = self._safe_dir(run_id)
        try:
            return bool(rd is not None and self.alive_fn(rd))
        except Exception:  # noqa: BLE001 - liveness is best-effort; never crash the loop
            return False

    def _list_runs(self, only_live: bool) -> str:
        rows = self.summaries(only_live)
        if not rows:
            return "(no live runs)" if only_live else "(no runs yet)"
        lines = []
        for r in rows:
            live = " · LIVE" if r["engine_running"] else ""
            best = digest.fmt_num(r["best_metric"]) if r["best_metric"] is not None else "—"
            lines.append(f"{r['run_id']}: {str(r['goal'])[:70]} · best={best} ({r['direction']}) · "
                         f"{r['nodes']} nodes · {r['phase']}{live}"
                         + self._partial_suffix(r["run_id"]))
        return f"{len(lines)} run(s):\n" + "\n".join(lines)

    def _read_run(self, run_id, sort, limit) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        note = self._runs.source_note(run_id)
        best = st.best()
        live = self._alive(str(run_id))
        head = (f"run {run_id} · goal: {st.goal or st.task_id} · direction={st.direction} · "
                f"phase={'finished' if st.finished else ('live' if live else 'idle')} · "
                f"{len(st.nodes)} nodes · best={digest.fmt_num(digest.node_metric(best)) if best else '—'}"
                + (f" (#{best.id})" if best else ""))
        self._reader.bind_state(st, None)
        listing = self._reader.execute("list_experiments",
                                       {"sort": sort or "best", "limit": int(limit or 8)})
        return (f"{note}\n" if note else "") + head + "\n" + listing

    def _read_experiment(self, run_id, nid: int, trials_arg=None) -> str:
        return self._delegate(run_id, "read_experiment", {"node_id": nid, "trials": trials_arg},
                              prefix=f"run {run_id} · ")

    def _read_logs(self, run_id, nid: int) -> str:
        return self._delegate(run_id, "read_logs", {"node_id": nid}, prefix=f"run {run_id} · ")

    def _trace_source(self, run_id, nid: int):
        """`(run_dir, state, spans_path, attempt)` for a node trace read, or a refusal SENTENCE.

        The three-line preamble both trace surfaces need — resolve the run, prove a trace was
        recorded at all, and settle the node's lifecycle generation. Returned as a tuple or a string
        so the two callers cannot come to disagree about which attempt they are reading (the map's
        anchors are only valid inside the generation the window then reads)."""
        rd = self._safe_dir(run_id)
        st = self._state(run_id)
        if rd is None or st is None:
            return f"(no such run: {run_id!r})"
        spans_path = rd / "spans.jsonl"
        if not spans_path.exists():
            return (f"(run {run_id} has no spans.jsonl — no agent trace was recorded. This run may "
                    "predate tracing, or ran with tracing off.)")
        attempt = getattr(st.nodes.get(nid), "attempt", 0)
        return rd, st, spans_path, (attempt if type(attempt) is int and attempt >= 0 else 0)

    def _read_trace(self, run_id, nid: int, stage: Optional[str] = None,
                    before: Optional[str] = None) -> str:
        """The node's agent trace as a linear, de-duplicated conversation. Reuses the SAME
        `build_conversation` projection the Web UI's Trace tab shows (so the assistant reads exactly
        what the human sees), rendered to text and bounded to `max_chars`.

        `before` is the F6 SEEK, and it is what makes that "exactly what the human sees" claim true
        again: the window is the newest `TRACE_CONVERSATION_SPAN_CAP` spans of one
        `(node_id, generation)`, so without an anchor everything older is unreachable at any
        parameter — measured on `runs/rubert-dr-0804` node 1 (14,507 spans over 3 h 50 m, 2,345
        inline repairs, all of them generation 0 because inline repair does not bump `Node.attempt`):
        74 % of that node could not be read, INCLUDING every early repair an operator asks "what
        happened in this node" to find out about. The HTTP routes gained `?before=` and an episode
        map; this surface is a sibling caller of the very same `full_spans_for_node` /
        `build_conversation` and did not. Anchors come from `read_run_trace_episodes`.

        An anchor this run's index cannot place is REFUSED, never degraded to the tail — the routes'
        rule (`_settle_window_anchor`), for the same reason: answering with the newest spans under an
        older episode's label is worse than answering nothing, and worse still for a reader that
        cannot see the label."""
        source = self._trace_source(run_id, nid)
        if isinstance(source, str):
            return source
        _rd, st, spans_path, attempt = source
        note = self._runs.source_note(run_id)   # a truncated log must not read as complete
        from looplab.events.span_index import get_index
        from looplab.events.traceview import (
            TRACE_CONVERSATION_SPAN_CAP, build_conversation, load_spans, settle_trace_anchor)
        try:
            index = get_index(spans_path)
            anchor = settle_trace_anchor(before) if str(before or "").strip() else None
            if str(before or "").strip() and (
                    anchor is None or index is None or not index.has_span(anchor)):
                return (f"(run {run_id} node #{nid}: {before!r} is not a step in this run's trace "
                        f"index, so the window cannot be placed on it — call "
                        f"`read_run_trace_episodes` for this node and use an episode's `anchor`.)")
            if index is not None:
                total = index.node_span_count(nid, generation=attempt)
                spans = index.full_spans_for_node(
                    nid, TRACE_CONVERSATION_SPAN_CAP, generation=attempt, before=anchor)
                convo = build_conversation(
                    st, spans, nid, total_spans=total,
                    span_cap=TRACE_CONVERSATION_SPAN_CAP,
                    # The node's build claims, resolved over the whole index — `spans` is a bounded
                    # window and the claiming span can fall outside it. Same reason as the route's:
                    # an anchor INSIDE the build legitimately ends before the `materialize_node` row
                    # that names it, and re-deriving from the window would then drop every row.
                    claimed_traces=index.node_build_traces(nid, generation=attempt),
                    _normalized=True)
            else:
                # Missing indexes are rare (the source existence was checked above). Preserve the
                # compatibility path, but apply the same attempt fence before the conversation cap.
                convo = build_conversation(
                    st, load_spans(spans_path), nid, generation=attempt, _normalized=True)
        except Exception as e:  # noqa: BLE001 — an unexpected hand-edited/I/O failure must soft-fail
            return f"(could not read trace: {e})"  # and never terminate the agent tool loop
        return ((f"{note}\n" if note else "")
                + _render_conversation(convo, run_id, nid, stage, self.max_chars, before=anchor))

    def _read_trace_episodes(self, run_id, nid: int, from_index=None, limit=None) -> str:
        """THE MAP of one node's trace — every episode, with none of their contents.

        The half that makes `before` usable: an anchor the reader cannot discover is not a control.
        Same derivation as `/nodes/{nid}/episodes` (`traceview.node_episodes` over the in-memory
        light index, no spans.jsonl bytes at all), so the map and the window it aims speak one
        vocabulary and describe one generation."""
        source = self._trace_source(run_id, nid)
        if isinstance(source, str):
            return source
        _rd, _st, spans_path, attempt = source
        from looplab.events.span_index import get_index
        from looplab.events.traceview import node_episodes
        try:
            index = get_index(spans_path)
            if index is None:
                return (f"(run {run_id} node #{nid}: this run's trace index is unavailable, so its "
                        "episodes cannot be mapped — read the trace without an anchor.)")
            payload = node_episodes(
                index.light_spans_for_node(nid, None, generation=attempt), nid,
                total_spans=index.node_span_count(nid, generation=attempt), _normalized=True)
        except Exception as e:  # noqa: BLE001 — same soft-fail contract as every tool here
            return f"(could not read episodes: {e})"
        note = self._runs.source_note(run_id)
        return ((f"{note}\n" if note else "")
                + _render_episodes(payload, run_id, nid, from_index, limit, self.max_chars))


class RunLauncherTools:
    """Lets the assistant PROPOSE a new run (the evolution of the Genesis 'New run' flow). It does not
    launch anything itself — it records an editable spec that the UI shows as a launch card, and the
    user starts it via the existing /api/start. So run-creation is one assistant capability rather than
    a separate modal."""

    def __init__(self):
        self.proposals: list[dict] = []

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("propose_run",
                "Propose a NEW LoopLab run for the user to launch (a run name + a task + optional "
                "settings). The user reviews an editable card and starts it — you do not launch it. "
                "Give EITHER an inline `task` object OR a `task_file` from the catalogue. Put "
                "model/max_nodes/etc. in `settings`. Inline tasks are VALIDATED before card creation "
                "and an invalid one is bounced back to you; task-file cards are resolved and validated "
                "by launch-card preflight.\n"
                "A task is COMPOSABLE — there is NO `kind`. You describe what you HAVE and the engine "
                "infers the task. Always give `goal` and `direction` (EXACTLY \"max\" or \"min\"), then "
                "add the capability fields that apply:\n"
                "IMPORTANT — the `goal` is the ONLY task text the coding agent (Developer) reads; the "
                "`rationale` and any knowledge you save are NOT reliably in its context. So put EVERY "
                "developer-critical setup detail IN THE GOAL: required CLI flags (e.g. a `--flag` that is "
                "mandatory or the run crashes), a known-good baseline command to start from, data quirks "
                "(label conventions, formats), exact paths that exist. If you discovered a must-have flag "
                "or command while exploring, it belongs in the goal, not just the summary you show me.\n"
                "• `repo`: ABSOLUTE path to an editable codebase that EXISTS on disk — the agent may edit "
                "ANY file within it (protect exceptions with `protect:[...]`).\n"
                "• `dataset`: read-only data/model weights that live OUTSIDE the repo, as "
                "{\"<mount>\":\"<ABSOLUTE path>\"} (a bare path is mounted as ./dataset). They appear at "
                "./<mount> in the workdir. A repo that trains but has NO dataset mounts fails every node "
                "with file-not-found — DISCOVER the paths from the repo (README, configs, script defaults) "
                "+ the user's message, VERIFY each exists, and if a required path is unknown ASK in "
                "`reply` (never omit/guess).\n"
                "• `cmd`: HOW to run + score one experiment. Either a bare argv "
                "([\"python\",\"test.py\"]) or an object {command:[...], metric:{reader,...}, timeout}. "
                "`metric.reader` is one of stdout_json / stdout_regex / file_json / file_regex — HOW to "
                "read the printed metric. For stdout_json/file_json give `key` (the JSON field, e.g. "
                "\"recall\"); for stdout_regex/file_regex give `pattern` (a regex whose group 1 is the "
                "number, e.g. \"RECALL@100: ([0-9.]+)\") — NOT `key`; add `path` for the file_* readers. Set "
                "`reader:\"auto\"` ONLY for the narrow case where a training COMMAND already runs and you "
                "just need the agent to write the metric reader.\n"
                "• `kaggle`: a Kaggle / MLE-bench competition slug (the official grader scores a "
                "submission — no `cmd` needed).\n"
                "`cmd` IS A CONTRACT — the command that runs + the reader that reads its metric. It is the "
                "SCORING step, NOT the trainer: training is a SEPARATE stage the agent declares at run time "
                "(its `declare_stages` tool), and the engine runs it BEFORE `cmd`. WHAT the agent may EDIT "
                "is a SEPARATE, independent decision — `edit_surface` (globs the agent may edit; default = "
                "the WHOLE repo) minus `protect` (exceptions). The file `cmd` runs is NOT auto-protected, "
                "so decide edit-scope explicitly:\n"
                "  • `cmd` points at an OPERATOR-owned scorer the agent must not tamper with (e.g. the "
                "framework's test.py) → add that file to `protect` (the agent then adds a train stage before "
                "it; your protected cmd scores the freshly-trained model).\n"
                "  • `cmd` points at a file the agent must BUILD → leave it editable (a protected file can't "
                "be created).\n"
                "  • NO existing scorer anywhere → point `cmd` at an entrypoint the agent will BUILD "
                "(e.g. [\"python\",\"looplab_eval.py\"]) and leave it editable — a repo task ALWAYS "
                "carries a `cmd` (or metric.reader \"auto\"); say in the goal what it must train and "
                "print.\n"
                "In every case say each node must actually TRAIN a fresh model and score THAT model — never "
                "read a pre-existing checkpoint or a static results file (results_last.csv is a PRIOR run's "
                "output, not a score). If training happens, set `cmd.timeout` GENEROUSLY (seconds): training "
                "runs minutes-to-hours but the default is 600s, which SIGKILLs it mid-first-epoch into an "
                "undertrained model — size it to the full schedule (often 7200-14400s).\n"
                "OPTIONAL fields (the engine honors them — reach for them when the task needs it): "
                "`edit_surface`:[globs] restricts what the agent may edit (default: the WHOLE repo); "
                "a `setup`:[argv] field INSIDE `cmd` runs before each eval (write it nested: "
                "`cmd`:{command, metric, setup:[\"pip\",\"install\",\"-r\",\"requirements.txt\"]} — NOT a "
                "top-level \"cmd.setup\" key); a `profiles` field INSIDE `cmd` "
                "({smoke:{overrides,timeout},full:{…}}) gives a cheap search eval + a full "
                "confirm eval; `params`:{name:[lo,hi]} + a `%params%` token in a command tunes numeric "
                "hyperparameters with NO code edit; `editables`:[{name,path,surface}] mounts several "
                "editable repos. Per-source DATA permissions: a `dataset`/`data` value may be an object "
                "{path, mount(read-only symlink vs copy-in), edit, copy_modify, preprocess, extend} — "
                "default is read-only with copy/preprocess/extend allowed, so the agent can derive/augment "
                "a training set but not touch the original. To let it MODIFY the data, set mount:false (a "
                "writable per-node copy); a mounted original is read-only, so mount:true+edit:true is "
                "auto-converted to a writable copy.",
                {"run_id": {"type": "string", "description": "short kebab-case name you invent"},
                 "task": {"type": "object", "description": "composable inline task: goal + direction + the fields you have (repo / dataset / cmd{command|stages,metric:{reader,key},timeout} / kaggle). No `kind`."},
                 "task_file": {"type": "string", "description": "a catalogue task path (alternative to task)"},
                 "settings": {"type": "object", "description": "engine overrides, e.g. {\"llm_model\":..,\"max_nodes\":..}"},
                 "rationale": {"type": "string"},
                 "setup_steps": {"type": "array", "items": {"type": "string"},
                                 "description": "operator-facing readiness/adaptation notes; these are not executed automatically"}},
                ["run_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        if name != "propose_run":
            return f"(unknown tool: {name})"
        args = args or {}
        rid = str(args.get("run_id") or "").strip()
        if not rid:
            return "(propose_run needs a run_id)"
        task = args.get("task") if isinstance(args.get("task"), dict) else None
        # a model sometimes passes `task` as a JSON STRING — parse it instead of bouncing with a
        # misleading error (the old wording sent agents hunting for a legacy `kind` field)
        if task is None and isinstance(args.get("task"), str) and args["task"].strip().startswith("{"):
            try:
                parsed = json.loads(args["task"])
                task = parsed if isinstance(parsed, dict) else None
            except Exception:  # noqa: BLE001 — fall through to the error below
                task = None
        task_file = args.get("task_file") or None
        if not task and not task_file:
            return ("(propose_run needs an inline composable `task` OBJECT — goal + direction + the "
                    "fields you have (repo / dataset / cmd / kaggle), NO `kind` — or a `task_file`)")
        # VALIDATE before proposing so the card the user sees is actually launchable — an invalid spec
        # (e.g. a repo task with no `eval` and no `onboard`) is bounced BACK to you to fix here, instead
        # of failing only when the user clicks Start (which spawns an engine that dies with no events).
        if task:
            try:
                # DELIBERATE runtime-only upward import (tools -> adapters): validating a task spec
                # inherently needs the adapter registry (_KINDS + model_validate), which cannot move
                # below tools; a constructor-injected validator would add a "silently unvalidated"
                # default. Kept lazy so the import graph stays acyclic at import time.
                from looplab.adapters.tasks import validate_task
                validate_task(task)
            except Exception as e:  # noqa: BLE001
                return (f"(NOT proposed — the task is INVALID: {e}\nFix it and call propose_run again. "
                        "A repo task MUST carry a `cmd` {command|stages, metric:{reader,key}} — point it "
                        "at a file the agent will BUILD if no scorer exists — or set metric.reader "
                        "\"auto\"; `repo` must be an ABSOLUTE path that exists.)")
        steps = [str(step).strip() for step in (args.get("setup_steps") or [])
                 if str(step).strip()][:12]
        spec = {"proposal_id": str(uuid.uuid4()),
                "run_id": rid, "task": task or {}, "task_file": task_file,
                "settings": args.get("settings") if isinstance(args.get("settings"), dict) else {},
                "rationale": str(args.get("rationale") or ""), "setup_steps": steps}
        self.proposals.append(spec)
        # describe the proposal by WHAT the composable task carries (there is no `kind` field)
        what = task_file or (task and ("repo" if task.get("repo") else
                                       "kaggle" if (task.get("kaggle") or task.get("competition")) else
                                       "dataset" if (task.get("dataset") or task.get("data")) else
                                       task.get("kind") or "task")) or "a task"
        return (f"(proposed run '{rid}' ({what}) — shown to the user as a launch card; they will start "
                "it. Tell them what you proposed.)")


class RunControlTools:
    """Lets the assistant DRIVE an existing run's lifecycle — finalize, stop, resume, reset a node,
    delete a node, or delete the whole run. Lifecycle/engine commands go through the server-owned
    command service; only the deliberately separate destructive delete implementations edit storage
    here. Every verb first goes through `decide(mode, ...)` + the injected `approver` (a UI
    confirm-card), so it's denied in read-only `plan` mode, asks in default/acceptEdits, and runs inline
    only in `auto`. Destructive edits (delete node/run) additionally REFUSE while the run is live: all
    appenders serialize through EventStore, but a physical rewrite cannot safely race those appends."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 mode: str = "plan", approver: Optional[Callable] = None, *,
                 command_service=None, command_key_namespace: str = "",
                 mutation_journal_path=None, mutation_recovery: bool = False,
                 lifecycle: "Optional[RunLifecycleFns]" = None,
                 trace_rewrite: "Optional[TraceRewriteFns]" = None):
        self.run_root = Path(run_root)
        self.alive_fn = alive_fn
        self.mode = mode
        self.approver = approver
        # The serve-side run-lifecycle primitives, INJECTED (doc 25 XP-03). `tools/` sits below
        # `serve/` in the package map, and `serve/assistant.py` constructs this class — so reaching
        # up into `serve` from here closes a tools<->serve cycle that only function-local imports
        # were keeping open. Passing them in makes the dependency an explicit argument of the one
        # component that needs it. ``None`` keeps read/lifecycle controls usable for embedders that
        # construct this provider directly, while irreversible trace purge fails closed until the
        # host supplies the serving-layer transaction boundary.
        self._lifecycle = lifecycle
        self._trace_rewrite = trace_rewrite
        self._commands = _RunCommandAdapter(
            command_service, key_namespace=command_key_namespace)
        self._mutation_fence = (_TurnMutationFence(
            Path(mutation_journal_path), command_key_namespace, recovering=mutation_recovery)
            if mutation_journal_path is not None and command_key_namespace else None)

    def lifecycle(self) -> "RunLifecycleFns":
        """The injected run-lifecycle primitives, or the lazily-imported serve defaults.

        Resolved per call rather than in `__init__` so the default path keeps its historical import
        timing — these are only needed by the mutating tools, and importing `serve` at construction
        would make every read-only assistant session pay for (and depend on) the server package.
        """
        if self._lifecycle is not None:
            return self._lifecycle
        from looplab.serve.engine_proc import (
            _engine_alive, _fresh_resume_launch_pending, _fresh_run_launch_pending,
            _run_lifecycle_lock)
        from looplab.serve.run_files import run_config_write_lock
        return RunLifecycleFns(
            engine_alive=_engine_alive,
            fresh_resume_launch_pending=_fresh_resume_launch_pending,
            fresh_run_launch_pending=_fresh_run_launch_pending,
            run_lifecycle_lock=_run_lifecycle_lock,
            run_config_write_lock=run_config_write_lock,
        )

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("finalize_run",
                "Finalize a run: stop it AND wrap up (final report + cross-run lessons + cost roll-up). "
                "Use to END a run cleanly; the command service attaches the driver when needed.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("stop_run",
                "Freeze a run (pause, NO wrap-up) — resumable later. Use to PAUSE without finalizing.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("resume_run",
                "Resume a stopped/finished run through the durable singleton-owner handoff. Records "
                "the intent, lets a live/finalizing owner serve it or hand it off, and otherwise "
                "claims and launches the engine without requiring a separate UI action.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("reset_node",
                "Re-run an existing node IN PLACE from a stage (no new node): 'eval' re-scores (keep the "
                "code), 'implement' re-runs only the Developer (keep the idea), 'propose' is a full redo. "
                "The command service resumes the run when needed.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 # No enum: the executor + HTTP route accept ANY pipeline stage name, and an enum here
                 # would make the model refuse legitimate stage resets (train, data_prep, …).
                 "stage": {"type": "string",
                           "description": "propose | implement | eval, or any eval-pipeline stage "
                           "name (train, data_prep, …) to re-run the pipeline from that stage"}},
                ["run_id", "node_id"]),
            fn_spec("retag_node",
                "Re-tag ONE experiment's CONCEPTS on a run — replace node #node_id's concept ids with the "
                "given `axis/slug` list (e.g. after you notice a mis-tag). Operator-authoritative: it wins "
                "over the Researcher's authored tags and the classifier, and is the per-run counterpart to "
                "the cross-run `concept_merge`/`concept_split` taxonomy edits. Pass every concept the node "
                "should carry (not a delta); an empty list clears its tags.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "concepts": {"type": "array", "items": {"type": "string"},
                              "description": "the full axis/slug concept id list for this node"}},
                ["run_id", "node_id", "concepts"]),
            fn_spec("set_run_concepts",
                "Set a run's BASE concept set — the common `axis/slug` concepts every node inherits unless "
                "it authors a delta. The engine seeds this from the first experiment; use this to correct or "
                "refine it. Last-write-wins; pass the full base list.",
                {"run_id": {"type": "string"},
                 "concepts": {"type": "array", "items": {"type": "string"},
                              "description": "the full axis/slug base concept id list for the run"}},
                ["run_id", "concepts"]),
            fn_spec("delete_node",
                "DELETE a node AND its descendants from a run. Default is an APPEND-ONLY tombstone: the "
                "subtree is logically removed (excluded from best-pick / breeding / re-eval) while its "
                "events stay in the log, so it's reversible and parent/chosen/archive refs stay valid. "
                "Pass purge=true for an IRREVERSIBLE physical compaction that also rewrites the log and "
                "removes spans + workdirs (backs the log up first). Refuses while the engine is live — "
                "stop the run first.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "purge": {"type": "boolean",
                           "description": "irreversibly rewrite the log + remove workdirs (default: "
                           "false = reversible tombstone)"}},
                ["run_id", "node_id"]),
            fn_spec("delete_run",
                "DELETE an entire run and all its artifacts. DESTRUCTIVE + irreversible. Refuses while "
                "the engine is live — stop the run first.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("extend_budget",
                "Give a run MORE budget (and REOPEN it if it already finished, so the new budget is "
                "actually used). Set any of: add_nodes (N more experiment nodes), max_seconds (new "
                "wall-clock ceiling), max_eval_seconds (new cumulative-eval ceiling). The command "
                "service attaches the engine when needed and reports the observed outcome.",
                {"run_id": {"type": "string"},
                 "add_nodes": {"type": "integer", "description": "additive: N more experiment nodes"},
                 "max_seconds": {"type": "number", "description": "new whole-run wall-clock ceiling (s)"},
                 "max_eval_seconds": {"type": "number", "description": "new cumulative in-eval ceiling (s)"}},
                ["run_id"]),
            fn_spec("set_directive",
                "Give the run's agents a standing DIRECTIVE that steers the next proposals + code "
                "(e.g. 'use only sklearn', 'prefer lighter models', 'stop trying deep nets'). "
                "replace=true rewrites the single directive instead of accumulating.",
                {"run_id": {"type": "string"}, "text": {"type": "string"},
                 "replace": {"type": "boolean", "description": "replace all prior directives (default: append)"}},
                ["run_id", "text"]),
            fn_spec("set_trust_gate",
                "Change what a reward-hack / leakage flag does to the run: audit (surface only) · "
                "gate (a flagged node can't win and isn't bred from) · block (also fully infeasible). "
                "Applies immediately (last-write-wins) on the next fold.",
                {"run_id": {"type": "string"},
                 "trust_gate": {"type": "string", "enum": ["audit", "gate", "block"]}},
                ["run_id", "trust_gate"]),
        ]

    # ------------------------------------------------------------------ helpers
    def _rd(self, run_id) -> Optional[Path]:
        # Resolve a run_id to its dir, refusing traversal (must be a direct, existing child of run-root).
        rid = str(run_id or "").strip()
        if not rid or "/" in rid or "\\" in rid or rid.startswith("."):
            return None
        root = self.run_root.resolve()
        candidate = root / rid
        try:
            # Refuse aliases even when they happen to resolve to another direct child: direct mutation
            # paths (notably set_trust_gate) must never follow a run/events symlink outside the root.
            if candidate.is_symlink():
                return None
            rd = candidate.resolve()
            events = rd / "events.jsonl"
            if rd.parent != root or events.is_symlink() or not events.exists() \
                    or events.resolve().parent != rd:
                return None
        except OSError:
            return None
        return rd

    def _gate(self, name: str, rid: str, rd: Path, verb: str, *,
              scope: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
        # Returns a "declined/disabled" string to short-circuit, or None to proceed.
        from looplab.tools.perm_modes import decide_action, refusal_for
        action = {"tool": name, "tool_kind": "run_control", "label": f"{name} {rid}",
                  "verb": verb, "preview": f"{name}({rid})", "run_id": rid,
                  "scope": dict(scope or {"run_id": rid})}
        denied = ("(run control is disabled in read-only plan mode — switch to "
                  "default/acceptEdits/auto.)")
        # `refusal_for` rather than `authorize`: the generation must be captured BETWEEN the deny
        # short-circuit and the approval round-trip, so the mutation fence describes the run as it was
        # before the user was asked. A deny also returns NO generation — nothing was fenced.
        decision = decide_action(self.mode, action)
        if decision == "deny":
            return denied, None
        generation = (None if self._mutation_fence is not None and self._mutation_fence.recovering
                      else self._commands.run_generation(rd))
        return refusal_for(decision, self.approver, action,
                           denied=denied, declined=f"{name} {rid}"), generation

    def _live(self, rd: Path) -> bool:
        """Is a run's engine actively writing its log? The flock probe is primary, but on FUSE / NFS / S3
        mounts flock can wrongly report "not live" — so ALSO trip on a fresh-write backstop: a run that
        is neither paused nor finished AND whose events.jsonl was appended in the last 30s is treated as
        live (the engine and serialized control writers keep the log fresh). This gates the destructive
        delete_node/delete_run so they can't rewrite the log out from under a live engine even when flock
        lies. Conservative: a genuinely crashed run (stale mtime) still deletes."""
        try:
            if self.alive_fn and self.alive_fn(rd):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            import time as _time
            from looplab.events.eventstore import EventStore
            from looplab.events.replay import fold
            evp = rd / "events.jsonl"
            st = fold(EventStore(evp).read_all())
            if st.finished or st.paused:
                return False                              # a settled run is safe to act on
            return (_time.time() - evp.stat().st_mtime) < 30.0   # recent write on an unsettled run -> live
        except Exception:  # noqa: BLE001
            return False

    @contextmanager
    def _mutation_intent(self, name: str, rid: str, rd: Path, data: dict, *, command_backed: bool,
                         expected_generation: Optional[str]):
        """Stage one canonical run mutation before any command/event/storage side effect."""
        key = ""
        generation = ""
        if self._mutation_fence is not None:
            key, generation = self._mutation_fence.claim(
                {"tool": name, "run_id": rid, "data": data}, command_backed=command_backed,
                expected_generation=expected_generation)
        else:
            generation = _exact_run_generation(expected_generation)
        yield key, generation

    # ------------------------------------------------------------------ dispatch
    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        rid = str(args.get("run_id") or "").strip()
        rd = self._rd(rid)
        if rd is None:
            if (name == "delete_run" and self._mutation_fence is not None
                    and self._mutation_fence.recovering):
                try:
                    return self._recover_delete_run(rid)
                except _MutationRecoveryBlocked as e:
                    return f"(run mutation blocked: code={e.code}; {e})"
                except Exception as e:  # noqa: BLE001 - a tool error must never crash the loop
                    return f"(tool error in {name}: {e})"
            return f"(no such run: {rid!r})"
        try:
            if name in ("finalize_run", "stop_run", "resume_run"):
                return self._control(name, rid, rd)
            if name == "reset_node":
                return self._reset_node(rid, rd, args)
            if name == "retag_node":
                return self._retag_node(rid, rd, args)
            if name == "set_run_concepts":
                return self._set_run_concepts(rid, rd, args)
            # One method per verb: the outer dispatch used to hand three unrelated settings verbs to
            # a single `_settings`, which then re-dispatched on the same name it was just given.
            if name in ("extend_budget", "set_directive", "set_trust_gate"):
                return getattr(self, f"_tool_{name}")(name, rid, rd, args)
            if name == "delete_node":
                return self._delete_node(rid, rd, args)
            if name == "delete_run":
                return self._delete_run(rid, rd)
        except _MutationRecoveryBlocked as e:
            return f"(run mutation blocked: code={e.code}; {e})"
        except Exception as e:  # noqa: BLE001 — a tool error must never crash the loop
            return f"(tool error in {name}: {e})"
        return f"(unknown tool: {name})"

    def _recover_delete_run(self, rid: str) -> str:
        if not self._commands.durable_deletion_available or self._mutation_fence is None:
            raise _MutationRecoveryBlocked(
                "run_deletion_service_unavailable",
                "The exact deletion receipt cannot be recovered in this process.")
        if (not rid or "/" in rid or "\\" in rid or rid.startswith(".")
                or Path(rid).name != rid):
            raise _MutationRecoveryBlocked(
                "run_deletion_identity_invalid", "The durable deletion run id is invalid.")
        key, generation, data = self._mutation_fence.claim_recovery("delete_run", rid)
        expected_tail = data.get("expected_tail")
        if type(expected_tail) is not int or expected_tail < -1:
            raise _MutationRecoveryBlocked(
                "run_deletion_identity_invalid", "The durable deletion tail is invalid.")
        rd = self.run_root.resolve() / rid
        result = self._commands.begin_or_resume_deletion(
            rd, operation_id=_deletion_operation_id(key),
            expected_generation=generation, expected_seq=expected_tail)
        return _render_deletion_result(result, rid)

    def _control(self, name: str, rid: str, rd: Path) -> str:
        from looplab.events.types import EV_PAUSE, EV_RESUME, EV_RUN_ABORT
        etype, data, verb = {
            "finalize_run": (EV_RUN_ABORT, {"reason": "finalized"}, f"finalize run {rid} (stop + wrap up)"),
            "stop_run": (EV_PAUSE, {}, f"stop (freeze) run {rid}"),
            "resume_run": (EV_RESUME, {}, f"resume run {rid}"),
        }[name]
        blocked, formed_generation = self._gate(name, rid, rd, verb, scope={"run_id": rid})
        if blocked:
            return blocked
        with self._mutation_intent(
                name, rid, rd, {"event_type": etype, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, etype, data, idempotency_key=key, expected_generation=generation)
        return _render_command_result(record, name=name, run_id=rid, completed=verb)

    def _tool_extend_budget(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Raise a LIVE run's node/time budget by appending the same EV_BUDGET_EXTEND the UI writes."""
        import math

        from looplab.events.types import EV_BUDGET_EXTEND

        data: dict = {}
        for k in ("add_nodes", "max_seconds", "max_eval_seconds"):
            v = args.get(k)
            if v is None:
                continue
            try:
                data[k] = int(v) if k == "add_nodes" else float(v)
            except (TypeError, ValueError):
                return f"({k} must be a number)"
            if k != "add_nodes" and not math.isfinite(data[k]):
                return f"({k} must be a finite number — nan/inf would disable the budget)"
        if not data:
            return "(extend_budget needs at least one of add_nodes / max_seconds / max_eval_seconds)"
        if data.get("add_nodes", 1) <= 0:      # a negative/zero delta SHRINKS the budget, not extends
            return "(add_nodes must be a positive count of MORE experiment nodes)"
        blocked, formed_generation = self._gate(
            name, rid, rd, f"extend budget of {rid}: {data}",
            scope={"run_id": rid, **data})
        if blocked:
            return blocked
        with self._mutation_intent(
                name, rid, rd, {"event_type": EV_BUDGET_EXTEND, "data": data},
                command_backed=True,
                expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_BUDGET_EXTEND, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name=name, run_id=rid, completed=f"budget extended for {rid}: {data}")

    def _tool_set_directive(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Record a standing directive for a LIVE run (EV_HINT), gated like every other mutation."""
        from looplab.events.types import EV_HINT

        text = " ".join(str(args.get("text") or "").split())
        if not text:
            return "(set_directive needs a non-empty text)"
        blocked, formed_generation = self._gate(
            name, rid, rd, f"directive for {rid}: {text[:60]}",
            scope={"run_id": rid,
                   "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "replace": bool(args.get("replace"))})
        if blocked:
            return blocked
        data = {"text": text, "replace": bool(args.get("replace"))}
        with self._mutation_intent(
                name, rid, rd, {"event_type": EV_HINT, "data": data},
                command_backed=True,
                expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_HINT, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name=name, run_id=rid, completed=f"directive recorded for {rid}: {text[:80]!r}")

    def _tool_set_trust_gate(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Set the trust gate.

        The only settings verb that is NOT command-backed: it writes the event and mirrors
        `config.snapshot.json` directly, so a later RESUME re-enters with the new gate and the
        settings panel does not show a stale value. It stays this way until the trust gate joins the
        server's control registry, at which point it becomes a submit like the other two.

        The WRITE is `events/trust_gate.py::apply_trust_gate`, the same one the config PUT uses.
        This path used to spell its own and had drifted on all three of its properties — it appended
        unconditionally (so confirming a gate that already held grew the log by a row claiming a
        change nobody made), with no tail CAS and no writer lock. Only the refusal is phrased here.
        """
        from looplab.events.trust_gate import (
            GATE_WRITE_ALREADY_SET, GATE_WRITE_CONTENDED, TRUST_GATE_VALUES, apply_trust_gate,
        )
        from looplab.events.types import ASSISTANT_APPENDABLE, EV_TRUST_GATE_CHANGED

        # Invariant #1's assistant seam, declared at the site like its two thread-side siblings.
        assert EV_TRUST_GATE_CHANGED in ASSISTANT_APPENDABLE

        tg = str(args.get("trust_gate") or "").strip().lower()
        if tg not in TRUST_GATE_VALUES:
            return "(trust_gate must be audit | gate | block)"
        blocked, formed_generation = self._gate(
            name, rid, rd, f"set trust_gate={tg} for {rid}",
            scope={"run_id": rid, "trust_gate": tg})
        if blocked:
            return blocked
        with self._mutation_intent(
                name, rid, rd, {"trust_gate": tg}, command_backed=False,
                expected_generation=formed_generation) as (_key, generation):
            with self._commands.mutation_guard(
                    rd, "set the trust gate", expected_generation=generation) as rd:
                # Mirror the UI PUT /config path: the fold already applies the event, but also update
                # config.snapshot.json so a later RESUME re-enters with the new gate and the settings panel
                # doesn't show a stale value (the two mutation paths must not drift). Best-effort.
                snap = rd / "config.snapshot.json"
                if snap.exists():
                    # Global order is config -> events. Reset takes the same pair in that order,
                    # preventing an event->config/config->event deadlock while also closing the
                    # reset-marker race for this dual write.
                    with self.lifecycle().run_config_write_lock(snap):
                        outcome = apply_trust_gate(rd, tg, source="assistant")
                        if outcome != GATE_WRITE_CONTENDED:
                            try:
                                import json as _json
                                from looplab.core.atomicio import atomic_write_text
                                cfg = _json.loads(snap.read_text(encoding="utf-8"))
                                cfg["trust_gate"] = tg
                                atomic_write_text(snap, _json.dumps(cfg, indent=2))
                            except (OSError, ValueError):
                                pass
                else:
                    outcome = apply_trust_gate(rd, tg, source="assistant")
                if outcome == GATE_WRITE_CONTENDED:
                    # The refusal is phrased here, not in the shared writer: this surface answers an
                    # LLM, so it says what to do next rather than returning a status code.
                    return (f"(run {rid} changed while the trust gate was being saved — "
                            f"refresh and retry)")
                if outcome == GATE_WRITE_ALREADY_SET:
                    # Say it, rather than reporting a change that did not happen. The row is what a
                    # later audit reads; claiming one nobody made is the defect this closes.
                    return f"(trust_gate was already {tg} for {rid} — nothing recorded)"
                return f"(trust_gate set to {tg} for {rid})"

    def _reset_node(self, rid: str, rd: Path, args: dict) -> str:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        from looplab.events.types import EV_NODE_RESET
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(reset_node needs an integer node_id)"
        stage = str(args.get("stage") or "eval").strip()
        if not stage or len(stage) > 64:      # propose|implement|eval OR an eval-pipeline stage name
            return "(stage must be a non-empty stage name)"
        store = EventStore(rd / "events.jsonl")
        inspected_events = store.read_all()
        expected_tail = inspected_events[-1].seq if inspected_events else -1
        state = fold(inspected_events)
        node = state.nodes.get(nid)
        if node is None:
            return f"(no node #{nid} in {rid})"
        if node.tombstoned:
            return f"(node #{nid} in {rid} is tombstoned and cannot be reset)"
        generation = node.attempt
        blocked, formed_generation = self._gate(
            "reset_node", rid, rd, f"reset node #{nid} of {rid} from {stage}",
            scope={"run_id": rid, "node_id": nid, "generation": generation, "stage": stage})
        if blocked:
            return blocked
        # Permission can stay open while another control changes the node. Reject that stale scope
        # before handing the exact lifecycle generation to the command sequencer.
        if not _node_lifecycle_unchanged(
                store, node_id=nid, expected_tail=expected_tail, generation=generation):
            return f"(node #{nid} or run intent changed while awaiting permission — refresh and retry)"
        data = {"node_id": nid, "generation": generation, "from_stage": stage}
        with self._mutation_intent(
                "reset_node", rid, rd, {"event_type": EV_NODE_RESET, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_NODE_RESET, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name="reset_node", run_id=rid,
            completed=f"node #{nid} of {rid} re-run from {stage}")

    def _retag_node(self, rid: str, rd: Path, args: dict) -> str:
        # PART V (D): let the assistant re-tag ONE node's concepts — the operator's per-run concept edit,
        # now available to the operator's assistant. Reuses the existing EV_CONCEPT_TAG_EDITED control event
        # (folds to node_concepts with OPERATOR provenance, wins over authored/classifier tags), through the
        # same command funnel + generation fence + permission gate as reset_node. The server normalizes and
        # caps the concept ids, so submit raw and surface any 400/409 through the command result.
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        from looplab.events.types import EV_CONCEPT_TAG_EDITED
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(retag_node needs an integer node_id)"
        raw = args.get("concepts")
        if not isinstance(raw, list):
            return ("(retag_node needs a `concepts` list of axis/slug ids, "
                    'e.g. ["loss/contrastive", "regularization/r-drop"])')
        concepts = [str(c) for c in raw]
        store = EventStore(rd / "events.jsonl")
        inspected = store.read_all()
        expected_tail = inspected[-1].seq if inspected else -1
        node = fold(inspected).nodes.get(nid)
        if node is None:
            return f"(no node #{nid} in {rid})"
        if node.tombstoned:
            return f"(node #{nid} in {rid} is tombstoned and cannot be re-tagged)"
        node_gen = node.attempt
        blocked, formed_generation = self._gate(
            "retag_node", rid, rd, f"re-tag node #{nid} of {rid}",
            scope={"run_id": rid, "node_id": nid, "generation": node_gen, "concepts": concepts})
        if blocked:
            return blocked
        # Reject a stale subject that changed while the confirm card was open (same fence as reset_node).
        if not _node_lifecycle_unchanged(
                store, node_id=nid, expected_tail=expected_tail, generation=node_gen):
            return f"(node #{nid} or run intent changed while awaiting permission — refresh and retry)"
        data = {"node_id": nid, "node_generation": node_gen, "concepts": concepts}
        with self._mutation_intent(
                "retag_node", rid, rd, {"event_type": EV_CONCEPT_TAG_EDITED, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_CONCEPT_TAG_EDITED, data, idempotency_key=key,
                expected_generation=generation)
        return _render_command_result(
            record, name="retag_node", run_id=rid,
            completed=f"node #{nid} of {rid} re-tagged with {len(concepts)} concept(s)")

    def _set_run_concepts(self, rid: str, rd: Path, args: dict) -> str:
        # PART V (D): set a run's BASE concept set (EV_RUN_CONCEPTS, last-write-wins). The engine seeds this
        # once from the first node; the assistant can override/refine it. Run-scoped (no node fence); the
        # server normalizes/caps the ids. Nodes then author only deltas vs this base.
        from looplab.events.types import EV_RUN_CONCEPTS
        raw = args.get("concepts")
        if not isinstance(raw, list):
            return ("(set_run_concepts needs a `concepts` list of axis/slug ids, "
                    'e.g. ["model/transformer", "loss/contrastive"])')
        concepts = [str(c) for c in raw if str(c)]
        if not concepts:
            # An EMPTY base is indistinguishable from "never seeded", so while concept_run_base is on the
            # engine cadence would re-seed it from the first node and silently undo the clear. Replace the
            # base with a real set instead; to disable run-base authoring entirely, turn off concept_run_base.
            return ("(set_run_concepts needs at least one concept — an empty base is re-seeded by the engine. "
                    "Pass the base set you want, or disable concept_run_base to stop run-base authoring.)")
        blocked, formed_generation = self._gate(
            "set_run_concepts", rid, rd, f"set the base concepts of {rid}",
            scope={"run_id": rid, "concepts": concepts})
        if blocked:
            return blocked
        data = {"concepts": concepts}
        with self._mutation_intent(
                "set_run_concepts", rid, rd, {"event_type": EV_RUN_CONCEPTS, "data": data},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            record = self._commands.submit(
                rd, EV_RUN_CONCEPTS, data, idempotency_key=key, expected_generation=generation)
        return _render_command_result(
            record, name="set_run_concepts", run_id=rid,
            completed=f"run {rid} base concepts set ({len(concepts)})")

    def _delete_node(self, rid: str, rd: Path, args: dict) -> str:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(delete_node needs an integer node_id)"
        purge = bool(args.get("purge"))
        if self._live(rd):
            return f"(run {rid} is LIVE — stop it before physically rewriting its event log)"
        evp = rd / "events.jsonl"
        store = EventStore(evp)
        events = store.read_all()
        st = fold(events)
        if nid not in st.nodes:
            return f"(no node #{nid} in {rid})"
        subtree = _node_subtree(st, nid)
        expected_tail = events[-1].seq if events else -1
        verb = "PURGE (physical, irreversible)" if purge else "tombstone"
        blocked, formed_generation = self._gate(
            "delete_node", rid, rd, f"{verb} node(s) {sorted(subtree)} of {rid}",
            scope={"run_id": rid, "node_id": nid, "subtree": sorted(subtree), "purge": purge})
        if blocked:
            return blocked
        with self._mutation_intent(
                "delete_node", rid, rd,
                {"node_id": nid, "subtree": sorted(subtree), "purge": purge,
                 "expected_tail": expected_tail},
                command_backed=False, expected_generation=formed_generation) as (_key, generation):
            pass
        with self._commands.destructive_guard(
                rd, "delete node", expected_generation=generation) as canonical:
            if self._live(canonical):
                return f"(run {rid} is LIVE — stop it before physically rewriting its event log)"
            return self._commit_delete_node_snapshot(
                rid, canonical, nid, subtree, expected_tail, purge=purge)

    def _commit_delete_node_snapshot(self, rid: str, rd: Path, nid: int,
                                     subtree: set[int], expected_tail: int, *, purge: bool) -> str:
        from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, _interprocess_lock
        from looplab.events.replay import fold
        from looplab.events.types import ASSISTANT_APPENDABLE, EV_NODE_TOMBSTONED
        lifecycle = self.lifecycle()

        evp = rd / "events.jsonl"

        # The launch claim/Popen/child-lock gap is fenced only by the lifecycle lock. Acquire it after
        # approval, reject a fresh pending launch, then take engine.lock before the event-log CAS.
        with lifecycle.run_lifecycle_lock(rd):
            if (lifecycle.fresh_resume_launch_pending(rd)
                    or lifecycle.fresh_run_launch_pending(rd)):
                return f"(run {rid} is launching — retry delete after the engine settles)"
            if lifecycle.engine_alive(rd):
                return f"(run {rid} became LIVE while awaiting permission — stop it and retry)"
            if purge:
                return self._purge_node_snapshot(rid, rd, nid, subtree, expected_tail)
            with _interprocess_lock(rd / "engine.lock"):
                store = EventStore(evp)
                events = store.read_all()
                tail = events[-1].seq if events else -1
                state = fold(events)
                current_subtree = _node_subtree(state, nid) if nid in state.nodes else set()
                if current_subtree != subtree:
                    return (f"(delete scope changed while awaiting permission: approved "
                            f"{sorted(subtree)}, now {sorted(current_subtree)}; review and approve "
                            f"again)")
                if (tail != expected_tail or nid not in state.nodes
                        or (state.nodes[nid].tombstoned and not purge)):
                    return f"(run {rid} changed while awaiting permission — refresh and retry)"
                # Invariant #1's assistant seam, declared. This provider is neither the engine nor
                # a control intent, and `node_tombstoned` has no other writer in the tree — so the
                # exception is stated at the site, exactly as the two thread-side seams state theirs.
                assert EV_NODE_TOMBSTONED in ASSISTANT_APPENDABLE
                try:
                    store.append(
                        EV_NODE_TOMBSTONED, {"node_ids": sorted(subtree)},
                        expected_last_seq=expected_tail)
                except EventStoreConcurrencyError:
                    return f"(run {rid} changed before delete could commit — refresh and retry)"

        state = fold(EventStore(evp).read_all())
        live_left = sum(1 for node in state.nodes.values() if not node.tombstoned)
        return (f"(tombstoned node(s) {sorted(subtree)} of {rid} — logically deleted, log intact + "
                f"reversible; {live_left} live nodes left, best now #{state.best_node_id}. "
                f"Use purge=true for an irreversible physical compaction.)")

    # DEFERRED DECISION D-01 (docs/34): an irreversible multi-file transaction with NO durable
    # receipt, while its three siblings (reset, deletion, trace clear) all go through
    # `serve/durable_op.py::ReceiptProtocol`. A death between the event-log rewrite and the span
    # publish leaves a renumbered log whose `seq` no longer matches the spans sidecar, with nothing
    # on disk saying an operation was in flight. Adopting a receipt here needs answers this code
    # cannot give itself — who owns the operation id when an AGENT initiates it, and what recovery
    # should do with no operator in the loop. Read docs/34 before adding a fourth receipt protocol.
    def _purge_node_snapshot(self, rid: str, rd: Path, nid: int,
                             subtree: set[int], expected_tail: int) -> str:
        """Physically compact exactly the stopped tree snapshot the operator approved."""
        import json
        import shutil

        from looplab.core.atomicio import atomic_write_text
        from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
        from looplab.events.eventstore import EventStore, _interprocess_lock, iter_event_jsonl
        from looplab.events.replay import fold
        from looplab.events.span_index import invalidate, span_destructive_write_guard

        evp = rd / "events.jsonl"
        spans = rd / "spans.jsonl"
        # Lock order matches the engine (singleton first, event append second). If a resume won the
        # liveness-check race, wait for it to release engine.lock and then fail the tail CAS; if purge
        # wins, no child can enter while the source-of-truth logs are rewritten. The span-index guard
        # is the same third lock used by reset/archive: a cold trace read cannot publish offsets for
        # the pre-purge inode behind this rewrite.
        with (_interprocess_lock(rd / "engine.lock"),
              _interprocess_lock(Path(str(evp) + ".lock")),
              span_destructive_write_guard(spans, required=True)):
            self._commands._reject_unresolved_reset(rd, "purge nodes")
            source_store = EventStore(evp)
            events = source_store.read_all()
            source_bytes = evp.read_bytes()
            torn_nonblank_tail = bool(
                source_bytes
                and not source_bytes.endswith(b"\n")
                and source_bytes.rsplit(b"\n", 1)[-1].strip()
            )
            # Purge is an irreversible compaction, not an implicit log repair. The historical raw
            # parser failed before rewriting any malformed complete row; preserve that posture for a
            # corrupt batch and additionally refuse a torn nonblank tail that EventStore legitimately
            # hides from ordinary replay.
            if source_store.divergence is not None or torn_nonblank_tail:
                return (
                    f"(run {rid} event log has an invalid or torn tail — refusing irreversible "
                    "purge; repair or restore the log first)"
                )
            actual_tail = events[-1].seq if events else -1
            state = fold(events)
            if actual_tail != expected_tail or nid not in state.nodes:
                return f"(run {rid} changed while awaiting permission — refresh and retry)"

            current_subtree = _node_subtree(state, nid)
            if current_subtree != subtree:
                return (f"(delete scope changed while awaiting permission: approved "
                        f"{sorted(subtree)}, now {sorted(current_subtree)}; review and approve again)")

            # Work over the logical Events already decoded above. ``append_many`` is stored as one
            # physical batch envelope; filtering physical rows would retain the whole transaction when
            # only one nested member names the purged node. Rewriting logical rows also removes the
            # internal storage wrapper while preserving every surviving event and sequence.
            recs = list(iter_event_jsonl(evp))
            kept = [record for record in recs
                    if not (isinstance(record.get("data"), dict)
                            and record["data"].get("node_id") in subtree)]
            # RENUMBER to a dense 0..N-1 run. Filtering alone left a seq GAP after every purged
            # event — the trailing `pause`, later sibling nodes — and the event store's dense fence
            # (`event_sequence_continues`: "no legitimate workflow produces a monotonic gap") reads
            # that as CORRUPTION: `read_all` silently drops every surviving event past the gap, and
            # the next append or resume raises EventLogCorruptionError. A purge could brick the run
            # it was cleaning. This whole path is already a full rewrite of the log (the backup taken
            # below is what makes it recoverable), so renumbering costs nothing extra, and seq is
            # POSITIONAL identity — nothing in the fold keys off an absolute value.
            for position, record in enumerate(kept):
                record["seq"] = position

            # Trace rows can contain credentials, prompts and host paths. Use the same descriptor-
            # first, root-aware streaming filter as HTTP trace-clear instead of following a link or
            # materialising a multi-GB sidecar. Besides explicit per-span node ids, this removes
            # unstamped children from legacy traces whose ROOT belongs to the purged subtree. Invalid
            # complete rows and a torn EOF remain byte-for-byte, so purge never turns an uncommitted
            # crash suffix into a committed JSONL record.
            trace_rewrite = self._trace_rewrite
            if trace_rewrite is None:
                return (
                    f"(run {rid} trace rewrite service is unavailable — refusing irreversible "
                    "purge; retry through the owner assistant service)"
                )
            prepared_trace = None
            try:
                prepared_trace = trace_rewrite.prepare_filtered_snapshot(spans, subtree)
                current_trace = trace_rewrite.digest_snapshot(spans)
            except Exception as exc:  # noqa: BLE001 - soft-fail this assistant tool before writes
                if prepared_trace is not None:
                    prepared_trace.cleanup()
                detail = getattr(exc, "detail", None)
                code = detail.get("code") if isinstance(detail, dict) else type(exc).__name__
                return (
                    f"(run {rid} trace sidecar is unavailable or unsafe ({code}) — refusing "
                    "irreversible purge; restore the private run-owned spans.jsonl first)"
                )
            if current_trace != prepared_trace.source:
                prepared_trace.cleanup()
                return (
                    f"(run {rid} trace sidecar changed while purge was prepared — refusing "
                    "irreversible purge; refresh and retry)"
                )

            # APPEND-ONLY backups: the name used to be keyed only by the root node id, so a second
            # purge of the SAME nid — a scope-change retry, or an id reused after a purge on resume —
            # silently overwrote the safety receipt for an IRREVERSIBLE operation. Find the first
            # free suffix instead; the unnumbered name stays as-is so existing backups keep working.
            _backup = rd / f"events.jsonl.bak-del{nid}"
            _n = 2
            while _backup.exists():
                _backup = rd / f"events.jsonl.bak-del{nid}.{_n}"
                _n += 1

            # Eviction is harmless if a later source write fails, while doing it first makes a
            # conflicting directory/unremovable projection fail before the irreversible event-log
            # rewrite. The guard prevents a reader from rebuilding either projection in this gap.
            try:
                invalidate(spans)
                (rd / "spans.index.jsonl").unlink(missing_ok=True)
                (rd / SPAN_APPEND_JOURNAL_NAME).unlink(missing_ok=True)
            except OSError:
                prepared_trace.cleanup()
                return (
                    f"(run {rid} trace projections could not be retired — refusing irreversible "
                    "purge; repair the run-owned trace sidecars first)"
                )
            try:
                shutil.copy(evp, _backup)
                atomic_write_text(evp, "".join(json.dumps(record) + "\n" for record in kept))
                if prepared_trace.temporary is not None:
                    trace_rewrite.publish_prepared_snapshot(prepared_trace, spans)
                for deleted_id in subtree:
                    shutil.rmtree(rd / "nodes" / f"node_{deleted_id}", ignore_errors=True)
            finally:
                prepared_trace.cleanup()

        remaining = fold(EventStore(evp).read_all())
        broken = sorted({parent for node in remaining.nodes.values() for parent in node.parent_ids
                         if parent not in remaining.nodes})
        return (f"(deleted node(s) {sorted(subtree)} from {rid}; {len(remaining.nodes)} nodes left, "
                f"best now #{remaining.best_node_id}, broken parent links: {broken or 'none'}. "
                f"Backup: events.jsonl.bak-del{nid})")

    def _delete_run(self, rid: str, rd: Path) -> str:
        if self._live(rd):
            return f"(run {rid} is LIVE — stop it first before deleting)"
        from looplab.events.eventstore import EventStore

        events = EventStore(rd / "events.jsonl").read_all()
        expected_tail = events[-1].seq if events else -1
        blocked, formed_generation = self._gate(
            "delete_run", rid, rd, f"DELETE the entire run {rid} (irreversible)",
            scope={"run_id": rid, "expected_tail": expected_tail})
        if blocked:
            return blocked
        if not self._commands.durable_deletion_available:
            raise _MutationRecoveryBlocked(
                "run_deletion_service_unavailable",
                "Durable run deletion is unavailable; no run files were modified.")
        with self._mutation_intent(
                "delete_run", rid, rd, {"expected_tail": expected_tail},
                command_backed=True, expected_generation=formed_generation) as (key, generation):
            result = self._commands.begin_or_resume_deletion(
                rd, operation_id=_deletion_operation_id(key),
                expected_generation=generation, expected_seq=expected_tail)
            return _render_deletion_result(result, rid)
