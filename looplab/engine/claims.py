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



# The context pack / retrieval planner / atlas, re-exported so `engine.claims` keeps its
# historical surface (doc 25 EM-01). Imported LAST: this module reads names defined above.
from looplab.engine.claims_retrieval import (  # noqa: F401,E402
    _CAVEAT,
    _CAVEAT_QUERY_COVERAGE,
    _CAVEAT_SCORE_RATIO,
    _CAVEAT_STATES,
    _INTENTS,
    _INTENT_CUES,
    _INTENT_SCORE_BONUS,
    _INTENT_TIE_RANK,
    _RETRIEVAL_CORPUS_VERSION,
    _RETRIEVAL_DOCUMENT_VERSION,
    _claim_claim_source_summary,
    _claim_research_source_summary,
    _classify_intent,
    _eligible,
    _json_digest,
    _lexical_relevance,
    _preselect_retrieval_docs,
    _retrieval_corpus_digest,
    _retrieval_doc,
    _retrieval_tokens,
    _safe_text,
    build_context_pack,
    cross_run_retrieve,
    portfolio_atlas,
    render_context_pack,
)
