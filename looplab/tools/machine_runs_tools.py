"""Cross-run introspection tools for the assistant (ADR-7 tool protocol).

Where `RunTools` reads the ONE live run bound to it and `SiblingRunTools` reads other runs of the
SAME task, `MachineRunsTools` gives the general-purpose assistant a view over EVERY run on this machine —
so it can reference an existing run, report which ones are live, and read one in detail before
steering or fixing it. Same `.specs()`/`.execute()` shape as the other providers; every `execute`
returns a string and soft-fails (a junk tool call must never crash the loop).

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
from typing import Callable, Optional

from looplab.events import digest
from looplab.core.models import RunState
from looplab.tools.run_tools import RunTools
from looplab.tools._base import RESULT_CAP, fn_spec
from looplab.tools._runcache import RunStateCache

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
        except Exception:  # accepted is durable; a failed observation is not command failure
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
        except Exception as exc:  # never expose internals or retry a possibly accepted submission
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

    @contextmanager
    def destructive_guard(self, rd: Path, operation: str, *, expected_generation: str):
        """Use the server's per-run command sequencer when this provider runs in the UI server."""
        guard = getattr(self.service, "destructive_guard", None)
        if callable(guard):
            with guard(rd, operation) as canonical:
                self._require_generation(canonical, expected_generation)
                yield canonical
            return
        # Standalone/unit-tool use has no AppState command coordinator. Preserve the historical tool
        # surface there; the live check below remains mandatory and is re-run immediately before I/O.
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

    def retire_start_record_for_delete(self, rd: Path) -> Optional[dict]:
        """Retire the exact root-sidecar start owner before removing a run directory.

        Keyed Genesis start records deliberately live outside ``rd`` so they survive partial
        materialization and lost responses.  A storage-level assistant delete must therefore retire
        that sidecar explicitly; otherwise recreating the same run id can remain permanently blocked
        (or an old same-key start can be reported as successful after its run was deleted).
        """
        load = getattr(self.service, "load_start_record", None)
        if not callable(load):
            return None
        record = load(rd)
        if record is None:
            return None
        retire = getattr(self.service, "retire_start_record", None)
        save = getattr(self.service, "save_start_record", None)
        if not callable(retire) or not callable(save):
            raise _MutationRecoveryBlocked(
                "run_start_record_unavailable",
                "The run start owner cannot be retired safely; the run was not deleted.")
        start_id = record.get("id") if isinstance(record, dict) else None
        if not isinstance(start_id, str) or not start_id or not retire(rd, start_id):
            raise _MutationRecoveryBlocked(
                "run_start_record_changed",
                "The run start owner changed before deletion; the run was not deleted.")
        return dict(record)

    def restore_start_record_after_failed_delete(self, rd: Path, record: Optional[dict]) -> None:
        """Restore an exactly-retired start sidecar when directory deletion was incomplete."""
        if record is None:
            return
        save = getattr(self.service, "save_start_record", None)
        if not callable(save):
            raise _MutationRecoveryBlocked(
                "run_start_record_lost",
                "Run deletion was incomplete and its start owner could not be restored.")
        save(rd, record)


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


def _render_conversation(convo: dict, run_id, nid, stage: Optional[str], max_chars: int) -> str:
    """Render `traceview.build_conversation` output as a readable linear thread. One block per stage
    (create_node / evaluate / …); within a stage, requests show the prompt, generations show
    thinking + output + which tools were called, tool turns show input→output. Filtered to one stage
    when `stage` is given (substring match on its label). Bounded to a generous trace budget."""
    stages = convo.get("stages") or []
    if stage:
        s = str(stage).lower()
        stages = [st for st in stages if s in str(st.get("label") or "").lower()]
    if not stages:
        which = f" matching {stage!r}" if stage else ""
        return f"(run {run_id} node #{nid}: no trace stages{which} recorded)"
    lines = [f"run {run_id} · node #{nid} · trace ({len(stages)} stage(s)):"]
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


class MachineRunsTools:
    """Read-only view over ALL runs under the run-root (for the assistant)."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 max_chars: int = 3500):
        self.run_root = Path(run_root)
        self.alive_fn = alive_fn
        self.max_chars = max_chars
        # Traversal-guarded, (size, mtime)-fingerprinted fold cache — shared with SiblingRunTools.
        self._runs = RunStateCache(self.run_root)
        self._reader = RunTools(max_chars=max_chars)

    # MachineRunsTools is not bound to a single run; accept bind_state for CompositeTools symmetry (no-op).
    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_runs",
                "List EVERY LoopLab run on this machine with its goal, phase, best metric, node count "
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
                "Read one experiment's AGENT TRACE as a linear, de-duplicated conversation: the "
                "system+user request once per sub-loop, then each LLM generation's reasoning + output "
                "and the tools it called, interleaved with tool results. This is the full train of "
                "thought that produced the node. Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "stage": {"type": "string", "description": "optional: only the stage whose label "
                                                            "contains this text (e.g. 'repair')"}},
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
                                        args.get("stage"))
            return f"(unknown tool: {name})"
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            return f"(tool error: {e})"

    # --- machine-readable summaries (also reused by the /api/assistant run-ref expansion) ------------
    def summaries(self, only_live: bool = False) -> list[dict]:
        """Structured per-run summary for EVERY run (used by the tool AND by @run-mention expansion)."""
        out = []
        for rid in self._run_ids():
            st = self._state(rid)
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

    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        return self._runs.state(run_id)

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
                         f"{r['nodes']} nodes · {r['phase']}{live}")
        return f"{len(lines)} run(s):\n" + "\n".join(lines)

    def _read_run(self, run_id, sort, limit) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        best = st.best()
        live = self._alive(str(run_id))
        head = (f"run {run_id} · goal: {st.goal or st.task_id} · direction={st.direction} · "
                f"phase={'finished' if st.finished else ('live' if live else 'idle')} · "
                f"{len(st.nodes)} nodes · best={digest.fmt_num(digest.node_metric(best)) if best else '—'}"
                + (f" (#{best.id})" if best else ""))
        self._reader.bind_state(st, None)
        listing = self._reader.execute("list_experiments",
                                       {"sort": sort or "best", "limit": int(limit or 8)})
        return head + "\n" + listing

    def _read_experiment(self, run_id, nid: int, trials_arg=None) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"run {run_id} · " + self._reader.execute(
            "read_experiment", {"node_id": nid, "trials": trials_arg})

    def _read_logs(self, run_id, nid: int) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"run {run_id} · " + self._reader.execute("read_logs", {"node_id": nid})

    def _read_trace(self, run_id, nid: int, stage: Optional[str] = None) -> str:
        """The node's agent trace as a linear, de-duplicated conversation. Reuses the SAME
        `build_conversation` projection the Web UI's Trace tab shows (so the assistant reads exactly
        what the human sees), rendered to text and bounded to `max_chars`."""
        rd = self._safe_dir(run_id)
        st = self._state(run_id)
        if rd is None or st is None:
            return f"(no such run: {run_id!r})"
        from looplab.events.traceview import build_conversation, load_spans
        spans_path = rd / "spans.jsonl"
        if not spans_path.exists():
            return (f"(run {run_id} has no spans.jsonl — no agent trace was recorded. This run may "
                    "predate tracing, or ran with tracing off.)")
        try:
            convo = build_conversation(st, load_spans(spans_path), nid)
        except Exception as e:  # noqa: BLE001 — an unexpected hand-edited/I/O failure must soft-fail
            return f"(could not read trace: {e})"  # and never terminate the agent tool loop
        return _render_conversation(convo, run_id, nid, stage, self.max_chars)


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
                "model/max_nodes/etc. in `settings`. The task is VALIDATED before it becomes a card — an "
                "invalid one is bounced back to you to fix.\n"
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
    only in `auto`. Destructive edits (delete node/run) additionally REFUSE while the engine is live —
    the engine is the sole writer of events.jsonl, so rewriting it under a live one would corrupt it."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 mode: str = "plan", approver: Optional[Callable] = None, *,
                 command_service=None, command_key_namespace: str = "",
                 mutation_journal_path=None, mutation_recovery: bool = False):
        self.run_root = Path(run_root)
        self.alive_fn = alive_fn
        self.mode = mode
        self.approver = approver
        self._commands = _RunCommandAdapter(
            command_service, key_namespace=command_key_namespace)
        self._mutation_fence = (_TurnMutationFence(
            Path(mutation_journal_path), command_key_namespace, recovering=mutation_recovery)
            if mutation_journal_path is not None and command_key_namespace else None)

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
        rid = str(run_id or "").strip().strip("/")
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
        from looplab.tools.perm_modes import approval_allows, decide_action
        action = {"tool": name, "tool_kind": "run_control", "label": f"{name} {rid}",
                  "verb": verb, "preview": f"{name}({rid})", "run_id": rid,
                  "scope": dict(scope or {"run_id": rid})}
        d = decide_action(self.mode, action)
        if d == "deny":
            return ("(run control is disabled in read-only plan mode — switch to "
                    "default/acceptEdits/auto.)", None)
        generation = (None if self._mutation_fence is not None and self._mutation_fence.recovering
                      else self._commands.run_generation(rd))
        if d == "ask":
            verdict = self.approver(action) or "deny" if self.approver else "deny"
            if not approval_allows(verdict):
                return f"(declined by the user: {name} {rid})", generation
        return None, generation

    def _live(self, rd: Path) -> bool:
        """Is a run's engine actively writing its log? The flock probe is primary, but on FUSE / NFS / S3
        mounts flock can wrongly report "not live" — so ALSO trip on a fresh-write backstop: a run that
        is neither paused nor finished AND whose events.jsonl was appended in the last 30s is treated as
        live (a running engine is the sole writer and appends constantly). This gates the destructive
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
            if name in ("extend_budget", "set_directive", "set_trust_gate"):
                return self._settings(name, rid, rd, args)
            if name == "delete_node":
                return self._delete_node(rid, rd, args)
            if name == "delete_run":
                return self._delete_run(rid, rd)
        except _MutationRecoveryBlocked as e:
            return f"(run mutation blocked: code={e.code}; {e})"
        except Exception as e:  # noqa: BLE001 — a tool error must never crash the loop
            return f"(tool error in {name}: {e})"
        return f"(unknown tool: {name})"

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

    def _resume_run(self, rid: str, rd: Path) -> str:
        """Durably hand resume to the singleton owner/claim funnel; never reopen before lock ownership."""
        from looplab.events.eventstore import EventStore, EventStoreConcurrencyError
        from looplab.events.types import EV_RESUME_REQUESTED
        from looplab.serve.engine_proc import (
            _claim_and_spawn_resume, _engine_alive, _resolve_task_file, _run_lifecycle_lock)

        with _run_lifecycle_lock(rd):
            task_file = _resolve_task_file(rd)
            if task_file is None:
                return (f"(run {rid} is not self-describing; no task.snapshot.json/ui_meta task file — "
                        "resume it from the UI with its task definition)")
            store = EventStore(rd / "events.jsonl")
            for _attempt in range(8):
                events = store.read_all()
                tail = events[-1].seq if events else -1
                try:
                    store.append(
                        EV_RESUME_REQUESTED, {"mode": "resume"},
                        expected_last_seq=tail)
                    break
                except EventStoreConcurrencyError:
                    continue
            else:
                return f"(run {rid} changed repeatedly — retry resume)"

        cli_args = ["resume", str(rd), "--task-file", str(task_file)]
        was_alive = _engine_alive(rd)
        spawned = _claim_and_spawn_resume(rd, cli_args, wait_on_alive=True)
        if was_alive and not spawned:
            return f"(resume recorded for {rid} — current owner will serve it or hand off after exit)"
        return (f"(resume recorded for {rid} — "
                f"{'engine launched' if spawned else 'durable launch pending'})")

    def _settings(self, name: str, rid: str, rd: Path, args: dict) -> str:
        """Change an allow-listed LIVE run setting by appending the matching control/config event the UI
        writes (budget extension, a standing directive, or the trust gate). Gated exactly like the other
        mutations. Command-backed settings use the server's engine policy and postcondition; the legacy
        trust-gate path remains a direct event + snapshot update until it joins that control registry."""
        import math
        from looplab.events.eventstore import EventStore
        from looplab.events.types import EV_BUDGET_EXTEND, EV_HINT, EV_TRUST_GATE_CHANGED
        if name == "extend_budget":
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
        if name == "set_directive":
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
        if name == "set_trust_gate":
            tg = str(args.get("trust_gate") or "").strip().lower()
            if tg not in ("audit", "gate", "block"):
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
                    store = EventStore(rd / "events.jsonl")
                    store.append(EV_TRUST_GATE_CHANGED, {"trust_gate": tg, "source": "assistant"})
                    # Mirror the UI PUT /config path: the fold already applies the event, but also update
                    # config.snapshot.json so a later RESUME re-enters with the new gate and the settings panel
                    # doesn't show a stale value (the two mutation paths must not drift). Best-effort.
                    snap = rd / "config.snapshot.json"
                    if snap.exists():
                        try:
                            import json as _json
                            from looplab.core.atomicio import atomic_write_text
                            cfg = _json.loads(snap.read_text(encoding="utf-8"))
                            cfg["trust_gate"] = tg
                            atomic_write_text(snap, _json.dumps(cfg, indent=2))
                        except (OSError, ValueError):
                            pass
                    return f"(trust_gate set to {tg} for {rid})"
        return f"(unknown settings tool: {name})"

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
        latest_events = store.read_all()
        latest_tail = latest_events[-1].seq if latest_events else -1
        latest = fold(latest_events).nodes.get(nid)
        if (latest_tail != expected_tail or latest is None or latest.tombstoned
                or latest.attempt != generation):
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
        latest = store.read_all()
        latest_tail = latest[-1].seq if latest else -1
        fresh = fold(latest).nodes.get(nid)
        if (latest_tail != expected_tail or fresh is None or fresh.tombstoned
                or fresh.attempt != node_gen):
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
            return f"(run {rid} is LIVE — stop it first; the engine is the sole writer of its log)"
        evp = rd / "events.jsonl"
        store = EventStore(evp)
        events = store.read_all()
        st = fold(events)
        if nid not in st.nodes:
            return f"(no node #{nid} in {rid})"
        # The node AND every descendant (deleting a node alone would orphan its children's parent links).
        def _subtree(state):
            found = {nid}
            changed = True
            while changed:
                changed = False
                for node in state.nodes.values():
                    if node.id not in found and any(p in found for p in node.parent_ids):
                        found.add(node.id)
                        changed = True
            return found

        subtree = _subtree(st)
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
                return f"(run {rid} is LIVE — stop it first; the engine is the sole writer of its log)"
            return self._commit_delete_node_snapshot(
                rid, canonical, nid, subtree, expected_tail, purge=purge)

    def _commit_delete_node_snapshot(self, rid: str, rd: Path, nid: int,
                                     subtree: set[int], expected_tail: int, *, purge: bool) -> str:
        from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, _interprocess_lock
        from looplab.events.replay import fold
        from looplab.events.types import EV_NODE_TOMBSTONED
        from looplab.serve.engine_proc import (
            _engine_alive, _fresh_resume_launch_pending, _fresh_run_launch_pending,
            _run_lifecycle_lock)

        evp = rd / "events.jsonl"

        def _subtree(state):
            found = {nid}
            changed = True
            while changed:
                changed = False
                for node in state.nodes.values():
                    if node.id not in found and any(parent in found for parent in node.parent_ids):
                        found.add(node.id)
                        changed = True
            return found

        # The launch claim/Popen/child-lock gap is fenced only by the lifecycle lock. Acquire it after
        # approval, reject a fresh pending launch, then take engine.lock before the event-log CAS.
        with _run_lifecycle_lock(rd):
            if _fresh_resume_launch_pending(rd) or _fresh_run_launch_pending(rd):
                return f"(run {rid} is launching — retry delete after the engine settles)"
            if _engine_alive(rd):
                return f"(run {rid} became LIVE while awaiting permission — stop it and retry)"
            if purge:
                return self._purge_node_snapshot(rid, rd, nid, subtree, expected_tail)
            with _interprocess_lock(rd / "engine.lock"):
                store = EventStore(evp)
                events = store.read_all()
                tail = events[-1].seq if events else -1
                state = fold(events)
                current_subtree = _subtree(state) if nid in state.nodes else set()
                if current_subtree != subtree:
                    return (f"(delete scope changed while awaiting permission: approved "
                            f"{sorted(subtree)}, now {sorted(current_subtree)}; review and approve "
                            f"again)")
                if (tail != expected_tail or nid not in state.nodes
                        or (state.nodes[nid].tombstoned and not purge)):
                    return f"(run {rid} changed while awaiting permission — refresh and retry)"
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

    def _purge_node_snapshot(self, rid: str, rd: Path, nid: int,
                             subtree: set[int], expected_tail: int) -> str:
        """Physically compact exactly the stopped tree snapshot the operator approved."""
        import json
        import shutil

        from looplab.core.atomicio import atomic_write_text
        from looplab.events.eventstore import EventStore, _interprocess_lock, iter_event_jsonl
        from looplab.events.replay import fold

        evp = rd / "events.jsonl"
        # Lock order matches the engine (singleton first, event append second). If a resume won the
        # liveness-check race, wait for it to release engine.lock and then fail the tail CAS; if purge
        # wins, no child can enter while the source-of-truth logs are rewritten.
        with (_interprocess_lock(rd / "engine.lock"),
              _interprocess_lock(Path(str(evp) + ".lock"))):
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

            current_subtree = {nid}
            changed = True
            while changed:
                changed = False
                for node in state.nodes.values():
                    if (node.id not in current_subtree
                            and any(parent in current_subtree for parent in node.parent_ids)):
                        current_subtree.add(node.id)
                        changed = True
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

            spans = rd / "spans.jsonl"
            kept_spans = None
            if spans.exists():
                def _span_node(line):
                    try:
                        return (json.loads(line).get("attributes") or {}).get("node_id")
                    except (ValueError, TypeError, AttributeError):
                        return None   # torn or non-object JSON: unknown node, so retain the line

                kept_spans = [line for line in spans.read_text("utf-8").splitlines()
                              if line.strip() and _span_node(line) not in subtree]

            shutil.copy(evp, rd / f"events.jsonl.bak-del{nid}")
            atomic_write_text(evp, "".join(json.dumps(record) + "\n" for record in kept))
            if kept_spans is not None:
                atomic_write_text(spans, "".join(line + "\n" for line in kept_spans))
            for deleted_id in subtree:
                shutil.rmtree(rd / "nodes" / f"node_{deleted_id}", ignore_errors=True)

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
        with self._mutation_intent(
                "delete_run", rid, rd, {"expected_tail": expected_tail},
                command_backed=False, expected_generation=formed_generation) as (_key, generation):
            pass
        with self._commands.destructive_guard(
                rd, "delete run", expected_generation=generation) as canonical:
            if self._live(canonical):
                return f"(run {rid} is LIVE — stop it first before deleting)"
            return self._delete_run_snapshot(rid, canonical, expected_tail)

    def _delete_run_snapshot(self, rid: str, rd: Path, expected_tail: int) -> str:
        """Delete only the stopped run snapshot approved before a possibly-slow permission prompt."""
        from looplab.serve.engine_proc import (
            _engine_alive, _fresh_resume_launch_pending, _fresh_run_launch_pending,
            _run_lifecycle_lock)

        with _run_lifecycle_lock(rd):
            if _fresh_resume_launch_pending(rd) or _fresh_run_launch_pending(rd):
                return f"(run {rid} is launching — retry delete after the engine settles)"
            # Mirror the node-delete guard (`_commit_delete_node_snapshot`): a directly-launched engine
            # (`looplab run/resume`) records no resume launch-claim, so `_fresh_resume_launch_pending` is
            # False for it. Without this check, an approve-after-the-engine-went-live delete would call
            # `_delete_run_snapshot_locked`, whose BLOCKING `flock(engine.lock)` waits for the engine to
            # exit while holding the lifecycle lock — hanging the call and cascading to every other
            # lifecycle op on this run. Refuse cleanly instead.
            if _engine_alive(rd):
                return f"(run {rid} became LIVE while awaiting permission — stop it and retry)"
            return self._delete_run_snapshot_locked(rid, rd, expected_tail)

    def _delete_run_snapshot_locked(self, rid: str, rd: Path, expected_tail: int) -> str:
        import shutil

        from looplab.events.eventstore import EventStore, _interprocess_lock

        event_path = rd / "events.jsonl"
        engine_lock = rd / "engine.lock"
        event_lock = Path(str(event_path) + ".lock")
        # Holding engine.lock makes a racing CLI's non-blocking singleton acquisition fail. Holding
        # the event lock closes the UI-control append race. Remove source-of-truth/artifacts while both
        # are held, keeping the two open lock files until their contexts release on Windows.
        retired_start_record = None
        with _interprocess_lock(engine_lock), _interprocess_lock(event_lock):
            if not event_path.exists():
                return f"(run {rid} changed before delete could commit — refresh and retry)"
            events = EventStore(event_path).read_all()
            actual_tail = events[-1].seq if events else -1
            if actual_tail != expected_tail:
                return f"(run {rid} changed while awaiting permission — refresh and retry)"
            # This root-sidecar is outside ``rd`` and therefore would survive the directory delete.
            # The outer destructive guard still owns the command sequencer here, matching the HTTP
            # delete route's start-record retirement order.
            retired_start_record = self._commands.retire_start_record_for_delete(rd)
            try:
                for child in list(rd.iterdir()):
                    if child in (engine_lock, event_lock):
                        continue
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except FileNotFoundError:
                            pass
            except Exception:
                self._commands.restore_start_record_after_failed_delete(rd, retired_start_record)
                raise

        for lock_path in (event_lock, engine_lock):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
        try:
            rd.rmdir()
        except OSError:
            shutil.rmtree(rd, ignore_errors=True)
        if rd.exists():
            self._commands.restore_start_record_after_failed_delete(rd, retired_start_record)
            return f"(run {rid} delete was incomplete — inspect {rd})"
        return f"(deleted run {rid} and all its artifacts)"
