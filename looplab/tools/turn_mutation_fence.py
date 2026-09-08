"""The durable, ordered mutation journal one assistant user turn writes before it touches a run.

Split out of `machine_runs_tools.py` on 2026-09-08 (doc 25 TO-02): that module was a 1,988-line
god-module holding this fence, the run-command adapter and three unrelated providers. Nothing here
knows about tools or providers — it is the crash-recovery vocabulary the mutating provider composes
(`run_control_tools.py`), and the reason it is its own file is that a fence read by a recovered turn
is easier to reason about when the only thing in front of you is the fence.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional


_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


class _MutationRecoveryBlocked(RuntimeError):
    """Fail-closed signal for a mutation that a recovered assistant turn may not issue."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _exact_run_generation(value: object) -> str:
    if not isinstance(value, str) or _RUN_GENERATION_RE.fullmatch(value) is None:
        raise _MutationRecoveryBlocked(
            "run_generation_unavailable",
            "The run generation is missing or invalid; no run mutation was attempted.")
    return value


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
