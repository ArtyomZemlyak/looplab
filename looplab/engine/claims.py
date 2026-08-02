"""PART IV cross-run Step 4 (§21.20) — evidence-grounded CLAIM assessments.

The pure projection core maps existing memory into verifiable claims: a distilled lesson
already carries `{statement, outcome, evidence:[node_ids], run_id, task_id}` (a verdict + its grounding
nodes), and a D8 deep-research memo carries `claims:[{statement, node_ids, urls}]`. This module UNIFIES
those two shapes (it does not fork a third): it groups by normalized statement and records support vs
oppose evidence refs plus an epistemic state, so the loop/UI can ask "what does the accumulated evidence
suggest, and what contradicts it?" — the §21.20.5 claim idea in lean form.

Around that core, this module owns the durable v3 ``research_claims.jsonl`` store, governance decisions,
health-aware readers and live API/prompt consumers. It performs no LLM inference itself. The
verdict→stance mapping reuses the shipped lesson vocabulary (`memory._NEGATIVE` / "supported"); a
"noted"/unknown verdict is neutral (it takes no stance), exactly as on the lesson read/write paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from typing import Optional

from looplab.engine.governance_health import (
    GovernanceLedgerUnavailable,
    confirm_governance_durable,
    observed_path_missing,
    read_governance_rows,
    raise_governance_storage_unavailable,
    validate_action_ids,
    validate_local_revisions,
    validate_optional_text,
    validate_revision_fields,
)
from looplab.engine.memory import _CLAIM_STANCES, _NEGATIVE, normalize_statement
from looplab.trust.cross_run import (
    cross_run_identity_text,
    cross_run_text,
    sanitize_cross_run_projection,
)

# Re-exported so `looplab.engine.claims` keeps its historical surface: both spellings are the
# SAME object, so existing imports and monkeypatch seams are unaffected (doc 25 EM-01).
from looplab.engine.claims_health import (  # noqa: F401
    _CLAIM_READ_HEALTH_VERSION,
    _CLAIM_SOURCE_ROW_MAX_CHARS,
    _CLAIM_SOURCE_ROW_MAX_TOTAL_ITEMS,
    _CLAIM_SOURCE_SEMANTIC_FIELDS,
    _ClaimAssessmentRows,
    _ClaimSourceRows,
    _LESSON_OUTCOMES,
    _MAX_CLAIM_PROJECTION_ITEMS,
    _MAX_CONTEXT_CLAIMS,
    _MAX_DECISION_METRIC,
    _MAX_RESEARCH_CLAIMS_PER_RUN,
    _MAX_RESEARCH_SOURCE_ITEMS,
    _MAX_RETRIEVAL_CORPUS,
    _MAX_RETRIEVAL_HITS,
    _MAX_SOURCE_EVIDENCE,
    _MAX_SOURCE_FINGERPRINT,
    _MAX_SOURCE_ID,
    _MAX_SOURCE_STATEMENT,
    _RESEARCH_CLAIM_VERSION,
    _RESEARCH_EVIDENCE_RECEIPT_FIELDS,
    _RESEARCH_SOURCE_RECEIPT_FIELDS,
    _RESEARCH_SOURCE_RECEIPT_ROW_FIELDS,
    _RESEARCH_SOURCE_RECEIPT_V2,
    _RESEARCH_SOURCE_RECEIPT_VERSION,
    _RESEARCH_VERDICTS,
    _RESEARCH_VERIFICATION_FIELDS,
    _bounded_claim_projection,
    _claim_rows_snapshot_digest,
    _claim_source_rows,
    _claim_source_semantic_projection,
    _claim_source_summary,
    _claim_text,
    _empty_claim_read_health,
    _empty_claim_read_segment,
    _epistemic,
    _filter_claim_assessments,
    _filter_claim_source_rows,
    _identity_text,
    _indexable_research_claim,
    _lesson_claim_stance,
    _load_claim_source_path,
    _metric_identity,
    _node_ids,
    _qualify_refs,
    _research_source_receipt,
    _research_source_summary,
    _research_verification,
    _safe_claim_read_health,
    _safe_claim_read_segment,
    _safe_claim_source_summary,
    _safe_research_source_summary,
    _source_guarded_epistemic,
    _unknown_claim_source_summary,
    _valid_claim_source_row,
    _valid_claim_source_rows,
    _valid_node_source,
    _valid_research_evidence_receipt,
    _valid_research_node_refs,
    _valid_research_url_identities,
    claim_evidence_digest,
)

# --------------------------------------------------------------------------- #
# Operator claim DECISIONS (§22.4) — the ONLY write to cross-run MEANING an actor other than the engine
# may make. Append-only, keyed by normalized statement, overlaid on the machine-proposed assessment.
# --------------------------------------------------------------------------- #

CLAIM_DECISIONS = ("ratified", "rejected", "pinned")
CLAIM_DECISION_ACTIONS = CLAIM_DECISIONS + ("clear",)

_MAX_DECISION_STATEMENT = 4000
_MAX_DECISION_SCOPE = 500
_MAX_DECISION_NOTE = 4000
_MAX_DECISION_ACTOR = 120
_MAX_DECISION_AT = 120
_MAX_DECISION_ACTION_ID = 160
_MAX_EVIDENCE_DIGEST = 80
_CLAIM_LEDGER = "claim_decisions"


class ClaimDecisionConflict(ValueError):
    """Optimistic-concurrency conflict on the append-only claim-governance ledger."""

    def __init__(self, expected: int, current: int):
        super().__init__(f"claim governance revision conflict: expected {expected}, current {current}")
        self.expected_revision = expected
        self.current_revision = current


class ClaimDecisionIdempotencyConflict(ValueError):
    """An ``action_id`` was reused with a different semantic decision payload."""


class ClaimTargetConflict(ValueError):
    """The operator's observed claim identity/evidence no longer names a writable live target."""

    def __init__(self, code: str, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _validate_claim_decision_row(row: dict) -> str | None:
    """Strict schema fence for rows whose omission changes live claim policy."""
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid

    decision = row.get("decision")
    if not isinstance(decision, str) or decision not in CLAIM_DECISION_ACTIONS:
        return "unknown_action"
    statement = row.get("statement")
    if (not isinstance(statement, str) or not statement.strip()
            or len(statement) > _MAX_DECISION_STATEMENT
            or not _claim_text(statement, _MAX_DECISION_STATEMENT).strip()):
        return "invalid_record"
    version = row.get("claim_key_version")
    # Missing is the migration discriminator. Known historical versions are re-keyed from the
    # durable statement; a future version may change identity and must not be guessed by this reader.
    if version is not None and (
            isinstance(version, bool) or not isinstance(version, int)
            or version < 1 or version > CLAIM_KEY_VERSION):
        return "unsupported_schema"
    for field, maximum in (
        ("key", 160), ("claim_uid", 80), ("scope", _MAX_DECISION_SCOPE),
        ("metric", _MAX_DECISION_METRIC), ("note", _MAX_DECISION_NOTE),
        ("by", _MAX_DECISION_ACTOR), ("at", _MAX_DECISION_AT),
        ("action_id", _MAX_DECISION_ACTION_ID),
        ("evidence_digest", _MAX_EVIDENCE_DIGEST),
    ):
        if reason := validate_optional_text(row, field, maximum):
            return reason
    if "action_id" in row and not row["action_id"].strip():
        return "invalid_record"
    if version == CLAIM_KEY_VERSION:
        scope = _identity_text(row.get("scope"), _MAX_DECISION_SCOPE)
        metric = _identity_text(row.get("metric"), _MAX_DECISION_METRIC)
        canonical_statement = _claim_text(statement, _MAX_DECISION_STATEMENT)
        # current-version rows are writer receipts, not migration input. Replay must
        # never sanitize one durable identity and then expose/apply it under another key.
        if (statement != canonical_statement
                or row.get("scope") != scope or row.get("metric") != metric
                or row.get("key") != normalize_statement(canonical_statement)
                or row.get("claim_uid") != claim_uid(
                    canonical_statement, scope=scope, metric=metric)):
            return "invalid_record"
    return validate_revision_fields(row)


def _read_claim_decision_rows(path) -> list[dict]:
    from pathlib import Path

    rows = read_governance_rows(
        Path(path), ledger=_CLAIM_LEDGER, validate=_validate_claim_decision_row)
    validate_action_ids(rows, ledger=_CLAIM_LEDGER)
    normalized_ids: set[str] = set()
    for line_number, row in enumerate(rows, start=1):
        raw_action_id = row.get("action_id")
        if not raw_action_id:
            continue
        action_id = _identity_text(raw_action_id, _MAX_DECISION_ACTION_ID)
        if action_id != raw_action_id or action_id in normalized_ids:
            raise GovernanceLedgerUnavailable(
                _CLAIM_LEDGER, "duplicate_action_id" if action_id in normalized_ids
                else "invalid_record", line=line_number)
        normalized_ids.add(action_id)
    validate_local_revisions(rows, ledger=_CLAIM_LEDGER)
    return rows


def _bounded(value, name: str, maximum: int, *, required: bool = False) -> str:
    text = str(value or "")
    if required and not text.strip():
        raise ValueError(f"empty {name}")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    if name in {"scope", "metric", "action_id", "at", "evidence_digest", "claim_uid"}:
        bounded = cross_run_identity_text(text, max_chars=maximum).strip()
    else:
        bounded = cross_run_text(
            text, max_chars=maximum, single_line=True, entropy=True)
    # requiredness applies to the persisted value. A control-only statement passed
    # the raw ``strip`` check but sanitized to empty, was acknowledged, and poisoned the next replay.
    if required and not bounded.strip():
        raise ValueError(f"empty {name}")
    return bounded


def _decision_payload(row: dict) -> tuple:
    """Semantic request identity for ``action_id`` replay.

    Actor and timestamp are receipt metadata: a transport retry may be served after the deployment's
    operator label changes, but it must still return the original durable receipt instead of conflicting.
    """
    return (
        _claim_text(row.get("statement"), _MAX_DECISION_STATEMENT),
        _identity_text(row.get("scope"), _MAX_DECISION_SCOPE),
        _identity_text(row.get("metric"), _MAX_DECISION_METRIC),
        str(row.get("decision") or ""),
        _claim_text(row.get("note"), _MAX_DECISION_NOTE),
        _identity_text(row.get("evidence_digest"), _MAX_EVIDENCE_DIGEST),
    )


def _logical_decision_rows(rows) -> list[dict]:
    """Assign one monotonic revision after the strict physical-ledger health boundary."""
    logical: list[dict] = []
    actions: dict[str, tuple] = {}
    for raw in rows or []:
        if not isinstance(raw, dict) or raw.get("decision") not in CLAIM_DECISION_ACTIONS:
            continue
        action_id = str(raw.get("action_id") or "")
        if action_id:
            if action_id in actions:
                # Exact duplicate or collision: either way the repeated physical row is not a new action.
                continue
            actions[action_id] = _decision_payload(raw)
        logical.append({**raw, "revision": len(logical) + 1})
    return logical


def claim_governance_revision(memory_dir) -> int:
    """Current logical claim-governance revision; valid legacy rows count in file order."""
    from pathlib import Path

    if not memory_dir:
        return 0
    path = Path(memory_dir) / "claim_decisions.jsonl"
    rows = _read_claim_decision_rows(path)
    return len(_logical_decision_rows(rows))


def record_claim_decision(memory_dir, *, statement: str, decision: str, note: str = "",
                          by: str = "operator", at: str = "", scope: str = "", metric: str = "",
                          expected_revision: Optional[int] = None, action_id: str = "",
                          evidence_digest: str = "", validate: Optional[Callable[[], None]] = None,
                          validate_evidence: Optional[Callable[[list[dict]], None]] = None) -> dict:
    """Persist an OPERATOR verdict on a claim (ratify / reject / pin). Append-only JSONL, keyed BOTH by the
    legacy `normalize_statement` (so the lean projection still overlays) AND by a structured `claim_uid`
    (scope+polarity-precise, so a decision in task A never reaches a same-worded claim in task B).
    `scope` (task id) / `metric` qualify the structured key. This is the §22.4 governance write — agents
    never call it. Returns the record. Durable locked+fsynced append; raises on an invalid decision or
    missing memory dir (a real operator error)."""
    from pathlib import Path

    if decision not in CLAIM_DECISION_ACTIONS:
        raise ValueError(f"decision must be one of {CLAIM_DECISION_ACTIONS}, got {decision!r}")
    if not memory_dir:
        raise ValueError("no memory_dir")
    # Reject oversized identity fields instead of truncating them: the exact persisted statement/scope/metric
    # must always recompute the same UID after restart. The 4000 statement cap matches persisted D8 claims.
    s = _bounded(statement, "statement", _MAX_DECISION_STATEMENT, required=True).strip()
    sc = _bounded(scope, "scope", _MAX_DECISION_SCOPE)
    mt = _bounded(metric, "metric", _MAX_DECISION_METRIC)
    aid = _bounded(action_id, "action_id", _MAX_DECISION_ACTION_ID).strip()
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    rec = {"statement": s, "key": normalize_statement(s), "claim_key_version": CLAIM_KEY_VERSION,
           "claim_uid": claim_uid(s, scope=sc, metric=mt), "scope": sc, "metric": mt,
           "decision": decision, "note": _bounded(note, "note", _MAX_DECISION_NOTE),
           "by": _bounded(by or "operator", "by", _MAX_DECISION_ACTOR),
           "at": _bounded(at, "at", _MAX_DECISION_AT)}
    digest = _bounded(evidence_digest, "evidence_digest", _MAX_EVIDENCE_DIGEST).strip()
    if digest:
        rec["evidence_digest"] = digest
    if aid:
        rec["action_id"] = aid
    path = Path(memory_dir) / "claim_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    from looplab.core.atomicio import strict_fsync, strict_fsync_parent
    from looplab.events.eventstore import _interprocess_lock
    # Idempotency lookup, revision CAS, allocation and append are one critical section. A
    # pre-lock check lets two UI writers both accept revision N and silently create divergent policy.
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        # governance corruption is not a zero-row revision. Refuse every operator
        # write until the ledger is explicitly repaired; a later pin/clear must never hide the
        # quarantine behind a fresh, apparently healthy revision.
        rows = _read_claim_decision_rows(path)
        logical = _logical_decision_rows(rows)
        created = not path.exists()
        if aid:
            existing = next((r for r in logical
                             if _identity_text(r.get("action_id"), _MAX_DECISION_ACTION_ID) == aid), None)
            if existing is not None:
                if _decision_payload(existing) == _decision_payload(rec):
                    confirm_governance_durable(path)
                    return sanitize_cross_run_projection(
                        existing, max_chars=16_000, max_items=64, max_total_items=256)
                raise ClaimDecisionIdempotencyConflict(
                    f"action_id {aid!r} was already used for a different claim decision")
        current = len(logical)
        if expected_revision is not None:
            if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                    or expected_revision < 0):
                raise ValueError("expected_revision must be a non-negative integer")
            if expected_revision != current:
                raise ClaimDecisionConflict(expected_revision, current)
        def _persist(governance=None):
            if validate is not None:
                validate()
            if validate_evidence is not None:
                lessons_snapshot = load_claim_lessons(memory_dir)
                research_snapshot = load_research_claims(memory_dir)
                evidence_snapshot = claim_assessments(
                    lessons_snapshot, research_claims=research_snapshot,
                    decisions=governance["decisions"], structured=True)
                evidence_snapshot.lessons_snapshot = lessons_snapshot
                evidence_snapshot.research_claims_snapshot = research_snapshot
                evidence_snapshot.decisions_snapshot = governance["decisions"]
                validate_evidence(evidence_snapshot)
            stored = {**rec, "revision": current + 1}
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(stored) + "\n")
                    f.flush()
                    strict_fsync(f.fileno())
                if created:
                    strict_fsync_parent(path)
            except (OSError, TimeoutError, RuntimeError) as exc:
                raise_governance_storage_unavailable(path, exc)
            return stored

        if validate_evidence is None:
            return _persist()

        # claim policy is already locked. Acquire evidence only after it, then keep every
        # lock through digest validation and the durable append. This is the same global order used by
        # read projections and prevents a lesson/research rewrite from slipping under the operator CAS.
        from looplab.engine.governance_health import project_governed_sources
        return project_governed_sources(
            memory_dir, _persist,
            source_names=("lessons.jsonl", "research_claims.jsonl"),
            claim_locked=True,
        )


def record_observed_claim_decision(
        memory_dir, *, statement: str, claim_uid: str, evidence_digest: str,
        decision: str, note: str = "", by: str = "operator", at: str = "",
        scope: str = "", metric: str = "", expected_revision: int,
        action_id: str) -> dict:
    """Record a decision only for the exact live claim snapshot an operator reviewed.

    This is the transport-neutral governance entrypoint used by both HTTP and CLI. The content-addressed
    UID prevents statement/scope retargeting, the evidence digest fences source rewrites, and the ledger
    revision/action id provide CAS plus lost-response idempotency. All live-target checks execute inside
    ``record_claim_decision``'s policy-then-evidence lock chain.
    """
    from looplab.engine.claim_key import claim_uid as derive_claim_uid

    observed_uid = _bounded(claim_uid, "claim_uid", 80, required=True).strip()
    observed_digest = _bounded(
        evidence_digest, "evidence_digest", _MAX_EVIDENCE_DIGEST, required=True).strip()
    stable_action_id = _bounded(
        action_id, "action_id", _MAX_DECISION_ACTION_ID, required=True).strip()
    if expected_revision is None:
        raise ValueError("expected_revision is required")
    expected_uid = derive_claim_uid(statement, scope=scope, metric=metric)
    if observed_uid != expected_uid:
        raise ClaimTargetConflict(
            "claim_target_changed", expected_claim_uid=expected_uid)

    def _validate_target(evidence_snapshot) -> None:
        current_projection = claims_for_memory(
            memory_dir, lessons=evidence_snapshot.lessons_snapshot,
            research_claims=evidence_snapshot.research_claims_snapshot,
            decisions=evidence_snapshot.decisions_snapshot,
            scope_task=scope, structured=True,
        )
        current = next((candidate for candidate in current_projection
                        if candidate.get("claim_uid") == observed_uid), None)
        if decision == "clear":
            decisions = evidence_snapshot.decisions_snapshot
            if observed_uid in decisions:
                return
            active = (current or {}).get("decision") or {}
            actual_uid = str(active.get("claim_uid") or "")
            if actual_uid and actual_uid != observed_uid:
                raise ClaimTargetConflict(
                    "claim_clear_target_mismatch", claim_uid=actual_uid,
                    scope=str(active.get("scope") or ""),
                    metric=str(active.get("metric") or ""),
                )
            raise ClaimTargetConflict("claim_decision_missing")
        if current is None:
            raise ClaimTargetConflict("claim_target_missing")
        if current.get("evidence_digest") != observed_digest:
            raise ClaimTargetConflict(
                "claim_evidence_changed",
                current_evidence_digest=current.get("evidence_digest"),
            )

    # never pre-resolve the target in a CLI/API wrapper. The replay lookup must happen first
    # for lost-response retries, then CAS + evidence validation + append must remain one locked operation.
    return record_claim_decision(
        memory_dir, statement=statement, scope=scope, metric=metric,
        decision=decision, note=note, by=by, at=at,
        expected_revision=expected_revision, action_id=stable_action_id,
        evidence_digest=observed_digest, validate_evidence=_validate_target,
    )


def _global_key(legacy_key: str) -> str:
    """A DISTINCT index (in the same decisions dict) for the last SCOPE-LESS decision on a statement, so
    a later scoped decision that overwrites the plain legacy key can't hide the portfolio-wide verdict
    from the structured fallback. The control-char prefix won't collide with a claim_uid ("clm_"+hex) or,
    in practice, a normalize_statement key — the only way to collide is a statement literally beginning
    with a NUL byte, which argv, LLM text and engine-written JSON logs never carry. The dict is only ever
    read via `.get(key)`, never iterated, so the extra keys are safe."""
    return "\x00global\x00" + legacy_key


def _scoped_key(legacy_key: str, scope: str) -> str:
    """A lean-projection index for a scope-only decision.

    The structured UID remains authoritative.  This secondary key lets the default statement projection
    retrieve an exact task verdict without putting scoped policy back at the shared legacy key, where the
    latest task would overwrite every earlier task's decision.
    """
    return "\x00scope\x00" + str(scope) + "\x00" + legacy_key


def load_claim_decisions(memory_dir) -> dict:
    """Replay current decisions into safe global and structured namespaces.

    UIDs are recomputed with the current claim-key version, so durable v1 rows migrate on read. A scoped or
    metric-qualified row is indexed ONLY by its structured UID: it must never overwrite the global legacy
    statement key. Unscoped/unqualified rows remain the fallback for every scope. ``clear`` tombstones only
    the namespace it addresses. Last write wins within each exact namespace. Missing is empty; an unhealthy
    policy ledger raises instead of projecting a guessed valid subset.
    """
    from pathlib import Path

    if not memory_dir:
        return {}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    out: dict = {}
    # claim_uid -> every key currently indexed at it. A SUPERSET index: entries are added on write
    # and pruned lazily when the retirement scan finds the key no longer carries that uid, which is
    # exactly the predicate the old full `out.items()` walk evaluated. That walk ran once per row,
    # so a portfolio with thousands of appended decisions paid a quadratic re-index on EVERY
    # claims_for_memory / retrieval / governance read.
    by_uid: dict[str, set] = {}
    rows = _read_claim_decision_rows(path)
    for r in _logical_decision_rows(rows):
        statement = _claim_text(r.get("statement"), _MAX_DECISION_STATEMENT)
        scope = _identity_text(r.get("scope"), _MAX_DECISION_SCOPE)
        metric = _identity_text(r.get("metric"), _MAX_DECISION_METRIC)
        k = normalize_statement(statement) if statement else str(r.get("key") or "")
        # A legacy scoped row without its statement cannot be migrated safely. Never fall back to its old UID:
        # that would silently replay a v1 token-set collision under the v2 role-aware contract.
        uid = claim_uid(statement, scope=scope, metric=metric) if statement else ""
        # Legacy decision rows predate always-on redaction. Keep their governance identity/revision but
        # never copy a nested note/actor/action payload back into an agent or HTTP projection verbatim.
        current = sanitize_cross_run_projection(
            {**r, "statement": statement, "scope": scope, "metric": metric,
             "claim_uid": uid, "claim_key_version": CLAIM_KEY_VERSION},
            max_chars=16_000, max_items=64, max_total_items=256)
        keys = ([uid] if uid else [])
        if k and not scope and not metric:
            # Retain a distinct portfolio-wide fallback as well as the legacy lean key. A
            # caller may merge overlays that place a scoped decision at the plain key; that must not erase
            # the durable global verdict for every other scope.
            keys.extend((k, _global_key(k)))
        elif k and scope and not metric:
            keys.append(_scoped_key(k, scope))
        # One semantic UID may have several historical display spellings. Retire every index that points
        # at the same namespace before applying its newest row, so ``clear`` cannot be bypassed through an
        # older legacy statement key.
        if uid:
            indexed = by_uid.get(uid)
            for old_key in tuple(indexed or ()):
                old = out.get(old_key)
                if old is not None and str(old.get("claim_uid") or "") == uid:
                    out.pop(old_key, None)
                indexed.discard(old_key)     # the key no longer points here either way
        for key in keys:
            if r.get("decision") == "clear":
                out.pop(key, None)
            else:
                out[key] = current
                if uid:
                    by_uid.setdefault(uid, set()).add(key)
    return out


def _string_list(raw, *, maximum: int, item_maximum: int) -> list[str]:
    """Bounded JSON-list normalization; strings are scalar values, never character iterables."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for value in raw[:maximum]:
        if isinstance(value, str):
            clean = _claim_text(value, item_maximum)
            if clean:
                out.append(clean)
    return out


# --------------------------------------------------------------------------- #
# D8 research claims persisted cross-run (§21.20 / CR1b) — so a deep-research memo's evidence-backed
# claims survive their run and can CONTEST/support lesson verdicts (contested is otherwise unreachable
# from newest-verdict-wins lessons alone). Written at finalize; read by the claim assessments callers.
# --------------------------------------------------------------------------- #

def record_research_claims(memory_dir, *, run_id: str, task_id: str, claims,
                           direction: str, claims_total: Optional[int] = None,
                           claims_receipt_known: Optional[bool] = None,
                           evidence_complete: Optional[bool] = None) -> int:
    """Replace one run's v3 D8 claims in ``research_claims.jsonl`` under the required store lock.

    Rows carry version/record kind, run/task identity, direction/metric identity, statement,
    verification/evidence fields, node/URL references and both aggregate and per-row source receipts.
    Replacement preserves quarantined malformed input and returns the number of current rows written.
    """
    from pathlib import Path

    from looplab.events.eventstore import (
        _interprocess_lock, replace_jsonl_rows_atomic_preserving_quarantine,
    )
    if not memory_dir:
        return 0
    rid = _identity_text(run_id, 500)
    if not rid:
        return 0
    rows = []
    direction = str(direction or "")
    if direction not in ("min", "max"):
        # Current v3 identity is exact. An orientation-free record would be permanently unusable for live
        # scope and indistinguishable from a malformed writer, so refuse before replacing any prior rows.
        return 0
    source = claims if isinstance(claims, (list, tuple)) else []
    derived_total = len(source) + int(claims is not None and not isinstance(claims, (list, tuple)))
    modern_receipt = any(value is not None for value in (
        claims_total, claims_receipt_known, evidence_complete))
    if modern_receipt:
        if (type(claims_total) is not int or not derived_total <= claims_total <= _MAX_RESEARCH_SOURCE_ITEMS
                or type(claims_receipt_known) is not bool
                or type(evidence_complete) is not bool):
            return 0
        source_total = claims_total
    else:
        source_total = derived_total
    # select the first bounded set of VALID claims rather than slicing the raw list first. A
    # malformed prefix must not hide a valid opposition row later in the memo, and every skipped/capped input
    # remains visible in the repeated per-run receipt below.
    for c in source:
        if len(rows) >= _MAX_RESEARCH_CLAIMS_PER_RUN:
            break
        stmt = _claim_text(c.get("statement") if isinstance(c, dict) else "", 4000)
        if not stmt:
            continue
        verdict, method, note = _research_verification(c)
        node_ids = _node_ids(c.get("node_ids"))[:64]
        urls = _string_list(c.get("urls"), maximum=32, item_maximum=2000)
        row = {"v": _RESEARCH_CLAIM_VERSION, "record_kind": "claim", "run_id": rid,
               "task_id": _identity_text(task_id, 500),
               "direction": direction,
               "statement": stmt,
               "metric": _metric_identity(c),
               "node_ids": node_ids,
               "urls": urls,
               "verification": {"verdict": verdict, "method": method, "note": note}}
        raw_refs = c.get("node_refs")
        if _valid_research_node_refs(raw_refs, node_ids) and raw_refs is not None:
            row["node_refs"] = [
                {"node_id": ref["node_id"], "generation": ref["generation"]}
                for ref in raw_refs
            ]
        raw_url_ids = c.get("url_identities")
        if _valid_research_url_identities(raw_url_ids, urls) and raw_url_ids is not None:
            row["url_identities"] = list(raw_url_ids)
        raw_evidence_receipt = c.get("evidence_receipt")
        if _valid_research_evidence_receipt(raw_evidence_receipt) and raw_evidence_receipt is not None:
            row["evidence_receipt"] = {
                key: raw_evidence_receipt[key] for key in _RESEARCH_EVIDENCE_RECEIPT_FIELDS}
        rows.append(row)
    claim_count = len(rows)
    if modern_receipt:
        receipt = {
            "v": _RESEARCH_SOURCE_RECEIPT_V2,
            "claims_total": source_total,
            "claims_retained": claim_count,
            "claims_omitted": source_total - claim_count,
            "claims_receipt_known": claims_receipt_known,
            "evidence_complete": evidence_complete,
            "producer_complete": bool(
                claims_receipt_known and evidence_complete and source_total == claim_count),
        }
    else:
        receipt = {
            "v": _RESEARCH_SOURCE_RECEIPT_VERSION,
            "claims_total": source_total,
            "claims_retained": claim_count,
            "claims_omitted": source_total - claim_count,
            "producer_complete": source_total == claim_count,
        }
    for row in rows:
        row["source_receipt"] = receipt
    if claim_count == 0:
        # Every explicitly processed empty snapshot needs a durable denominator too. Otherwise a successful
        # empty extraction is indistinguishable from a run that never produced D8 at all, and a same-run
        # refresh can erase its only receipt. This sentinel participates in completeness only; assessment/
        # index loops ignore it because it has no statement.
        rows.append({
            "v": _RESEARCH_CLAIM_VERSION,
            "record_kind": "source_receipt",
            "run_id": rid,
            "task_id": _identity_text(task_id, 500),
            "direction": direction,
            "source_receipt": receipt,
        })
    path = Path(memory_dir) / "research_claims.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the same interprocess lock the case/capsule/decision sidecar stores use — and RE-READ inside it —
    # so concurrent runs survive. Raw-line preservation additionally keeps unreadable/future records visible
    # to store-health readers instead of laundering quarantine into an apparently complete file.
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        replace_jsonl_rows_atomic_preserving_quarantine(
            path,
            rows,
            # #4 (resolved): retire every SAME-RUN row the reader FULLY UNDERSTANDS — the current-schema
            # v3 claim/source_receipt siblings AND the understood legacy (v0-v2, empty-kind) claims a
            # pre-upgrade run of this same run_id wrote. `_valid_claim_source_row` IS that "understood"
            # fence: it accepts v3 (with a valid receipt + verification) and valid legacy claims, and
            # REJECTS future/malformed/unknown-kind rows — those are NOT retired here, so they stay as raw
            # quarantine evidence (never erased on a plausible run_id). Retiring the superseded legacy rows
            # at the SOURCE (rather than only skipping them at index time) fixes all three symptoms: a
            # refresh now retires a withdrawn claim, stale spellings leave the union, and
            # claims_retained == len(claim_members) again (no more perpetual producer-unknown for the run).
            # Cross-run legacy rows are untouched (the `== rid` guard): a genuinely old run that never
            # re-finalized keeps its claims — they are the latest for THAT run, not superseded.
            replace_if=lambda row: (
                _valid_claim_source_row(row, research=True)
                and _identity_text(row.get("run_id"), _MAX_SOURCE_ID) == rid
            ),
            loads=json.loads,
            dumps=json.dumps,
        )
    return claim_count


def load_research_claims(memory_dir) -> list[dict]:
    """Persisted D8 claims plus non-indexed processed-empty/all-invalid receipt sentinels. [] when none."""
    from pathlib import Path

    if not memory_dir:
        return _ClaimSourceRows()
    path = Path(memory_dir) / "research_claims.jsonl"
    rows = _load_claim_source_path(path, research=True)
    projected = []
    for row in rows:
        # Keep every schema-bounded field consumed by claim identity, evidence and source digest while
        # dropping unrelated legacy extensions before they can exhaust the redaction budget. Outward claim
        # projections remain capped separately; this internal snapshot must retain an allowed 65th..256th
        # reference so a tail rewrite changes governance identity instead of disappearing.
        durable = _claim_source_semantic_projection(row)
        # Missing version in a persisted file is not the direct pure-API snapshot compatibility case.
        durable.setdefault("v", 0)
        projected.append(sanitize_cross_run_projection(
            durable, max_chars=_CLAIM_SOURCE_ROW_MAX_CHARS,
            max_items=_MAX_SOURCE_EVIDENCE,
            max_total_items=_CLAIM_SOURCE_ROW_MAX_TOTAL_ITEMS))
    return _ClaimSourceRows(projected, read_health=rows.read_health)


def load_claim_lessons(memory_dir) -> list[dict]:
    """Claim-compatible lesson rows with physical/schema read health attached to the snapshot."""
    from pathlib import Path

    if not memory_dir:
        return _ClaimSourceRows()
    return _load_claim_source_path(Path(memory_dir) / "lessons.jsonl", research=False)


def claims_for_memory(memory_dir, *, lessons=None, research_claims=None, decisions=None,
                      scope_task: str = "", fuzzy: bool = False,
                      structured: bool = False) -> list[dict]:
    """Convenience: `claim_assessments` over a memory dir — lessons.jsonl (or a pre-filtered `lessons`) +
    the persisted D8 research claims + the operator-decision overlay. One call so every read path applies
    research claims AND decisions consistently. `fuzzy` (opt-in) merges paraphrased claims (CR1b);
    `structured` (opt-in) uses the scope+polarity-safe structured claim key (the full CR); `scope_task`
    filters the D8 research claims to the bound task so a task-scoped caller does not re-read another task's
    research claims (mega-review) — the decisions overlay is applied scope-safely by `claim_assessments`."""
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    research = _valid_claim_source_rows(research, research=True)
    if scope_task:
        wanted = str(scope_task)
        lessons = _filter_claim_source_rows(
            lessons, lambda r: str(r.get("task_id") or "") == wanted, research=False)
        research = _filter_claim_source_rows(
            research, lambda r: str(r.get("task_id") or "") == wanted, research=True)
    dec = load_claim_decisions(memory_dir) if decisions is None else decisions
    return claim_assessments(lessons, research_claims=research, decisions=dec,
                             fuzzy=fuzzy, structured=structured)


def atlas_for_memory(memory_dir, *, lessons=None, capsules=None, research_claims=None,
                     decisions=None, scope_task: str = "", max_items: int = 8,
                     structured: bool = False, _governance: Optional[dict] = None) -> dict:
    """Convenience: `portfolio_atlas` over a memory dir with EVERY overlay loaded — lessons + D8 research
    claims + operator decisions + concept aliases + splits. One call so every atlas surface is consistent.
    `structured` keeps the claim projection consistent with the researcher advisory; `scope_task` filters
    the D8 research claims to the bound task so a task-scoped caller does not surface another task's
    claims/contradictions (mega-review)."""
    from pathlib import Path

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import (ConceptCapsuleStore, _dedup_valid_capsules,
                                       _filter_capsule_rows)
    if _governance is None:
        source_names = []
        if lessons is None:
            source_names.append("lessons.jsonl")
        if research_claims is None:
            source_names.append("research_claims.jsonl")
        if capsules is None:
            source_names.append("concept_capsules.jsonl")
        return project_governed_sources(
            memory_dir,
            lambda governance: atlas_for_memory(
                memory_dir, lessons=lessons, capsules=capsules,
                research_claims=research_claims, decisions=decisions,
                scope_task=scope_task, max_items=max_items, structured=structured,
                _governance=governance,
            ),
            include_concepts=True, source_names=source_names,
        )
    base = Path(memory_dir) if memory_dir else None
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        # Path.exists() can collapse permission/storage failures into false. Only a confirmed
        # FileNotFound is an authoritative empty capsule source inside this governed snapshot.
        capsules = (ConceptCapsuleStore(cp).all()
                    if cp and not observed_path_missing(cp) else [])
    capsule_source = capsules if isinstance(capsules, (list, tuple)) else []
    capsules = _dedup_valid_capsules(capsule_source)
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    research = _valid_claim_source_rows(research, research=True)
    if scope_task:
        wanted = str(scope_task)
        # Scope is an access boundary across every joined store, not just D8. Filtering only
        # research rows still leaked other tasks through lessons and concept capsules in the same response.
        lessons = _filter_claim_source_rows(
            lessons, lambda r: str(r.get("task_id") or "") == wanted, research=False)
        capsules = _filter_capsule_rows(
            capsules, lambda r: str(r.get("task_id") or "") == wanted)
        research = _filter_claim_source_rows(
            research, lambda r: str(r.get("task_id") or "") == wanted, research=True)
    governance = _governance
    atlas = portfolio_atlas(
        lessons, capsules, max_items=max_items,
        decisions=(governance["decisions"] if decisions is None else decisions),
        research_claims=research, aliases=governance["aliases"],
        splits=governance["splits"], structured=structured)
    if decisions is None:
        atlas["governance"] = {
            "status": "complete", "complete": True,
            "revisions": {
                "claims": governance["claim_revision"],
                "concept_aliases": governance["alias_revision"],
                "concept_splits": governance["split_revision"],
                "concept_governance": governance["concept_governance_revision"],
            },
        }
    return atlas


_CLAIM_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _stmt_tokens(s: str) -> frozenset:
    return frozenset(w for w in _CLAIM_WORD.findall((s or "").casefold()) if len(w) > 2)


def _fuzzy_merge_claims(claims: list[dict], *, threshold: float = 0.6) -> list[dict]:
    """Conservative opt-in paraphrase projection.

    Candidates must share scope, semantic polarity and governance maturity, and every member must clear the
    threshold (complete-link). A bounded token index avoids all-pairs and single-link bridge collapse.
    """
    n = len(claims)
    if n <= 1:
        return claims
    from looplab.engine.claim_key import claim_signature
    toks = [_stmt_tokens(c["statement"]) for c in claims]
    meta = [(tuple(c.get("scopes") or []), claim_signature(c["statement"])["polarity"],
             str(c.get("maturity") or "machine-proposed")) for c in claims]
    groups: list[list[int]] = []
    token_groups: dict[str, set[int]] = {}
    for i, token_set in enumerate(toks):
        candidates = sorted({gid for token in token_set for gid in token_groups.get(token, ())})[:64]
        chosen = None
        for gid in candidates:
            members = groups[gid]
            if len(members) >= 64 or any(meta[j] != meta[i] for j in members):
                continue
            complete = True
            for j in members:
                union, inter = token_set | toks[j], token_set & toks[j]
                if not inter or len(inter) / len(union) < threshold:
                    complete = False
                    break
            if complete:
                chosen = gid
                break
        if chosen is None:
            chosen = len(groups)
            groups.append([])
        groups[chosen].append(i)
        for token in token_set:
            token_groups.setdefault(token, set()).add(chosen)

    out = []
    for idxs in groups:
        members = [claims[i] for i in idxs]
        if len(members) == 1:
            out.append(members[0])
            continue
        sup = sorted({r for m in members for r in m["support"]})
        opp = sorted({r for m in members for r in m["oppose"]})
        unverified = sorted({r for m in members for r in m.get("unverified", [])})
        rep = max(members, key=lambda m: (m["n_support"] + m["n_oppose"], m["statement"]))
        mat = members[0].get("maturity", "machine-proposed")
        research_source = (_safe_research_source_summary(members[0].get("research_source"))
                           or _research_source_summary([]))
        claim_source = (_safe_claim_source_summary(members[0].get("claim_source"))
                        or _claim_source_summary([], [], research_source=research_source))
        out.append({
            "statement": rep["statement"],
            "epistemic": _source_guarded_epistemic(sup, opp, claim_source), "maturity": mat,
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted({r for m in members for r in m["runs"]}),
            "scopes": sorted({r for m in members for r in m["scopes"]}),
            "sources": sorted({s for m in members for s in m.get("sources", [])}),
            "verification": sorted({v for m in members for v in m.get("verification", [])}),
            "decision": members[0].get("decision"),
            "merged_from": sorted(m["statement"] for m in members),
            "research_source": research_source,
            "claim_source": claim_source,
        })
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return out


def _structured_assessments(lessons, research_claims, decisions, *,
                            research_source: Optional[dict] = None,
                            claim_source: Optional[dict] = None) -> list[dict]:
    """The SCOPE+POLARITY-safe structured projection (full CR of the lean fuzzy merge). Identity is the
    `claim_signature` merge_key: (subject stems, scope=task, metric, polarity). Opposite-polarity claims
    sharing a `contra_key` are surfaced as a CONTRADICTION (they never merge, and each is marked contested).
    Governance overlays by the structured `claim_uid` (scope-precise)."""
    from looplab.engine.claim_key import claim_signature, claim_uid
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = (_safe_research_source_summary(research_source)
                       if research_source is not None else _research_source_summary(research_claims))
    if research_source is None:
        research_source = _research_source_summary(research_claims)
    claim_source = (_safe_claim_source_summary(claim_source)
                    if claim_source is not None else _claim_source_summary(
                        lessons, research_claims, research_source=research_source))
    if claim_source is None:
        claim_source = _claim_source_summary(
            lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    groups: dict[str, dict] = {}

    def _grp(statement, scope, metric=""):
        s = _claim_text(statement)
        if not s:
            return None
        sig = claim_signature(
            s, scope=_identity_text(scope, _MAX_DECISION_SCOPE),
            metric=_identity_text(metric, _MAX_DECISION_METRIC))
        if sig["polarity"] == 0:                     # no subject content -> not a claim
            return None
        g = groups.get(sig["merge_key"])
        if g is None:
            g = groups[sig["merge_key"]] = {
                "uid": sig["uid"], "contra_key": sig["contra_key"], "polarity": sig["polarity"],
                "scope": sig["scope"], "metric": sig["metric"],
                "support": set(), "oppose": set(), "unverified": set(),
                "runs": set(), "scopes": set(), "sources": set(), "verification": set(), "_ev": {}}
        g["_ev"][s] = g["_ev"].get(s, 0)             # candidate representative statements (evidence-weighted)
        return g

    for lz in lessons or []:
        g = _grp(lz.get("statement"), lz.get("task_id"), _metric_identity(lz))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(_identity_text(lz["run_id"], 500))
        if lz.get("task_id"):
            g["scopes"].add(_identity_text(lz["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        g["_ev"][_claim_text(lz.get("statement"))] += len(refs)

    for rc in research_claims or []:
        if not _indexable_research_claim(rc):
            continue
        g = _grp(rc.get("statement"), rc.get("task_id"), _metric_identity(rc))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(_identity_text(rc["run_id"], 500))  # D8 registers run/scope now
        if rc.get("task_id"):
            g["scopes"].add(_identity_text(rc["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            # unsupported/unclear/cited/legacy-unverified evidence is not counter-evidence; it simply has
            # not established the claim.  Keep the refs drillable without promoting them to support.
            g["unverified"].update(refs)
        g["_ev"][_claim_text(rc.get("statement"))] += len(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    # Contradiction map: a contra_key seen with BOTH polarities means two opposite claims about one subject
    # in one scope — the portfolio disagrees with itself at the ASSERTION level (unreachable from a single
    # merged statement). Each such claim is marked contested and carries its opposites' representative text.
    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}

    def _decision_for(g: dict, rep: str):
        overlay = decisions
        candidates = [g["uid"], claim_uid(rep, scope=g["scope"], metric=g["metric"])]
        if g["metric"]:
            candidates.append(claim_uid(rep, scope=g["scope"], metric=""))
        if g["metric"]:
            candidates.append(claim_uid(rep, scope="", metric=g["metric"]))
        candidates.append(claim_uid(rep, scope="", metric=""))
        seen = set()
        for uid in candidates:
            if uid and uid not in seen and isinstance(overlay.get(uid), dict):
                return overlay[uid]
            seen.add(uid)
        legacy_key = normalize_statement(rep)
        legacy = overlay.get(legacy_key)
        if (isinstance(legacy, dict) and not str(legacy.get("scope") or "")
                and not str(legacy.get("metric") or "")):
            return legacy
        global_legacy = overlay.get(_global_key(legacy_key))
        if (isinstance(global_legacy, dict) and not str(global_legacy.get("scope") or "")
                and not str(global_legacy.get("metric") or "")):
            return global_legacy
        return None

    prepared = []
    for g in groups.values():
        rep = max(g["_ev"], key=lambda s: (g["_ev"][s], s)) if g["_ev"] else ""
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        decision = _decision_for(g, rep)
        if decision is not None:
            decision = sanitize_cross_run_projection(
                decision, max_chars=16_000, max_items=64, max_total_items=256)
        prepared.append({"group": g, "statement": rep, "support": sup, "oppose": opp,
                         "unverified": unverified, "decision": decision,
                         "maturity": _dec.get((decision or {}).get("decision"), "machine-proposed")})

    # Keep a governance-independent contradiction map for the evidence digest. The live projection below
    # may hide a rejected opposite, but rejecting it must not make the reviewed proof revision change by
    # itself; only source evidence should age a decision.
    raw_contra: dict[str, dict[int, list]] = {}
    contra: dict[str, dict[int, list]] = {}
    for item in prepared:
        # Bound once per item: both maps key off the SAME group, and leaking `g` out of the first
        # branch would silently carry the previous item's group the moment either condition is
        # relaxed independently of the other.
        g = item["group"]
        if item["support"]:
            raw_contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)
        if item["maturity"] != "operator-rejected" and item["support"]:
            contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)

    out = []
    for item in prepared:
        g, rep = item["group"], item["statement"]
        sup, opp, unverified = item["support"], item["oppose"], item["unverified"]
        opposites = ([] if item["maturity"] == "operator-rejected" else
                     [og for pol, gs in contra.get(g["contra_key"], {}).items() if pol != g["polarity"]
                      for og in gs])
        contradicts = sorted({o["statement"] for o in opposites})
        raw_opposites = [og for pol, gs in raw_contra.get(g["contra_key"], {}).items()
                         if pol != g["polarity"] for og in gs]
        raw_contradicts = sorted({o["statement"] for o in raw_opposites})
        row = {
            "statement": rep,
            # a polarity contradiction is the strongest contested signal -> mixed even if this side's own
            # evidence is one-directional (that is exactly what the structured key makes reachable).
            "epistemic": ("mixed" if contradicts and sup
                           else _source_guarded_epistemic(sup, opp, claim_source)),
            "maturity": item["maturity"],
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]), "sources": sorted(g["sources"]),
            "verification": sorted(g["verification"]),
            "claim_uid": g["uid"], "scope": g["scope"], "polarity": g["polarity"],
            "metric": g["metric"],
            "decision": item["decision"], "contradicts": contradicts,
            "research_source": research_source,
            "claim_source": claim_source,
        }
        digest_row = {**row,
                      "epistemic": ("mixed" if raw_contradicts and sup
                                     else _source_guarded_epistemic(sup, opp, claim_source)),
                      "contradicts": raw_contradicts}
        row["evidence_digest"] = claim_evidence_digest(digest_row)
        decision_digest = str((item["decision"] or {}).get("evidence_digest") or "")
        row["decision_fresh"] = (decision_digest == row["evidence_digest"] if decision_digest else None)
        out.append(row)
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"],
                            0 if c["contradicts"] else 1, c["statement"]))
    return out


def claim_assessments(lessons: list[dict], *, research_claims: Optional[list[dict]] = None,
                      decisions: Optional[dict] = None, fuzzy: bool = False,
                      structured: bool = False, bounded: bool = True) -> list[dict]:
    """Project distilled `lessons` (+ optional D8 `research_claims`) into evidence-grounded claim
    assessments. Groups by normalized statement; each claim carries `support`/`oppose` node-id evidence,
    contributing `runs`/`scopes`, and an `epistemic` state. `decisions` (from `load_claim_decisions`)
    overlays an operator `maturity` (`operator-ratified`/`operator-rejected`/`operator-pinned`, else
    `machine-proposed`) — the §22.4 governance overlay. Sorted most-evidenced first. Pure.

    `structured` (opt-in, the full CR of the lean `fuzzy` merge) switches identity to the SCOPE+POLARITY-safe
    structured claim key (`claim_key.claim_signature`): claims from different tasks never merge, opposite
    polarity ("X helps" vs "X never helps") is a CONTRADICTION not a merge, and paraphrase/inflection
    variants collapse by exact structured key (O(n), no transitive over-merge). Mutually exclusive with the
    lean `fuzzy` path (structured wins)."""
    lessons = _valid_claim_source_rows(lessons, research=False)
    research_claims = _valid_claim_source_rows(research_claims, research=True)
    research_source = _research_source_summary(research_claims)
    claim_source = _claim_source_summary(
        lessons, research_claims, research_source=research_source)
    decisions = decisions if isinstance(decisions, dict) else {}
    if structured:
        rows = _structured_assessments(
            lessons, research_claims, decisions,
            research_source=research_source, claim_source=claim_source)
        projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
        return _ClaimAssessmentRows(
            projected, claim_source=claim_source, research_source=research_source)
    groups: dict[str, dict] = {}

    def _group(stmt: str) -> Optional[dict]:
        s = _claim_text(stmt)
        if not s:
            return None
        # NOTE: identity here is the normalized STATEMENT (the shipped lesson `normalize_statement`
        # key) — it can merge same-worded claims across incompatible scopes and the 160-char cap can
        # collide. A structured semantic claim key (subject/intervention/comparator/scope) is the CR1b TODO
        # (§21.20.13); this lean projection keeps scope/runs as metadata on the claim.
        return groups.setdefault(normalize_statement(s), {
            "statement": s, "support": set(), "oppose": set(),
            "unverified": set(), "runs": set(), "scopes": set(), "sources": set(),
            "verification": set()})

    for lz in lessons or []:
        g = _group(lz.get("statement"))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(_identity_text(lz["run_id"], 500))
        if lz.get("task_id"):
            g["scopes"].add(_identity_text(lz["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        # "noted"/unknown -> neutral: still registers the run/scope, but takes NO stance.

    for rc in research_claims or []:
        if not _indexable_research_claim(rc):
            continue
        g = _group(rc.get("statement"))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(_identity_text(rc["run_id"], 500))
        if rc.get("task_id"):
            g["scopes"].add(_identity_text(rc["task_id"], _MAX_DECISION_SCOPE))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            g["unverified"].update(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}
    out = []
    for key, g in groups.items():
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        overlay = decisions
        real_scopes = {str(scope) for scope in g["scopes"] if str(scope)}
        # A statement row spanning multiple tasks cannot safely receive any one task's policy.  For a
        # task-bound row, however, the exact scope-only decision outranks the portfolio-wide fallback.
        d = None
        if len(real_scopes) == 1:
            from looplab.engine.claim_key import claim_uid
            scope = next(iter(real_scopes))
            d = overlay.get(claim_uid(g["statement"], scope=scope, metric=""))
            # Compatibility for a custom lean overlay keyed by normalized statement+scope.
            if d is None:
                d = overlay.get(_scoped_key(key, scope))
        if d is None:
            d = overlay.get(key)
        # The lean projection groups by statement across tasks. A caller-supplied scoped decision may
        # therefore govern this row only when all contributing task scopes are that exact scope; unscoped
        # decisions remain the portfolio-wide fallback. The durable loader normally indexes scoped records
        # by structured UID only, but this guard also keeps custom/preloaded overlays fail-closed.
        if not isinstance(d, dict):
            d = None
        if d is not None:
            _dscope = str(d.get("scope") or "")
            if _dscope:
                if not real_scopes or not real_scopes <= {_dscope}:
                    d = None
        if d is None:
            d = overlay.get(_global_key(key))
        if not isinstance(d, dict):
            d = None
        if d is not None:
            d = sanitize_cross_run_projection(
                d, max_chars=16_000, max_items=64, max_total_items=256)
        out.append({
            "statement": g["statement"],
            "epistemic": _source_guarded_epistemic(sup, opp, claim_source),
            "maturity": _dec.get((d or {}).get("decision"), "machine-proposed"),
            "support": sup, "oppose": opp,
            "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]),
            "sources": sorted(g["sources"]), "verification": sorted(g["verification"]),
            "decision": d,
            "research_source": research_source,
            "claim_source": claim_source,
        })
    # most-evidenced first (support+oppose), contested claims break ties toward visibility, then statement
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    rows = _fuzzy_merge_claims(out) if fuzzy else out
    projected = [_bounded_claim_projection(row) for row in rows] if bounded else rows
    return _ClaimAssessmentRows(
        projected, claim_source=claim_source, research_source=research_source)


# --------------------------------------------------------------------------- #
# Step 5 (§21.20.5): a BOUNDED context pack for a proposing agent — evidence AND counter-arguments.
# --------------------------------------------------------------------------- #

_CAVEAT_STATES = ("mixed", "refuted", "inconclusive")


def _claim_research_source_summary(claims) -> Optional[dict]:
    """Return one coherent aggregate receipt carried by all rows in an assessment snapshot."""
    carried = _safe_research_source_summary(getattr(claims, "research_source", None))
    if carried is not None:
        return carried
    rows = [row for row in (claims if isinstance(claims, (list, tuple)) else [])
            if isinstance(row, dict)]
    explicit = [_safe_research_source_summary(row.get("research_source")) for row in rows
                if "research_source" in row]
    if not explicit:
        return None
    first = explicit[0]
    if first is not None and len(explicit) == len(rows) and all(item == first for item in explicit[1:]):
        return first
    # A mixed/malformed snapshot is lower-bound evidence. Keep known counts for diagnosis, but fail the
    # completeness gate so no pack or steward can infer an exact positive from incompatible rows.
    base = first or _research_source_summary([])
    unknown = max(1, base["producer_unknown_runs"])
    runs = max(base["producer_runs"], unknown + base["producer_partial_runs"])
    return {
        **base,
        "source_complete": False,
        "producer_receipt_known": False,
        "producer_complete": False,
        "producer_runs": runs,
        "producer_unknown_runs": unknown,
    }


def _claim_claim_source_summary(claims) -> Optional[dict]:
    """Return one coherent lessons+research authority receipt, including for an empty snapshot."""
    carried = _safe_claim_source_summary(getattr(claims, "claim_source", None))
    if carried is not None:
        return carried
    rows = [row for row in (claims if isinstance(claims, (list, tuple)) else [])
            if isinstance(row, dict)]
    explicit = [_safe_claim_source_summary(row.get("claim_source")) for row in rows
                if "claim_source" in row]
    if not explicit:
        return None
    first = explicit[0]
    if first is not None and len(explicit) == len(rows) and all(item == first for item in explicit[1:]):
        return first
    return _unknown_claim_source_summary()


def build_context_pack(claims: list[dict], *, concept_overview: Optional[dict] = None,
                       max_claims: int = 5,
                       _concept_rows: Optional[list[dict]] = None,
                       _research_source: Optional[dict] = None,
                       _claim_source: Optional[dict] = None) -> dict:
    """Assemble a CLAIM-COUNT-bounded cross-run context pack from claim assessments (+ an optional concept
    overview) for a proposing agent (§21.20.5, Step 5). ("Claim-count", not token/byte: the pack caps the
    number of claims + per-claim field lengths; a true serialized-token envelope is the CR2b TODO — see the
    NOTE below.) The design's hard rule is that positive hits must
    never crowd out caveats. Precedence is pinned → ratified → mixed → supported → refuted →
    inconclusive, and a **caveat slot is reserved** whenever it can be filled by replacing the weakest
    non-pinned positive. The hard claim cap is never exceeded; pins beyond it are reported as omitted.
    Pure/deterministic and
    'silent' by construction — it just returns structured data; promoting it to advisory prompt-grounding
    is a separate, gated step (never wired here). No LLM, no I/O."""
    # NOTE: this bounds by CLAIM COUNT + per-claim field caps (below), not a serialized token/byte
    # budget — a true token envelope is the CR2b TODO. `max_claims<1` is normalized to 1.
    max_claims = max(1, min(int(max_claims), _MAX_CONTEXT_CLAIMS))
    # Governance precedence is explicit: rejected is absent; pinned is retention-critical; ratified is the
    # next preference; then evidence ordering. A caveat may replace a non-pinned positive, never a pin.
    live = [c for c in (claims or []) if c.get("maturity") != "operator-rejected"]
    _kept = {"operator-pinned", "operator-ratified"}
    pinned = [c for c in live if c.get("maturity") == "operator-pinned"]
    ratified = [c for c in live if c.get("maturity") == "operator-ratified"]
    rest = [c for c in live if c.get("maturity") not in _kept]
    by_state: dict[str, list] = {"mixed": [], "supported": [], "refuted": [], "inconclusive": []}
    for c in rest:
        by_state.get(c["epistemic"], by_state["inconclusive"]).append(c)
    ordered = (pinned + ratified + by_state["mixed"] + by_state["supported"]
               + by_state["refuted"] + by_state["inconclusive"])
    picked = ordered[:max_claims]
    # Reserved caveat slot: if nothing picked carries a caveat but caveats exist, swap the weakest NON-kept
    # picked (a governance-retained claim is never evicted to make room) for the strongest available caveat —
    # opposition is never crowded out by a full slate of positives (§20.5). Kept caveats count as caveats too.
    if picked and not any(c["epistemic"] in _CAVEAT_STATES for c in picked):
        # Include RATIFIED caveats too: a ratified mixed/refuted/inconclusive claim pushed past max_claims by
        # the ratified block must still be able to fill the reserved slot, or a slate of ratified-supported
        # claims could crowd opposition out — the exact §20.5 rule this slot exists to protect.
        caveats = ([c for c in pinned if c["epistemic"] in _CAVEAT_STATES]
                   + [c for c in ratified if c["epistemic"] in _CAVEAT_STATES]
                   + by_state["mixed"] + by_state["refuted"] + by_state["inconclusive"])
        # Evict the weakest non-pinned positive. Ratification raises priority but may still yield to a
        # caveat; a pin is the explicit retention guarantee and cannot be displaced. If the cutoff is all
        # pins there is no legal victim, so the caveat remains outside this bounded projection.
        victim = next((i for i in range(len(picked) - 1, -1, -1)
                       if picked[i].get("maturity") != "operator-pinned"), None)
        if caveats and victim is not None:
            picked = picked[:victim] + picked[victim + 1:] + [caveats[0]]

    def _slim(c: dict) -> dict:
        # Evidence refs are run-QUALIFIED ("run:node"), so the truncated support/oppose lists stay citable;
        # keep runs/scopes too so a reader can resolve the claim's provenance.
        return {"statement": _claim_text(c.get("statement"), 300), "epistemic": c["epistemic"],
                "maturity": c.get("maturity", "machine-proposed"),
                "claim_uid": c.get("claim_uid", ""), "scope": c.get("scope", ""),
                "evidence_digest": c.get("evidence_digest", ""),
                "decision_fresh": c.get("decision_fresh"),
                "metric": c.get("metric", ""), "polarity": c.get("polarity"),
                "n_support": c["n_support"], "n_oppose": c["n_oppose"],
                "n_unverified": c.get("n_unverified", 0),
                "support": c["support"][:6], "oppose": c["oppose"][:6],
                "unverified": c.get("unverified", [])[:6],
                # Structured polarity contradictions are assertion-level counter-evidence,
                # not entries in ``oppose``. Keep their bounded text or a mixed claim renders as 1↑/0↓
                # with no visible reason for the disagreement.
                "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
                "runs": [_identity_text(value, 500) for value in c.get("runs", [])[:6]],
                "scopes": [_identity_text(value, _MAX_DECISION_SCOPE)
                           for value in c.get("scopes", [])[:6]]}

    pack = {
        "claims": [_slim(c) for c in picked],
        "n_claims_total": len(claims or []),
        "n_contested": sum(1 for c in live if c.get("epistemic") == "mixed"),
        # Pins have highest priority but cannot override the hard prompt-size cap. Surface any overflow
        # explicitly so a bounded advisory never implies that it retained every operator pin.
        "n_pinned_total": len(pinned),
        "n_pinned_omitted": max(0, len(pinned) - sum(
            1 for c in picked if c.get("maturity") == "operator-pinned")),
    }
    research_source = (_safe_research_source_summary(_research_source)
                       if _research_source is not None
                       else _claim_research_source_summary(claims))
    if _research_source is not None and research_source is None:
        research_source = {
            **_research_source_summary([]),
            "source_complete": False,
            "producer_receipt_known": False,
            "producer_complete": False,
            "producer_runs": 1,
            "producer_unknown_runs": 1,
        }
    if research_source is not None:
        pack["research_source"] = research_source
    claim_source = (_safe_claim_source_summary(_claim_source)
                    if _claim_source is not None else _claim_claim_source_summary(claims))
    if _claim_source is not None and claim_source is None:
        claim_source = _unknown_claim_source_summary()
    if claim_source is not None:
        pack["claim_source"] = claim_source
    if concept_overview:
        from looplab.engine.memory import concept_profit_tendencies
        # callers that own the retained capsule snapshot may supply its private pre-cap rows.
        # The pack still emits only `max_claims` labels/tendencies; this prevents the public overview's
        # display cap from becoming a silent analytics cap while keeping the outward prompt bounded.
        row_source = (_concept_rows if _concept_rows is not None
                      else concept_overview.get("concepts"))
        rows = [e for e in (row_source or []) if isinstance(e, dict)]
        source_complete = concept_overview.get("source_complete") is True
        # PART V Phase 1 profit signal: surface concepts with a CONSISTENT, MULTI-RUN rank tendency (advisory
        # only — prompts, never selection). The threshold lives in ONE shared helper so the context pack and
        # the cross_run_atlas tool can never diverge; a concept with mixed/thin evidence appears in neither.
        # consistency also needs a complete denominator. A non-matching partial capsule may
        # have omitted this exact concept and an opposite sign, so retained positive rows remain observable
        # below but cannot support a directional portfolio tendency until every capsule receipt is exact.
        tendency = (concept_profit_tendencies(rows, limit=max_claims) if source_complete
                    else {"helps": [], "hurts": []})
        pack["coverage"] = {
            "n_runs": concept_overview.get("n_runs", 0),
            "n_concepts": concept_overview.get("n_concepts", 0),
            # A hand-built/older overview with no receipt is UNKNOWN, never silently exact.
            "source_complete": source_complete,
            "partial_capsules": concept_overview.get(
                "partial_capsules",
                concept_overview.get("n_runs", 0) if "source_complete" not in concept_overview else 0),
            "source_unknown_capsules": concept_overview.get(
                "source_unknown_capsules",
                concept_overview.get("n_runs", 0) if "source_complete" not in concept_overview else 0),
            "source_concepts_omitted": concept_overview.get("source_concepts_omitted", 0),
            "source_outcomes_omitted": concept_overview.get("source_outcomes_omitted", 0),
            "source_store_complete": concept_overview.get(
                "source_store_complete", source_complete) is True,
            "source_rows_total": concept_overview.get("source_rows_total", 0),
            "source_rows_quarantined": concept_overview.get("source_rows_quarantined", 0),
            "source_malformed_rows": concept_overview.get("source_malformed_rows", 0),
            "source_invalid_capsule_rows": concept_overview.get(
                "source_invalid_capsule_rows", 0),
            "source_duplicate_run_rows": concept_overview.get("source_duplicate_run_rows", 0),
            "top_concepts": [_claim_text(e.get("concept"), 500) for e in rows[:max_claims]],
            # E3: keep the run COUNT (n_helped/n_hurt) in the rendered span — "loss/contrastive (n=7)"
            # vs "(n=2)" tells the Researcher how strong the multi-run tendency is, not just its direction.
            "helps": [f"{_claim_text(c, 480)} (n={int(n)})" for c, n in tendency["helps"]],
            "hurts": [f"{_claim_text(c, 480)} (n={int(n)})" for c, n in tendency["hurts"]],
        }
    return pack


# Deterministic query-INTENT cues (CR2a eligibility). Kept ML-context-safe: ambiguous technique words
# ("negative", "loss") are NOT cues, so "hard negatives for retrieval" reads as neutral EXPLORE, not FAILED.
_INTENT_CUES = {
    "failed":    frozenset("fail failed failing avoid avoided pitfall pitfalls mistake mistakes wrong "
                           "broke broken regress regression hurt hurts degrade degrades harmful useless "
                           "ineffective".split()),
    "contested": frozenset("contested contradict contradiction conflict conflicting disagree disagreement "
                           "controversial controversy debate unclear uncertain".split()),
    "worked":    frozenset("best proven effective recommend recommended success successful reliable robust "
                           "winning champion".split()),
}
# The CONTRADICTION pool for the retrieval quota — claims that carry actual OPPOSITION (mixed=contested,
# refuted=negative verdict). This is DELIBERATELY narrower than build_context_pack's `_CAVEAT_STATES`
# (which also includes `inconclusive`): the context-pack reserves a slot so a clean slate of positives can't
# hide any NON-positive (§21.20.5 coverage), whereas the retrieval quota reserves slots specifically for
# COUNTER-EVIDENCE/contradictions — an inconclusive (no-stance) claim is neither. Two distinct mechanisms,
# not an accidental inconsistency (concept-conformance).
_CAVEAT = frozenset(("mixed", "refuted"))
# Tie-break order for `_classify_intent` when two intents match the same number of cues: the intents that
# RAISE the caveat/contradiction quota win, so a mixed query surfaces counter-evidence rather than burying
# it. `contested` outranks `failed` because it is the narrower, more specific signal of the two.
_INTENT_TIE_RANK = {"contested": 2, "failed": 1, "worked": 0}


def _classify_intent(query: str) -> str:
    """Map a free-text query to a retrieval INTENT (failed / contested / worked / explore) by cue overlap.
    Deterministic, no LLM. `explore` (neutral) when no cue fires — the safe default that reorders nothing."""
    toks = set(_CLAIM_WORD.findall(str(query or "").casefold()))
    scored = [(sum(1 for w in cues if w in toks), name) for name, cues in _INTENT_CUES.items()]
    # An equal cue count is broken CAVEAT-FIRST, not alphabetically. The tie-break used to be the intent
    # NAME, which always resolves to the alphabetically-largest — "worked" > "failed" > "contested" — so a
    # genuinely mixed query ("avoid the failed approach, use the best proven method": one failed cue, one
    # worked cue) classified as "worked", floating positives and leaving the caveat/contradiction quota
    # unraised (only failed/contested raise it). That biased ties toward HIDING counter-evidence, the exact
    # inverse of this module's §21.20.5 caveat-preservation intent. Still total and deterministic.
    best_n, best = max(scored, key=lambda t: (t[0], _INTENT_TIE_RANK.get(t[1], 0)))
    return best if best_n else "explore"


def _eligible(kind: str, meta: dict, intent: str) -> bool:
    """Whether a doc is on-INTENT (a soft priority signal, never a hard exclusion — counter-evidence is
    still returned). Concepts are always eligible; a claim's eligibility depends on its epistemic/maturity."""
    if kind != "claim" or intent == "explore":
        return True
    ep, mat = meta.get("epistemic"), meta.get("maturity")
    if intent == "failed":
        return ep in _CAVEAT
    if intent == "contested":
        return ep == "mixed"
    if intent == "worked":
        return ep == "supported" or mat == "operator-ratified"
    return True


_INTENTS = ("failed", "contested", "worked", "explore")

# Document ids are a stable identity for the same searchable statement/concept.  The corpus digest has a
# separate schema because it also commits to aggregate source receipts that do not belong in every doc id.
_RETRIEVAL_DOCUMENT_VERSION = 2
_RETRIEVAL_CORPUS_VERSION = 7
_INTENT_SCORE_BONUS = 0.001
_CAVEAT_SCORE_RATIO = 0.50
_CAVEAT_QUERY_COVERAGE = 0.10


def _retrieval_tokens(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return frozenset(_CLAIM_WORD.findall(normalized))


def _lexical_relevance(query: str, text: str) -> tuple[int, float, float]:
    q, d = _retrieval_tokens(query), _retrieval_tokens(text)
    shared = len(q & d)
    coverage = shared / len(q) if q else 0.0
    jaccard = shared / len(q | d) if q or d else 0.0
    return shared, coverage, jaccard


def _json_digest(value, *, length: int = 20) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _retrieval_doc(kind: str, text: str, meta: dict) -> tuple[str, str, dict]:
    identity = {"v": _RETRIEVAL_DOCUMENT_VERSION, "kind": kind,
                "claim_uid": str(meta.get("claim_uid") or ""),
                "metric": str(meta.get("metric") or ""),
                "text": " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())}
    stable_id = f"{kind[:1]}_{_json_digest(identity, length=16)}"
    return kind, str(text or ""), {**meta, "stable_id": stable_id}


def _retrieval_corpus_digest(docs, *, concept_source: dict, research_source: dict,
                             claim_source: dict) -> str:
    canonical = [{"kind": kind, "text": text, "meta": meta}
                 for kind, text, meta in sorted(docs, key=lambda d: d[2]["stable_id"])]
    envelope = {"v": _RETRIEVAL_CORPUS_VERSION, "docs": canonical,
                "concept_source": concept_source, "research_source": research_source,
                "claim_source": claim_source}
    return _json_digest(envelope, length=20)


def _preselect_retrieval_docs(docs, query: str, limit: int):
    """Cheap query-aware cap with one best row per source kind before the expensive hybrid index."""
    cap = max(1, int(limit))
    if len(docs) <= cap:
        return list(docs)
    stats = [_lexical_relevance(query, d[1]) for d in docs]
    ranked = sorted(range(len(docs)),
                    key=lambda i: (-stats[i][0], -stats[i][1], -stats[i][2],
                                   docs[i][2]["stable_id"]))
    selected: list[int] = []
    kinds = sorted({d[0] for d in docs})
    if cap >= len(kinds):
        for kind in kinds:
            selected.append(next(i for i in ranked if docs[i][0] == kind))
    selected_set = set(selected)
    selected.extend(i for i in ranked if i not in selected_set)
    return [docs[i] for i in selected[:cap]]


def cross_run_retrieve(memory_dir, query: str, *, k: int = 8, lessons=None, capsules=None,
                       research_claims=None, scope_task: str = "", contradiction_quota: float = 0.34,
                       max_corpus: int = 2000, structured: bool = False, intent: Optional[str] = None,
                       scope_receipt: Optional[dict] = None,
                       _governance: Optional[dict] = None) -> dict:
    """CR2a retrieval planner (§21.20.5, full CR): RRF-fuse the portfolio's cross-run KNOWLEDGE — claims
    (epistemic state / operator maturity) + concepts (#runs) — over the shipped `HybridRetriever`
    (lexical + BM25 + vector; reuses hybrid_merge, NO new fuser), then shape the ranked recall with:

    - INTENT classification (`failed`/`contested`/`worked`/`explore`) → an eligibility priority so an
      on-intent claim floats up (soft; never hides counter-evidence);
    - a CONTRADICTION QUOTA reserving ~`contradiction_quota` of the k slots for caveat (mixed/refuted)
      claims when they exist, so a positive-heavy recall never buries the counter-evidence (mirrors the
      context pack's caveat slot). `failed`/`contested` intents raise the quota;
    - a bounded corpus (`max_corpus`, truncation REPORTED not silent) + a why-recalled RECEIPT (intent,
      quota, corpus digest, degraded-channel note, per-hit rank).

    Every source is SCOPED before indexing: pass scoped `lessons`/`capsules` plus their aggregate
    `scope_receipt`, and `scope_task` filters the D8 research claims to that task so a task-bound agent
    cannot retrieve another task's claims.
    Operator-rejected claims never enter the corpus. Advisory; pure w.r.t. the passed/loaded stores."""
    from pathlib import Path

    from looplab.engine.governance_health import observed_path_missing, project_governed_sources
    from looplab.engine.memory import (ConceptCapsuleStore, _filter_capsule_rows,
                                       _portfolio_concept_overview_data)
    if _governance is None:
        source_names = []
        if lessons is None:
            source_names.append("lessons.jsonl")
        if research_claims is None:
            source_names.append("research_claims.jsonl")
        if capsules is None:
            source_names.append("concept_capsules.jsonl")
        return project_governed_sources(
            memory_dir,
            lambda governance: cross_run_retrieve(
                memory_dir, query, k=k, lessons=lessons, capsules=capsules,
                research_claims=research_claims, scope_task=scope_task,
                contradiction_quota=contradiction_quota, max_corpus=max_corpus,
                structured=structured, intent=intent, scope_receipt=scope_receipt,
                _governance=governance,
            ),
            include_concepts=True, source_names=source_names,
        )
    base = Path(memory_dir) if memory_dir else None
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = (ConceptCapsuleStore(cp).all()
                    if cp and not observed_path_missing(cp) else [])
    if lessons is None:
        lessons = load_claim_lessons(memory_dir)
    lessons = _valid_claim_source_rows(lessons, research=False)
    # Scope EVERY source before joining. Decisions are a governance overlay; they never grant visibility.
    research = load_research_claims(memory_dir) if research_claims is None else research_claims
    if scope_task:
        wanted = str(scope_task)
        lessons = _filter_claim_source_rows(
            lessons, lambda r: str(r.get("task_id") or "") == wanted, research=False)
        capsules = _filter_capsule_rows(
            capsules, lambda r: str(r.get("task_id") or "") == wanted)
        research = _filter_claim_source_rows(
            research, lambda r: str(r.get("task_id") or "") == wanted, research=True)
    research = _valid_claim_source_rows(research, research=True)
    research_source = _research_source_summary(research)
    governance = _governance
    claims = _filter_claim_assessments(
        claim_assessments(lessons, research_claims=research,
                          decisions=governance["decisions"], structured=structured),
        lambda c: c.get("maturity") != "operator-rejected")
    claim_source = (_safe_claim_source_summary(claims.claim_source)
                    or _claim_source_summary(lessons, research, research_source=research_source))
    overview, concept_rows = _portfolio_concept_overview_data(
        capsules, aliases=governance["aliases"], splits=governance["splits"])
    # source completeness is part of the retrieval corpus, even when a query happens to match
    # only claims or the same retained concept rows.  Aggregate it across every eligible capsule before
    # query preselection so legacy/omitted concepts cannot masquerade as authoritative absence or exact
    # frequency, and a partial->complete transition changes the auditable corpus identity.
    concept_source = {
        "n_capsules": overview["n_runs"],
        "source_complete": overview.get("source_complete") is True,
        "partial_capsules": int(overview.get("partial_capsules", 0) or 0),
        "source_unknown_capsules": int(overview.get("source_unknown_capsules", 0) or 0),
        "source_concepts_omitted": int(overview.get("source_concepts_omitted", 0) or 0),
        "source_outcomes_omitted": int(overview.get("source_outcomes_omitted", 0) or 0),
        # The public overview is independently bounded. Commit both its display omission and the exact
        # retained concept cardinality to the corpus identity so a cap change/tail cannot look identical.
        "concepts_total": len(concept_rows),
        "overview_concepts_omitted": int(overview.get("concepts_omitted", 0) or 0),
        "source_store_complete": overview.get("source_store_complete") is True,
        "source_rows_total": int(overview.get("source_rows_total", 0) or 0),
        "source_rows_quarantined": int(overview.get("source_rows_quarantined", 0) or 0),
        "source_malformed_rows": int(overview.get("source_malformed_rows", 0) or 0),
        "source_invalid_capsule_rows": int(
            overview.get("source_invalid_capsule_rows", 0) or 0),
        "source_duplicate_run_rows": int(overview.get("source_duplicate_run_rows", 0) or 0),
    }
    scope_keys = (
        "scope_unknown_capsules", "scope_fingerprint_unknown_capsules",
        "scope_fingerprint_items_omitted", "scope_direction_unknown_capsules",
    )
    if scope_receipt is None:
        scope_source = {"scope_receipt_known": True, "scope_complete": True,
                        **{key: 0 for key in scope_keys}}
    else:
        source = scope_receipt if isinstance(scope_receipt, dict) else {}
        counts_valid = all(
            isinstance(source.get(key), int) and not isinstance(source.get(key), bool)
            and source.get(key) >= 0 for key in scope_keys
        )
        complete_valid = type(source.get("scope_complete")) is bool
        unknown = source.get("scope_unknown_capsules") if counts_valid else 0
        fingerprint_unknown = source.get("scope_fingerprint_unknown_capsules", 0) if counts_valid else 0
        direction_unknown = source.get("scope_direction_unknown_capsules", 0) if counts_valid else 0
        consistent = (complete_valid and counts_valid
                      and source.get("scope_complete") == (unknown == 0)
                      and fingerprint_unknown + direction_unknown <= unknown)
        scope_source = {
            "scope_receipt_known": consistent,
            # a caller-supplied malformed applicability receipt fails closed. Retrieval may
            # retain its positive documents, but neither an empty result nor a frequency is exact.
            "scope_complete": consistent and source.get("scope_complete") is True,
            **{key: source.get(key) if counts_valid else 0 for key in scope_keys},
        }
    concept_source.update(scope_source)
    docs: list[tuple[str, str, dict]] = []
    for c in claims:
        evidence_digest = _json_digest({"support": c.get("support", []), "oppose": c.get("oppose", []),
                                        "unverified": c.get("unverified", []),
                                        "sources": c.get("sources", []),
                                        "research_source": c.get("research_source"),
                                        "claim_source": c.get("claim_source")})
        docs.append(_retrieval_doc("claim", c["statement"], {
            "epistemic": c["epistemic"], "n_support": c["n_support"],
            "n_oppose": c["n_oppose"], "n_unverified": c.get("n_unverified", 0),
            "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
            "maturity": c.get("maturity"), "claim_uid": c.get("claim_uid", ""),
            "decision_fresh": c.get("decision_fresh"),
            "metric": c.get("metric", ""), "scopes": c.get("scopes", []),
            "research_source": c.get("research_source", research_source),
            "claim_source": c.get("claim_source", claim_source),
            "decision_revision": (c.get("decision") or {}).get("revision"),
            "governance_digest": _json_digest(c.get("decision") or {}),
            "evidence_digest": evidence_digest}))
    # query-aware preselection must see every validated canonical row. Iterating the public
    # top-512 projection made concept #513 look absent with source_complete=true and truncated=0.
    for e in concept_rows:
        docs.append(_retrieval_doc("concept", _claim_text(e.get("concept"), 500), {
            "n_runs": e["n_runs"],
            "runs": [_identity_text(r.get("run_id"), 500) for r in e["runs"][:5]
                     if isinstance(r, dict)],
            "evidence_digest": _json_digest(e["runs"])}))

    n_total = len(docs)
    max_corpus = max(1, min(int(max_corpus), _MAX_RETRIEVAL_CORPUS))
    indexed_docs = _preselect_retrieval_docs(docs, str(query or ""), max_corpus)
    truncated = n_total - len(indexed_docs)
    concepts_indexed = sum(kind == "concept" for kind, _text, _meta in indexed_docs)
    claims_indexed = sum(kind == "claim" for kind, _text, _meta in indexed_docs)
    projection_receipt = {
        "concepts_indexed": concepts_indexed,
        "concepts_omitted": len(concept_rows) - concepts_indexed,
        "claims_total": len(claims),
        "claims_indexed": claims_indexed,
        "claims_omitted": len(claims) - claims_indexed,
    }
    corpus_digest = _retrieval_corpus_digest(
        docs, concept_source=concept_source, research_source=research_source,
        claim_source=claim_source)
    # COST, stated exactly. `max_corpus` bounds the hybrid INDEX (`indexed_docs`) and the
    # `retrieval_digest` below is computed over that bounded set — but this `corpus_digest` covers
    # every row, sorting and serializing each full claim/concept. That is inherent to what it means:
    # a corpus REVISION has to change when any stored row changes, so a bounded sample cannot express
    # it. It is also not the only O(n) term — building `docs` in full is deliberate and load-bearing
    # (see the preselection note above: iterating the public top-512 projection made concept #513 look
    # absent with source_complete=true and truncated=0), so preselection has already visited every row
    # before this runs. One retrieval request is therefore O(all stored evidence) in CPU and memory
    # regardless of `max_corpus`, and portfolio growth scales it.
    # Fixing that is not a local change: it needs the revision PERSISTED and maintained incrementally
    # as rows are written, plus a bounded candidate index that can still emit an honest omission
    # receipt. That is scalability infrastructure over the durable claim/concept stores — tracked work,
    # not a repair — and a partial version (e.g. combining per-row hashes here) would change the
    # receipt value that governance consumers compare while still leaving the request O(n).
    indexed_source = {**concept_source, **projection_receipt}
    retrieval_source = {**indexed_source, "research_source": research_source,
                        "claim_source": claim_source}
    # The AGENT may pass an explicit `intent` (it knows why it is searching — genuinely agentic); otherwise
    # classify deterministically from the query text. An unknown value falls back to classification.
    intent = intent if intent in _INTENTS else _classify_intent(query)
    kk = max(1, min(int(k), _MAX_RETRIEVAL_HITS))
    try:
        base_quota = float(contradiction_quota)
    except (TypeError, ValueError):
        base_quota = 0.34
    if not math.isfinite(base_quota):
        base_quota = 0.34
    base_quota = min(1.0, max(0.0, base_quota))
    q = max(base_quota, 0.5) if intent in ("failed", "contested") else base_quota
    target = min(math.ceil(kk * q), max(0, kk - 1))
    # A why-recalled receipt: corpus revision (content digest), the degraded vector-channel semantics, the
    # classified intent + quota, and (below) the per-hit rank — enough to explain/reproduce a result.
    receipt = {"query": _claim_text(query, 4000), "k": kk, "n_corpus": n_total,
               "n_indexed": len(indexed_docs), "corpus_digest_version": _RETRIEVAL_CORPUS_VERSION,
               "channels": ["lexical", "bm25", "vector"], "intent": intent,
               "vector_channel": "hash_embed(64-bucket bag-of-words; lexical proxy, not semantic)",
               "corpus_digest": corpus_digest,
               "retrieval_digest": _retrieval_corpus_digest(
                   indexed_docs, concept_source=indexed_source,
                   research_source=research_source, claim_source=claim_source),
               "truncated": truncated,
               "preselection": "query-overlap+one-per-source/v1",
               "contradiction_quota": round(base_quota, 3),
               "effective_quota": round(q, 3), "caveat_target": target,
               "caveat_score_ratio": _CAVEAT_SCORE_RATIO,
                "caveat_query_coverage": _CAVEAT_QUERY_COVERAGE,
                "intent_score_bonus": _INTENT_SCORE_BONUS,
                "governance_complete": True,
                "claim_governance_revision": governance["claim_revision"],
                "concept_alias_revision": governance["alias_revision"],
                "concept_split_revision": governance["split_revision"],
                "concept_governance_revision": governance["concept_governance_revision"],
                **retrieval_source}
    if not indexed_docs or not str(query or "").strip():
        return {"results": [], "receipt": {**receipt, "n_hits": 0, "n_caveats": 0}}

    from looplab.search.hybrid_merge import HybridRetriever
    # Retrieve a POOL larger than k so the intent priority + contradiction quota have room to reorder/swap
    # without extra queries; the vector channel is the `hash_embed` bag-of-words (a lexical proxy — declared
    # in the receipt, not passed off as semantic retrieval).
    pool_n = min(len(indexed_docs), max(kk * 4, kk + 12))
    pool = HybridRetriever([t for _, t, _ in indexed_docs]).candidates(str(query), k=pool_n)
    ranked = []
    for rel_rank, (i, score) in enumerate(pool):
        kind, text, meta = indexed_docs[i]
        shared, coverage, jaccard = _lexical_relevance(str(query), text)
        eligible = _eligible(kind, meta, intent)
        # Intent is a bounded tiebreak-like bonus scaled by actual query overlap, never a hard tier that can
        # lift an unrelated "failed" memory above a strongly relevant positive result.
        bonus = (_INTENT_SCORE_BONUS * min(1.0, coverage * 2.0)
                 if intent != "explore" and eligible and shared else 0.0)
        ranked.append({"idx": i, "kind": kind, "text": text, "score": round(float(score), 6),
                       "intent_bonus": round(bonus, 6), "query_overlap": shared,
                       "query_coverage": round(coverage, 4), "query_jaccard": round(jaccard, 4),
                       "rel_rank": rel_rank, **meta})
    ranked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]), h["rel_rank"], h["stable_id"]))
    picked = ranked[:kk]

    # CONTRADICTION QUOTA: guarantee ~quota of the k slots are caveat (mixed/refuted) claims when the pool
    # has them — swapping the LEAST-relevant non-caveat picks (from the bottom) for the most-relevant unpicked
    # caveats, so the top relevance hit is never displaced and opposition is never crowded out.
    # ceil(k*q) caveat slots, but capped at k-1 so the #1 relevance hit is NEVER evicted (at k=1 the target
    # is 0 — the single slot stays the top hit, as the swap contract promises; mega-review finding).
    have = [h for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT]
    if target > len(have):
        picked_ids = {h["idx"] for h in picked}
        top_score = max((h["score"] for h in ranked), default=0.0)
        extra = [h for h in ranked if h["idx"] not in picked_ids
                 and h["kind"] == "claim" and h.get("epistemic") in _CAVEAT
                 and h["query_coverage"] >= _CAVEAT_QUERY_COVERAGE
                 and h["score"] >= top_score * _CAVEAT_SCORE_RATIO]
        need = target - len(have)
        for cav in extra[:need]:
            # Keep the raw relevance winner (rel_rank 0). Quotas reserve relevant counter-evidence, not an
            # unrelated caveat selected solely for its epistemic label. Also NEVER evict an operator-PINNED
            # claim — the "pinned is retained" governance projection applies to EVERY consumer, not just the
            # context pack (concept-conformance: §22.4 / §21.20.5, mirroring build_context_pack).
            victim = next((h for h in reversed(picked)
                           if not (h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
                           and h["rel_rank"] != 0 and h.get("maturity") != "operator-pinned"), None)
            if victim is None:
                break
            picked[picked.index(victim)] = cav
        picked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]),
                                   h["rel_rank"], h["stable_id"]))

    n_caveats = sum(1 for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
    results = [{k2: v for k2, v in h.items() if k2 != "idx"} for h in picked]
    # Report the EFFECTIVE quota actually applied (raised for failed/contested) + the reserved caveat target,
    # so the receipt explains why a contested claim was (or wasn't) surfaced — not just the configured base.
    return {"results": results,
            "receipt": {**receipt, "n_hits": len(results), "n_caveats": n_caveats}}


def portfolio_atlas(lessons: list[dict], capsules: list[dict], *, max_items: int = 8,
                    decisions: Optional[dict] = None, research_claims: Optional[list[dict]] = None,
                    aliases: Optional[dict] = None, splits: Optional[dict] = None,
                    structured: bool = False) -> dict:
    """The Research Atlas DATA payload (§21.20 Step 6): one structured bounded observation/mixed-evidence
    view, composing the concept overview (Step 3), the claim
    assessments (Step 4) and the bounded context pack (Step 5). Pure/deterministic — the read-model a
    Research Atlas UI (or an agent) would render; no LLM, no I/O.

    The legacy ``thin_coverage`` field means only "observed in one returned run". It is not a gap or coverage
    assertion: a true CoverageFrame (§20.6, unknown-vs-zero) needs a frozen scope, eligible denominator and
    health contract, which remain deferred full-CR3a work."""
    from looplab.engine.memory import _dedup_valid_capsules, _portfolio_concept_overview_data
    max_items = max(1, min(int(max_items), 100))             # route/CLI-independent hard envelope
    source_capsules = capsules if isinstance(capsules, (list, tuple)) else []
    capsules = _dedup_valid_capsules(source_capsules)
    overview, full_concept_rows = _portfolio_concept_overview_data(
        capsules, aliases=aliases, splits=splits)
    # Keep the complete internal sets for exact run totals and the governance evidence digest. Only the
    # outward contradictions/context projections are capped below.
    claims = claim_assessments(lessons, research_claims=research_claims, decisions=decisions,
                               structured=structured, bounded=False)
    research_source = (_safe_research_source_summary(getattr(claims, "research_source", None))
                       or _research_source_summary(
                           _valid_claim_source_rows(research_claims, research=True)))
    claim_source = (_safe_claim_source_summary(getattr(claims, "claim_source", None))
                    or _claim_source_summary(lessons, research_claims,
                                             research_source=research_source))
    # A contradiction the operator REJECTED is no longer live, consistent with build_context_pack and
    # cross_run_claims. Pin priority applies inside the embedded context pack; this human-facing contested
    # summary remains evidence-ordered and independently capped.
    contested = [c for c in claims if c["epistemic"] == "mixed" and c.get("maturity") != "operator-rejected"]
    # Atlas is independently bounded. Derive single-run observations and rank tendencies from
    # every canonical retained row BEFORE its outward cap; the old overview-capped path silently returned
    # `thin_coverage=[]` once 512 more-frequent concepts occupied the entire overview projection.
    thin = [e["concept"] for e in full_concept_rows if e["n_runs"] == 1]
    # Run count spans BOTH sources — capsules AND the runs cited by lessons — so a lesson-only / legacy
    # memory (no opt-in capsules) is not reported as zero runs. The authoritative scoped corpus
    # join (cross_run_index) is the full-CR TODO; this at least unions what the two memory stores know.
    run_ids = {c.get("run_id") for c in capsules if c.get("run_id")}
    for cl in claims:
        run_ids.update(cl.get("runs") or [])
    n_runs = len(run_ids)
    # Keep the embedded context-pack coverage n_runs CONSISTENT with the top-level count (both the union of
    # capsule + lesson-cited runs), so one atlas payload never reports two different run counts — otherwise a
    # lesson-only memory says n_runs>0 at the top but coverage.n_runs==0, the very "zero runs" artifact the
    # union set out to fix.
    pack_overview = {**overview, "n_runs": n_runs}
    explored = full_concept_rows[:max_items]
    thin_coverage = thin[:max_items]
    contradictions = [_bounded_claim_projection(row) for row in contested[:max_items]]
    payload = {
        "n_runs": n_runs, "n_concepts": overview["n_concepts"],
        "n_claims": len(claims), "n_contested": len(contested),
        # the Atlas UI must not infer capsule-source completeness from returned rows or from
        # transport freshness. Keep one small aggregate receipt at the read-model boundary; the embedded
        # context-pack copy remains for agents and backward-compatible consumers.
        "concept_source": {key: overview[key] for key in (
            "source_complete", "partial_capsules", "source_unknown_capsules",
            "source_concepts_omitted", "source_outcomes_omitted",
            "source_store_complete", "source_rows_total", "source_rows_quarantined",
            "source_malformed_rows", "source_invalid_capsule_rows",
            "source_duplicate_run_rows",
        )},
        "research_source": research_source,
        "claim_source": claim_source,
        "explored": explored,                               # what's been tried (concept × runs)
        "explored_total": len(full_concept_rows),
        "explored_omitted": len(full_concept_rows) - len(explored),
        "thin_coverage": thin_coverage,                     # legacy key: observed in one returned run
        "thin_coverage_total": len(thin),
        "thin_coverage_omitted": len(thin) - len(thin_coverage),
        "contradictions": contradictions,
        "contradictions_total": len(contested),
        "contradictions_omitted": len(contested) - len(contradictions),
        "context_pack": build_context_pack(
            claims, concept_overview=pack_overview, max_claims=max_items,
            _concept_rows=full_concept_rows, _research_source=research_source,
            _claim_source=claim_source),
    }
    return sanitize_cross_run_projection(
        payload, max_chars=128_000_000, max_items=128, max_total_items=100_000)


def _safe_text(s, limit: int = 120) -> str:
    """Sanitize UNTRUSTED memory text (claim statements / concept slugs — LLM/repo-derived) before it enters
    an agent prompt: strip control chars + collapse newlines/whitespace to a single space, then bound the
    length. Prevents newline/control-char prompt-injection through the cross-run advisory pack (mega-review)."""
    return _claim_text(s, limit)


def render_context_pack(pack: dict) -> str:
    """Render a context pack as a compact, bounded text block for a proposing agent (the advisory form).
    Deterministic; retains mixed evidence so the agent sees counter-arguments, not only positives.
    All memory-derived text is sanitized (control chars/newlines stripped) — quoted DATA, not instructions
    (mega-review prompt-injection hardening)."""
    if (not pack.get("claims") and not pack.get("coverage")
            and not pack.get("research_source") and not pack.get("claim_source")):
        return ""
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    lines = [f"Cross-run evidence ({pack.get('n_claims_total', 0)} claim records, "
             f"{pack.get('n_contested', 0)} mixed-evidence) — bounded observations, with counter-evidence:"]
    if pack.get("n_pinned_omitted", 0):
        lines.append(
            f"  WARNING: {int(pack['n_pinned_omitted'])} operator-pinned claim(s) omitted by the "
            "hard context limit; consult the full claims ledger.")
    research_source = _safe_research_source_summary(pack.get("research_source"))
    if research_source is not None and research_source["source_complete"] is not True:
        lines.append(
            "  WARNING: D8 research-claim source is PARTIAL/UNKNOWN "
            f"({research_source['producer_partial_runs']} capped run(s); "
            f"{research_source['producer_claims_omitted']} claim(s) known omitted"
            + (f"; {research_source['producer_unknown_runs']} legacy/malformed run receipt(s)"
               if research_source["producer_unknown_runs"] else "")
            + "); retained evidence is a lower bound and exact one-sided states are withheld.")
    claim_source = _safe_claim_source_summary(pack.get("claim_source"))
    if claim_source is None and "claim_source" in pack:
        lines.append(
            "  WARNING: claim evidence source receipt is malformed/unknown; exact one-sided states and "
            "absence are withheld.")
    elif claim_source is not None and claim_source["read_complete"] is not True:
        lessons_bad = claim_source["lessons"]["rows_quarantined"]
        research_bad = claim_source["research"]["rows_quarantined"]
        lines.append(
            "  WARNING: claim evidence stores are PARTIAL "
            f"(lessons quarantined={lessons_bad}; research quarantined={research_bad}); "
            "retained evidence is a lower bound and absence is not exact.")
    for c in pack.get("claims", []):
        statement = _safe_text(c.get("statement"), 120)
        contradicts = "; ".join(
            repr(_safe_text(value, 160))
            for value in (c.get("contradicts") or [])[:3])
        maturity = str(c.get("maturity") or "machine-proposed")
        policy = ""
        if maturity in {"operator-ratified", "operator-pinned"}:
            freshness = {True: "current", False: "stale-evidence", None: "unknown"}.get(
                c.get("decision_fresh"), "unknown")
            # operator policy persists until clear, but its evidence fence can age.
            # Surface both axes so retention priority never masquerades as a fresh ratification.
            policy = (f"; operator_policy={maturity.removeprefix('operator-')}; "
                      f"decision_freshness={freshness}")
        lines.append(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                     f"UNTRUSTED_MEMORY={statement!r}"
                     + policy + (f"; contradicts={contradicts}" if contradicts else ""))
    cov = pack.get("coverage")
    if cov:
        if cov.get("source_complete") is not True:
            lines.append(
                "  WARNING: concept capsule source is PARTIAL "
                f"({int(cov.get('partial_capsules', 0))} capsule(s); "
                f"{int(cov.get('source_concepts_omitted', 0))} concept(s) and "
                f"{int(cov.get('source_outcomes_omitted', 0))} outcome(s) known omitted"
                + (f"; {int(cov.get('source_unknown_capsules', 0))} legacy capsule(s) have unknown totals"
                   if cov.get("source_unknown_capsules", 0) else "")
                + (f"; {int(cov.get('source_rows_quarantined', 0))} durable row(s) were quarantined"
                   if cov.get("source_rows_quarantined", 0) else "")
                + "); "
                "coverage describes returned observations only; directional tendencies are withheld.")
        top = ", ".join(repr(_safe_text(x, 100))
                        for x in cov.get("top_concepts", [])[:6])
        lines.append(f"Bounded live concept observations (not coverage): {cov.get('n_runs', 0)} returned "
                     f"run(s), {cov.get('n_concepts', 0)} concept(s)"
                     f"{'; UNTRUSTED_MEMORY_CONCEPTS=' + top if top else ''}.")
        # Phase 1 profit signal: a direction-normalized RANK tendency across similar runs — which concepts
        # tended to land in the better vs worse half of their run's own field. ADVISORY — a prior rank
        # tendency, never causal proof, never a rule, and never a selection input; weigh but do not obey.
        helps = ", ".join(repr(_safe_text(x, 100)) for x in (cov.get("helps") or [])[:6])
        hurts = ", ".join(repr(_safe_text(x, 100)) for x in (cov.get("hurts") or [])[:6])
        if helps or hurts:
            # concept slugs are persisted, LLM-originated data. Keep the explicit trust
            # marker on rank tendencies just as on the coverage line and the sibling cross-run tool;
            # repr quoting alone does not tell a proposing model that the span is inert memory.
            parts = ([f"tended to RANK BETTER UNTRUSTED_MEMORY={helps}"] if helps else []) + (
                [f"tended to RANK WORSE UNTRUSTED_MEMORY={hurts}"] if hurts else [])
            lines.append("Cross-run concept rank tendency (better/worse half of each run vs its sibling "
                         "concepts; advisory, NOT a rule — consider toward the first, scrutinize the "
                         "second): " + "; ".join(parts) + ".")
    return "\n".join(lines)
