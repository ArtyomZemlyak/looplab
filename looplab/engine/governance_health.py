"""Strict health boundary for append-only operator-governance ledgers.

Governance rows change canonical concept identity and which claims are allowed into live
cross-run projections.  They therefore cannot use the lenient mutable-store reader: skipping a
damaged line would silently turn an unknown policy state into a different, apparently exact one.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import TypeVar

_ProjectionT = TypeVar("_ProjectionT")

_PUBLIC_LEDGERS = frozenset((
    "concept_aliases", "concept_splits", "claim_decisions", "concept_governance",
    "concept_curation", "claim_curation", "task_facets_curation",
))
_PUBLIC_REASONS = frozenset((
    "storage_unreadable", "torn_tail", "blank_row", "malformed_json", "non_object",
    "unsupported_schema", "unknown_action", "invalid_record", "duplicate_action_id",
    "invalid_revision", "revision_mismatch", "revision_collision", "identity_cycle",
))


class GovernanceLedgerUnavailable(RuntimeError):
    """A governance ledger cannot be projected without guessing operator intent.

    ``reason`` and ``ledger`` are closed, non-content-bearing values so API/CLI/tool boundaries
    can report the condition without reflecting a poisoned row, filesystem path, or parser text.
    """

    def __init__(self, ledger: str, reason: str, *, line: int | None = None):
        # Enforce the content-free public vocabulary here rather than relying on every future
        # caller to remember that exception attributes cross API/CLI/tool boundaries.
        self.ledger = str(ledger) if ledger in _PUBLIC_LEDGERS else "concept_governance"
        self.reason = str(reason) if reason in _PUBLIC_REASONS else "invalid_record"
        self.line = line
        super().__init__(
            f"{self.ledger} governance ledger unavailable ({self.reason}); operator repair required"
        )

    def public_receipt(self) -> dict:
        return {
            "v": 1,
            "status": "unavailable",
            "complete": False,
            "code": "governance_ledger_unavailable",
            "ledger": self.ledger,
            "reason": self.reason,
        }


def _reject_json_constant(_value: str):
    raise ValueError("non-standard JSON constant")


def _strict_json_object(pairs):
    """Build an object only when every member name is unique, including nested objects."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON object member")
        out[key] = value
    return out


def read_governance_rows(
        path: Path, *, ledger: str,
        validate: Callable[[dict], str | None]) -> list[dict]:
    """Read one operator ledger only when every physical row is durable and understood.

    A final line without a newline is a torn append, not an ignorable tail: it may be a purge,
    clear, split, reject, or pin.  Likewise, malformed/non-object/future rows cannot be skipped
    because their omission changes last-write-wins meaning.  The exception deliberately exposes
    only a stable reason class, never raw bytes.
    """
    if not path.exists():
        return []
    try:
        raw_file = path.read_bytes()
    except OSError as exc:
        raise GovernanceLedgerUnavailable(ledger, "storage_unreadable") from exc
    if not raw_file:
        return []

    rows: list[dict] = []
    for line_number, raw_line in enumerate(raw_file.splitlines(keepends=True), start=1):
        # CODEX AGENT: an un-terminated governance tail has unknown semantics. Appending a newline
        # and a new action must not magically turn that unknown operator decision into a skipped row.
        if not raw_line.endswith((b"\n", b"\r")):
            raise GovernanceLedgerUnavailable(ledger, "torn_tail", line=line_number)
        payload = raw_line.rstrip(b"\r\n")
        if not payload.strip():
            raise GovernanceLedgerUnavailable(ledger, "blank_row", line=line_number)
        try:
            text = payload.decode("utf-8", errors="strict")
            row = json.loads(
                text, parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise GovernanceLedgerUnavailable(ledger, "malformed_json", line=line_number) from exc
        if not isinstance(row, dict):
            raise GovernanceLedgerUnavailable(ledger, "non_object", line=line_number)
        reason = validate(row)
        if reason:
            raise GovernanceLedgerUnavailable(ledger, reason, line=line_number)
        rows.append(row)
    return rows


def validate_revision_fields(row: dict) -> str | None:
    """Validate optional writer-owned revision fields without requiring them on legacy rows."""
    for field in ("revision", "governance_revision"):
        if field not in row:
            continue
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return "invalid_revision"
    return None


def validate_optional_text(row: dict, field: str, maximum: int) -> str | None:
    if field not in row:
        return None
    value = row[field]
    if not isinstance(value, str) or len(value) > maximum:
        return "invalid_record"
    return None


def validate_action_ids(rows: list[dict], *, ledger: str) -> None:
    """Reject repeated physical action ids, including exact duplicates.

    A healthy locked writer returns an existing receipt instead of appending a retry.  Therefore a
    repeated id is either a collision or evidence of an out-of-contract writer; accepting either
    would make revision/CAS meaning depend on which duplicate the reader happened to retain.
    """
    seen: set[str] = set()
    for line_number, row in enumerate(rows, start=1):
        action_id = row.get("action_id")
        if not action_id:
            continue
        if action_id in seen:
            raise GovernanceLedgerUnavailable(
                ledger, "duplicate_action_id", line=line_number)
        seen.add(action_id)


def validate_local_revisions(rows: list[dict], *, ledger: str) -> None:
    """Explicit revisions must agree with their physical append position.

    Rows predating revision receipts remain valid. Once present, an explicit revision is writer
    authority and cannot be silently repaired with ``len(valid_rows)``.
    """
    for position, row in enumerate(rows, start=1):
        if "revision" in row and row["revision"] != position:
            raise GovernanceLedgerUnavailable(
                ledger, "revision_mismatch", line=position)


_CURATION_LEDGER_SCOPES = {
    "concept_curation_log.jsonl": ("concept", "concept_curation"),
    "claim_curation_log.jsonl": ("claim", "claim_curation"),
    "task_facets_curation_log.jsonl": ("facets", "task_facets_curation"),
}
_GOVERNANCE_LEDGER_FILES = {
    "concept_aliases.jsonl": "concept_aliases",
    "concept_splits.jsonl": "concept_splits",
    "claim_decisions.jsonl": "claim_decisions",
    **{name: ledger for name, (_kind, ledger) in _CURATION_LEDGER_SCOPES.items()},
}


def curation_ledger_scope(log_name: str) -> tuple[str, str]:
    """Return the closed steward kind/public-ledger pair for a paid history file."""
    try:
        return _CURATION_LEDGER_SCOPES[log_name]
    except KeyError as exc:
        raise ValueError("unknown curation ledger") from exc


def raise_governance_storage_unavailable(path: Path, exc: BaseException):
    """Map a known governance storage failure to the content-free public health contract."""
    ledger = _GOVERNANCE_LEDGER_FILES.get(path.name)
    if ledger is None:
        raise exc
    raise GovernanceLedgerUnavailable(ledger, "storage_unreadable") from exc


def confirm_governance_durable(path: Path) -> None:
    """Strictly sync an existing governance receipt before acknowledging an idempotent retry."""
    from looplab.core.atomicio import strict_fsync, strict_fsync_parent

    try:
        # Append mode supplies a write-capable descriptor on Windows without changing file bytes.
        with path.open("ab") as handle:
            strict_fsync(handle.fileno())
        # Always repeat the directory sync: a prior first-create attempt may have synced contents
        # but failed before durably publishing the directory entry.
        strict_fsync_parent(path)
    except (OSError, TimeoutError, RuntimeError) as exc:
        raise_governance_storage_unavailable(path, exc)


def _validate_curation_row(row: dict, *, kind: str) -> str | None:
    """Validate every known HTTP and finalize curation receipt schema."""
    action = row.get("action")
    version = row.get("v")
    if "action" in row and action not in {
            "steward-invocation-begun", "steward-invocation"}:
        return "unknown_action"
    if version is not None and (
            isinstance(version, bool) or not isinstance(version, int)
            or version not in {1, 2}):
        return "unsupported_schema"
    for field, maximum in (
            ("by", 120), ("at", 120), ("error", 500),
            ("error_type", 200), ("ambiguity", 200)):
        if reason := validate_optional_text(row, field, maximum):
            return reason
    if reason := validate_revision_fields(row):
        return reason
    proposals = row.get("proposals", {})
    receipt = row.get("receipt")
    if not isinstance(proposals, dict) or (receipt is not None and not isinstance(receipt, dict)):
        return "invalid_record"

    # Modern finalize rows are semantic input-digest receipts, not HTTP action-id receipts. All
    # three paid finalize stewards share this schema; facets additionally has a governed fast path.
    if version == 2 and action is None:
        for field, maximum in (
                ("curation_key", 240), ("source_key", 240),
                ("run_id", 500), ("task_id", 500), ("input_digest", 80),
                ("input_schema", 300), ("model", 300), ("parser", 160)):
            if reason := validate_optional_text(row, field, maximum):
                return reason
        outcomes = {
            "unavailable", "empty", "proposed", "error",
            "prior_attempt_incomplete_not_replayed",
        }
        if kind == "facets":
            outcomes.add("already-governed")
        if (not row.get("curation_key") or not row.get("source_key")
                or row.get("outcome") not in outcomes
                or not isinstance(row.get("auto"), bool)
                or not isinstance(row.get("auto_requested"), bool)):
            return "invalid_record"
        finish_seq = row.get("finish_seq")
        if finish_seq is not None and (
                isinstance(finish_seq, bool) or not isinstance(finish_seq, int)
                or finish_seq < 0):
            return "invalid_record"
        return None

    # Known pre-v2 finalize rows are run-keyed. Their absence of an HTTP action id means they are
    # audit/proposal history only; they never satisfy a new on-demand paid action-id lookup.
    if action is None and not row.get("action_id"):
        if version not in (None, 1):
            return "unsupported_schema"
        for field, maximum in (("run_id", 500), ("task_id", 500)):
            if reason := validate_optional_text(row, field, maximum):
                return reason
        outcomes = {"unavailable", "empty", "proposed", "error"}
        if kind == "facets":
            outcomes.add("already-governed")
        if not row.get("run_id") or row.get("outcome") not in outcomes:
            return "invalid_record"
        for field in ("auto", "auto_requested"):
            if field in row and not isinstance(row[field], bool):
                return "invalid_record"
        return None

    # Task-facet stewardship has no paid HTTP invocation schema. Treat any such row as foreign
    # instead of accidentally granting it terminal/action-id semantics copied from another ledger.
    if kind == "facets":
        return "unsupported_schema" if version is not None else "invalid_record"

    # The oldest HTTP audit projection carried action_id/proposals but no discriminator. Treat it
    # as a terminal receipt so the same id can never become a new paid cache miss.
    if action is None:
        action_id = row.get("action_id")
        if (version is not None or not isinstance(action_id, str) or not action_id
                or len(action_id) > 160 or row.get("from") not in (None, kind)
                or row.get("outcome") not in (None, "empty", "proposed", "error")):
            return "invalid_record"
        return None

    if version != 1 or row.get("from") != kind:
        return "unsupported_schema" if version != 1 else "invalid_record"

    if action == "steward-invocation-begun":
        invocation_id = row.get("invocation_id")
        if (not isinstance(invocation_id, str) or not invocation_id
                or len(invocation_id) > 160 or row.get("outcome") != "begun"):
            return "invalid_record"
        if any(row.get(field) not in (None, "", {}) for field in (
                "action_id", "proposals", "receipt", "error", "begun_revision")):
            return "invalid_record"
        return None

    action_id = row.get("action_id")
    outcome = row.get("outcome")
    if (not isinstance(action_id, str) or not action_id or len(action_id) > 160
            or outcome not in {"empty", "proposed", "error"}):
        return "invalid_record"
    if "invocation_id" in row and row["invocation_id"] not in (None, ""):
        return "invalid_record"
    begun_revision = row.get("begun_revision")
    if begun_revision is not None and (
            isinstance(begun_revision, bool) or not isinstance(begun_revision, int)
            or begun_revision < 1):
        return "invalid_revision"
    error = row.get("error")
    if outcome == "error":
        if not isinstance(error, str) or not error:
            return "invalid_record"
    elif error not in (None, ""):
        return "invalid_record"
    return None


def read_curation_rows(
        path: Path, *, kind: str | None = None, ledger: str | None = None) -> list[dict]:
    """Strictly read one complete paid-curation history, including task facets."""
    expected_kind, expected_ledger = curation_ledger_scope(path.name)
    if kind is not None and kind != expected_kind:
        raise ValueError("curation kind does not match ledger")
    if ledger is not None and ledger != expected_ledger:
        raise ValueError("curation public ledger does not match file")
    kind, ledger = expected_kind, expected_ledger
    rows = read_governance_rows(
        path, ledger=ledger,
        validate=lambda row: _validate_curation_row(row, kind=kind),
    )
    validate_local_revisions(rows, ledger=ledger)

    normalized: list[dict] = []
    for row in rows:
        if row.get("action") is None and row.get("action_id"):
            proposals = row.get("proposals") or {}
            has_proposals = any(
                isinstance(value, list) and value for value in proposals.values())
            outcome = row.get("outcome")
            if not outcome:
                outcome = ("error" if row.get("error")
                           else ("proposed" if has_proposals else "empty"))
            normalized.append({
                **row, "v": 1, "action": "steward-invocation",
                "from": kind, "outcome": outcome,
            })
        else:
            normalized.append(row)

    beginnings: dict[str, int] = {}
    outcomes: set[str] = set()
    for position, row in enumerate(normalized, start=1):
        if row.get("action") not in {
                "steward-invocation-begun", "steward-invocation"}:
            continue
        if row["action"] == "steward-invocation-begun":
            invocation_id = row["invocation_id"]
            if invocation_id in beginnings or invocation_id in outcomes:
                raise GovernanceLedgerUnavailable(
                    ledger, "duplicate_action_id", line=position)
            beginnings[invocation_id] = position
            continue
        action_id = row["action_id"]
        if action_id in outcomes:
            raise GovernanceLedgerUnavailable(
                ledger, "duplicate_action_id", line=position)
        outcomes.add(action_id)
        begun_revision = row.get("begun_revision")
        if action_id in beginnings:
            if begun_revision != beginnings[action_id]:
                raise GovernanceLedgerUnavailable(
                    ledger, "revision_mismatch", line=position)
        elif begun_revision is not None:
            raise GovernanceLedgerUnavailable(
                ledger, "revision_mismatch", line=position)
    return normalized


def claim_governance_snapshot(memory_dir) -> dict:
    """Freeze the claim-decision projection and its CAS revision under the writer lock."""
    return project_governed_sources(
        memory_dir,
        lambda governance: {
            "decisions": governance["decisions"],
            "revision": governance["claim_revision"],
            "status": "complete",
            "complete": True,
        },
    )


def cross_run_governance_snapshot(memory_dir) -> dict:
    """Freeze alias, split and claim policy plus matching revisions at one lock point.

    Lock order is concept-global then claim-ledger. Concept writers use only the former (plus
    their child ledger lock); claim writers use only the latter, so this introduces no inverse
    acquisition path. Atlas/retrieval can now label exactly the policy they projected rather than
    reading maps first and attaching newer revisions later.
    """
    return project_governed_sources(
        memory_dir, lambda governance: governance, include_concepts=True)


_GOVERNED_SOURCE_NAMES = frozenset((
    "concept_capsules.jsonl", "lessons.jsonl", "research_claims.jsonl",
))


def _empty_governance_snapshot() -> dict:
    return {
        "aliases": {}, "splits": {}, "decisions": {},
        "alias_revision": 0, "split_revision": 0,
        "concept_governance_revision": 0, "claim_revision": 0,
        "status": "complete", "complete": True,
    }


def _read_governance_locked(base: Path, *, include_concepts: bool) -> dict:
    """Read policy maps and matching revisions; the caller owns the required locks."""
    from looplab.engine.claims import claim_governance_revision, load_claim_decisions

    snapshot = _empty_governance_snapshot()
    snapshot["decisions"] = load_claim_decisions(base)
    snapshot["claim_revision"] = claim_governance_revision(base)
    if not include_concepts:
        return snapshot

    from looplab.engine.concept_registry import (
        concept_governance_global_revision,
        concept_governance_revision,
        load_concept_aliases,
        load_concept_splits,
    )

    snapshot.update({
        "aliases": load_concept_aliases(base),
        "splits": load_concept_splits(base),
        "alias_revision": concept_governance_revision(base, "aliases"),
        "split_revision": concept_governance_revision(base, "splits"),
        "concept_governance_revision": concept_governance_global_revision(base),
    })
    return snapshot


def project_governed_sources(
        memory_dir, project: Callable[[dict], _ProjectionT], *,
        include_concepts: bool = False, source_names=(),
        claim_locked: bool = False) -> _ProjectionT:
    """Project policy and mutable evidence at one canonical lock point.

    Lock order is concept-global (when requested), then claim decisions, then source files sorted
    by absolute path. The callback executes before any lock is released, so its payload and the
    attached governance revisions describe one linearizable snapshot. A claim writer that already
    owns ``claim_decisions.jsonl.lock`` may pass ``claim_locked=True``; acquiring concept governance
    from that position is rejected because it would invert the global order.
    """
    if claim_locked and include_concepts:
        raise ValueError("claim-locked projection cannot acquire concept governance")
    requested = tuple(source_names or ())
    if (any(not isinstance(name, str) or name not in _GOVERNED_SOURCE_NAMES
            for name in requested)):
        raise ValueError("unknown governed source")
    if not memory_dir:
        return project(_empty_governance_snapshot())
    base = Path(memory_dir)
    # Linearize an absent store at this observation point without creating lock artifacts on a read.
    if not base.exists():
        return project(_empty_governance_snapshot())

    from looplab.engine.concept_registry import _concept_governance_transaction
    from looplab.events.eventstore import _interprocess_lock

    concept_guard = (_concept_governance_transaction(base)
                     if include_concepts else nullcontext())
    claim_path = base / "claim_decisions.jsonl"
    claim_guard = (nullcontext() if claim_locked else _interprocess_lock(
        Path(str(claim_path) + ".lock"), required=True))
    sources = sorted({base / name for name in requested}, key=lambda path: str(path))
    with concept_guard:
        with claim_guard:
            with ExitStack() as source_stack:
                for source in sources:
                    source_stack.enter_context(_interprocess_lock(
                        Path(str(source) + ".lock"), required=True))
                governance = _read_governance_locked(
                    base, include_concepts=include_concepts)
                # CODEX AGENT: the callback, not just its input reads, remains inside the locks.
                # Otherwise a writer can land after payload construction but before its revision label.
                return project(governance)
