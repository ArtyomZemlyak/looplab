"""Bounded HTTP surface for Part IV/V cross-run knowledge and operator governance.

Reads expose live, revision-labelled projections plus an opaque identity for the configured portfolio.
Mutations are explicit, append-only operator actions: every request carries that portfolio identity, a
stable action id and the exact ledger revision observed by the caller. Agents can ask stewards for proposals,
but only the typed operator endpoints below may change portfolio meaning.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from looplab.engine.governance_health import read_curation_rows as _read_curation_rows
from looplab.trust.cross_run import cross_run_text, sanitize_cross_run_projection


_PORTFOLIO_ID_PATTERN = r"^portfolio-sha256:[0-9a-f]{64}$"


def _resolved_portfolio_identity(memory_dir: object) -> tuple[str, str, bool]:
    """Return canonical target, opaque identity, and whether its directory exists.

    The digest deliberately contains no source path. It binds the normalized real path and, when the
    directory exists, its filesystem identity. Switching settings, repointing a symlink, or replacing the
    directory therefore invalidates a delayed mutation even when the new ledgers have equal revisions.
    The existence bit comes from the same stat observation as the digest: mutation callers must not infer
    it with a second path check that can race directory creation.
    """
    raw = os.fspath(memory_dir)
    canonical = os.path.normcase(os.path.realpath(os.path.abspath(raw)))
    target = Path(canonical)
    try:
        status = target.stat()
    except FileNotFoundError:
        directory_identity = b"missing"
        initialized = False
    else:
        if not stat.S_ISDIR(status.st_mode):
            raise NotADirectoryError(canonical)
        directory_identity = (
            f"{int(status.st_dev)}:{int(status.st_ino)}:{int(status.st_mode)}".encode("ascii"))
        initialized = True
    material = b"looplab-cross-run-portfolio-v1\0" + os.fsencode(canonical) + b"\0" + directory_identity
    return canonical, "portfolio-sha256:" + hashlib.sha256(material).hexdigest(), initialized


def _public_cross_run_row(value):
    """One schema-preserving, bounded/redacted cross-run row for an HTTP response."""
    return sanitize_cross_run_projection(
        value, max_chars=640_000, max_items=256, max_total_items=4_096)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _GovernedBody(_StrictBody):
    expected_portfolio_id: str = Field(pattern=_PORTFOLIO_ID_PATTERN)
    expected_revision: StrictInt = Field(ge=0)
    action_id: str = Field(min_length=1, max_length=160)


class _ClaimDecision(_GovernedBody):
    statement: str = Field(min_length=1, max_length=4000)
    claim_uid: str = Field(min_length=1, max_length=80)
    evidence_digest: str = Field(min_length=1, max_length=80)
    scope: str = Field(default="", max_length=500)
    metric: str = Field(default="", max_length=200)
    decision: Literal["ratified", "rejected", "pinned", "clear"]
    note: str = Field(default="", max_length=4000)


class _ConceptSource(_GovernedBody):
    expected_governance_revision: StrictInt = Field(ge=0)
    from_concept: str = Field(min_length=1, max_length=500)


class _ConceptMerge(_ConceptSource):
    to_concept: str = Field(min_length=1, max_length=500)


class _ConceptPurge(_ConceptSource):
    confirm: Literal["purge"]


class _ConceptSplitRule(_StrictBody):
    to: str = Field(min_length=1, max_length=500)
    when_any: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=32)


class _ConceptSplit(_ConceptSource):
    rules: list[_ConceptSplitRule] = Field(min_length=1, max_length=64)
    default: str = Field(default="", max_length=500)


class _CrossRunResponse(BaseModel):
    """Version-tolerant response envelope; nested authority fields have explicit models below."""

    model_config = ConfigDict(extra="allow")
    portfolio_id: str = Field(pattern=_PORTFOLIO_ID_PATTERN)


class _CrossRunProjection(BaseModel):
    """Version-tolerant projection with executable types for every authority-bearing field."""

    model_config = ConfigDict(extra="allow")


class CrossRunSourceSegment(_CrossRunProjection):
    read_complete: bool
    rows_total: int = Field(ge=0)
    rows_retained: int = Field(ge=0)
    rows_quarantined: int = Field(ge=0)
    malformed_rows: int = Field(ge=0)
    invalid_rows: int = Field(ge=0)


class CrossRunResearchSource(_CrossRunProjection):
    source_complete: bool
    producer_receipt_known: bool
    producer_complete: bool
    producer_runs: int = Field(ge=0)
    producer_partial_runs: int = Field(ge=0)
    producer_unknown_runs: int = Field(ge=0)
    producer_claims_total: int = Field(ge=0)
    producer_claims_retained: int = Field(ge=0)
    producer_claims_omitted: int = Field(ge=0)
    read_health_v: int = Field(ge=0)
    read_complete: bool
    rows_total: int = Field(ge=0)
    rows_retained: int = Field(ge=0)
    rows_quarantined: int = Field(ge=0)
    malformed_rows: int = Field(ge=0)
    invalid_rows: int = Field(ge=0)
    snapshot_digest: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")


class CrossRunClaimSource(_CrossRunProjection):
    v: int = Field(ge=1)
    receipt_known: bool
    source_complete: bool
    read_complete: bool
    research_source_complete: bool
    lessons: CrossRunSourceSegment
    research: CrossRunSourceSegment
    snapshot_digest: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")


class CrossRunConceptSource(_CrossRunProjection):
    source_complete: bool
    partial_capsules: int = Field(ge=0)
    source_unknown_capsules: int = Field(ge=0)
    source_concepts_omitted: int = Field(ge=0)
    source_outcomes_omitted: int = Field(ge=0)
    source_store_complete: bool
    source_rows_total: int = Field(ge=0)
    source_rows_quarantined: int = Field(ge=0)
    source_malformed_rows: int = Field(ge=0)
    source_invalid_capsule_rows: int = Field(ge=0)
    source_duplicate_run_rows: int = Field(ge=0)


class CrossRunConceptRun(_CrossRunProjection):
    run_id: str
    metric: int | float | None = None
    direction: Literal["min", "max"]
    sign: Literal[-1, 0, 1] | None = None


class CrossRunExploredConcept(_CrossRunProjection):
    concept: str
    n_runs: int = Field(ge=0)
    n_helped: int = Field(ge=0)
    n_neutral: int = Field(ge=0)
    n_hurt: int = Field(ge=0)
    runs: list[CrossRunConceptRun]
    runs_omitted: int = Field(default=0, ge=0)


class CrossRunClaimDecision(_CrossRunProjection):
    statement: str
    claim_uid: str = ""
    scope: str = ""
    metric: str = ""
    decision: Literal["ratified", "rejected", "pinned", "clear"]
    note: str = ""
    by: str = ""
    at: str = ""
    action_id: str = ""
    evidence_digest: str = ""
    revision: int = Field(ge=0)


class CrossRunClaim(_CrossRunProjection):
    statement: str
    epistemic: Literal["supported", "refuted", "mixed", "inconclusive"]
    maturity: Literal[
        "machine-proposed", "operator-ratified", "operator-rejected", "operator-pinned"
    ]
    support: list[str]
    oppose: list[str]
    n_support: int = Field(ge=0)
    n_oppose: int = Field(ge=0)
    unverified: list[str]
    n_unverified: int = Field(ge=0)
    runs: list[str]
    scopes: list[str]
    sources: list[str]
    verification: list[str]
    claim_uid: str
    scope: str
    polarity: Literal[-1, 1]
    metric: str
    decision: CrossRunClaimDecision | None
    contradicts: list[str]
    research_source: CrossRunResearchSource
    claim_source: CrossRunClaimSource
    evidence_digest: str
    decision_fresh: bool | None
    n_contradicts: int = Field(ge=0)
    nested_omitted: dict[str, int] = Field(default_factory=dict)


class CrossRunContextClaim(_CrossRunProjection):
    statement: str
    epistemic: Literal["supported", "refuted", "mixed", "inconclusive"]
    maturity: Literal[
        "machine-proposed", "operator-ratified", "operator-rejected", "operator-pinned"
    ]
    claim_uid: str
    scope: str
    evidence_digest: str
    decision_fresh: bool | None
    metric: str
    polarity: Literal[-1, 1]
    n_support: int = Field(ge=0)
    n_oppose: int = Field(ge=0)
    n_unverified: int = Field(ge=0)
    support: list[str]
    oppose: list[str]
    unverified: list[str]
    contradicts: list[str]
    runs: list[str]
    scopes: list[str]


class CrossRunCoverage(_CrossRunProjection):
    n_runs: int = Field(ge=0)
    n_concepts: int = Field(ge=0)
    source_complete: bool
    partial_capsules: int = Field(ge=0)
    source_unknown_capsules: int = Field(ge=0)
    source_concepts_omitted: int = Field(ge=0)
    source_outcomes_omitted: int = Field(ge=0)
    source_store_complete: bool
    source_rows_total: int = Field(ge=0)
    source_rows_quarantined: int = Field(ge=0)
    source_malformed_rows: int = Field(ge=0)
    source_invalid_capsule_rows: int = Field(ge=0)
    source_duplicate_run_rows: int = Field(ge=0)
    top_concepts: list[str]
    helps: list[str]
    hurts: list[str]


class CrossRunContextPack(_CrossRunProjection):
    claims: list[CrossRunContextClaim]
    n_claims_total: int = Field(ge=0)
    n_contested: int = Field(ge=0)
    n_pinned_total: int = Field(ge=0)
    n_pinned_omitted: int = Field(ge=0)
    research_source: CrossRunResearchSource
    claim_source: CrossRunClaimSource
    coverage: CrossRunCoverage


class CrossRunRevisions(_CrossRunProjection):
    claims: int = Field(ge=0)
    concept_aliases: int = Field(ge=0)
    concept_splits: int = Field(ge=0)
    concept_governance: int = Field(ge=0)


class CrossRunGovernance(_CrossRunProjection):
    status: Literal["complete"]
    complete: Literal[True]
    revisions: CrossRunRevisions


class CrossRunAtlasResponse(_CrossRunResponse):
    n_runs: int
    n_concepts: int
    n_claims: int
    n_contested: int
    # these receipts are semantic UI authority, not opaque payloads. Their executable
    # response models keep generated clients aligned with the complete/partial/unknown contract while
    # allowing additive versioned fields.
    concept_source: CrossRunConceptSource
    research_source: CrossRunResearchSource
    claim_source: CrossRunClaimSource
    explored: list[CrossRunExploredConcept]
    explored_total: int
    explored_omitted: int
    thin_coverage: list[str]
    thin_coverage_total: int
    thin_coverage_omitted: int
    contradictions: list[CrossRunClaim]
    contradictions_total: int
    contradictions_omitted: int
    context_pack: CrossRunContextPack
    governance: CrossRunGovernance
    projection: Literal["live"]
    scope_task: str
    page: dict[str, int]
    revisions: CrossRunRevisions


class CrossRunConceptPolicyResponse(_CrossRunResponse):
    # `canonical` is `id -> canonical | null`, null meaning PURGED. Typed as an open str->str|None
    # map rather than a row list: the client's operation is one lookup with an identity default, and
    # a list would invite it to iterate and re-derive rather than index.
    canonical: dict[str, str | None]
    canonical_omitted: int = Field(ge=0)
    split_sources: list[str]
    split_sources_omitted: int = Field(ge=0)
    capsule_run_ids: list[str]
    capsule_run_ids_omitted: int = Field(ge=0)
    revisions: CrossRunRevisions


class CrossRunClaimsResponse(_CrossRunResponse):
    claims: list[CrossRunClaim]
    n: int
    returned: int
    offset: int
    limit: int
    scope_task: str
    research_source: CrossRunResearchSource
    claim_source: CrossRunClaimSource
    revision: int


class ClaimDecisionResponse(_CrossRunResponse):
    ok: bool
    decision: CrossRunClaimDecision
    revision: int


class ConceptAliasResponse(_CrossRunResponse):
    ok: bool
    alias: dict[str, Any]
    revision: int
    governance_revision: int


class ConceptSplitResponse(_CrossRunResponse):
    ok: bool
    split: dict[str, Any]
    revision: int
    governance_revision: int


class CurationLogResponse(_CrossRunResponse):
    v: Literal[1]
    status: Literal["complete"]
    complete: Literal[True]
    entries: list[dict[str, Any]]
    n: int
    limit: int


class StewardProposalResponse(_CrossRunResponse):
    ok: bool
    proposals: dict[str, Any]
    receipt: dict[str, Any] | None
    invocation: dict[str, Any]


def build_router(srv) -> APIRouter:
    router = APIRouter()
    def _portfolio(expected_portfolio_id: str | None = None) -> tuple[str, str]:
        """Resolve one request's portfolio exactly once and enforce its mutation precondition."""
        memory_dir = getattr(srv.global_settings(), "memory_dir", None)
        if not memory_dir:
            raise HTTPException(400, "no memory_dir configured")
        try:
            target, portfolio_id, initialized = _resolved_portfolio_identity(memory_dir)
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeError) as exc:
            raise HTTPException(503, detail={
                "code": "cross_run_portfolio_unavailable",
                "message": "the configured cross-run portfolio identity cannot be resolved",
            }, headers={"Cache-Control": "no-store"}) from exc
        if (expected_portfolio_id is not None
                and not hmac.compare_digest(expected_portfolio_id, portfolio_id)):
            # Do not echo the caller-controlled value. The current opaque id is safe remediation: refresh
            # the projection and deliberately form a new action against the replacement portfolio.
            raise HTTPException(409, detail={
                "code": "portfolio_identity_conflict",
                "current_portfolio_id": portfolio_id,
            }, headers={"Cache-Control": "no-store"})
        if expected_portfolio_id is not None and not initialized:
            # A read may honestly project an empty, not-yet-created target without side effects. A write
            # cannot: its first mkdir would replace the observed `missing` identity before the durable
            # action/provider receipt exists, making a lost-response retry conflict with its own work.
            raise HTTPException(409, detail={
                "code": "portfolio_not_initialized",
                "current_portfolio_id": portfolio_id,
                "message": "the observed cross-run portfolio directory does not exist yet",
                "remediation": (
                    "initialize the configured memory directory, refresh the portfolio, and form a new "
                    "action with its replacement identity"),
            }, headers={"Cache-Control": "no-store"})
        return target, portfolio_id

    def _assert_portfolio_current(memory_dir: str, expected_portfolio_id: str) -> None:
        """Reject a projection assembled while the configured directory was replaced."""
        try:
            _target, current, _initialized = _resolved_portfolio_identity(memory_dir)
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeError) as exc:
            raise HTTPException(503, detail={
                "code": "cross_run_portfolio_unavailable",
                "message": "the cross-run portfolio changed while it was being read",
            }, headers={"Cache-Control": "no-store"}) from exc
        if not hmac.compare_digest(current, expected_portfolio_id):
            raise HTTPException(409, detail={
                "code": "portfolio_identity_changed",
                "current_portfolio_id": current,
                "message": "the cross-run portfolio was replaced while this projection was read",
            }, headers={"Cache-Control": "no-store"})

    def _actor() -> str:
        return "deployment-owner" if getattr(srv, "owner_auth_enabled", False) else "local-operator"

    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _raise_governance_error(exc: Exception) -> None:
        from looplab.engine.claims import (
            ClaimDecisionConflict, ClaimDecisionIdempotencyConflict, ClaimTargetConflict,
        )
        from looplab.engine.concept_registry import (
            ConceptGovernanceConflict,
            ConceptGovernanceGlobalConflict,
            ConceptGovernanceIdempotencyConflict,
        )
        from looplab.engine.governance_health import GovernanceLedgerUnavailable
        from looplab.engine.steward_invocation import StewardInvocationIdempotencyConflict
        from looplab.events.eventstore import EventStoreLockError

        if isinstance(exc, GovernanceLedgerUnavailable):
            # never reflect poisoned JSON, parser text, or a filesystem path. The
            # closed health receipt is sufficient for an operator to identify the ledger to repair.
            raise HTTPException(
                503, detail=exc.public_receipt(), headers={"Cache-Control": "no-store"}) from exc
        if isinstance(exc, ClaimDecisionConflict):
            raise HTTPException(409, detail={
                "code": "claim_revision_conflict",
                "expected_revision": exc.expected_revision,
                "current_revision": exc.current_revision,
            }) from exc
        if isinstance(exc, ClaimTargetConflict):
            raise HTTPException(409, detail={
                "code": exc.code, **exc.detail,
            }) from exc
        if isinstance(exc, ConceptGovernanceConflict):
            raise HTTPException(409, detail={
                "code": "concept_revision_conflict",
                "ledger": exc.path.name,
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }) from exc
        if isinstance(exc, ConceptGovernanceGlobalConflict):
            raise HTTPException(409, detail={
                "code": "concept_governance_revision_conflict",
                "expected_governance_revision": exc.expected,
                "current_governance_revision": exc.actual,
            }) from exc
        if isinstance(exc, (ClaimDecisionIdempotencyConflict,
                            ConceptGovernanceIdempotencyConflict,
                            StewardInvocationIdempotencyConflict)):
            raise HTTPException(409, detail={
                "code": "action_id_reused",
                "message": cross_run_text(
                    exc, max_chars=500, single_line=True, entropy=True),
            }) from exc
        if isinstance(exc, EventStoreLockError):
            raise HTTPException(503, detail={
                "code": "governance_lock_unavailable",
                "message": "governance storage cannot guarantee an exclusive write",
            }, headers={"Cache-Control": "no-store"}) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(422, cross_run_text(
                exc, max_chars=500, single_line=True, entropy=True)) from exc
        raise exc

    def _raise_evidence_read_error(exc: Exception) -> None:
        """Map a failed required snapshot lock to stable read semantics, never a truthful empty view."""
        from looplab.events.eventstore import EventStoreLockError

        if isinstance(exc, (EventStoreLockError, OSError)):
            raise HTTPException(503, detail={
                "code": "cross_run_evidence_unavailable",
                "message": "cross-run evidence cannot be read as one coherent snapshot",
            }, headers={"Cache-Control": "no-store"}) from exc
        raise exc

    def _read_governance(project):
        """Map strict governance-health failures on read surfaces to one stable 503 contract."""
        from looplab.engine.governance_health import GovernanceLedgerUnavailable
        from looplab.events.eventstore import EventStoreLockError

        try:
            return project()
        except (GovernanceLedgerUnavailable, EventStoreLockError) as exc:
            _raise_governance_error(exc)

    def _capsule_run_ids(memory_dir) -> tuple[list[str], int]:
        """The run ids durable cross-run memory actually holds, bounded and reported.

        Deliberately the RAW capsule population, with no filtering: the point is to expose the gap
        between what the map draws and what memory knows, so any narrowing here would hide exactly
        the fact being disclosed. A quarantined store still reports the rows it retained — an
        incomplete answer that says how many it dropped beats refusing to answer, because this field
        is advisory disclosure and never an authority for a governance write (that fence is
        `_observed_concept_snapshot`, which does fail closed on an incomplete store).
        """
        from looplab.engine.concept_capsules import ConceptCapsuleStore

        path = Path(memory_dir) / "concept_capsules.jsonl"
        if not path.exists():
            return [], 0
        store = ConceptCapsuleStore(path)
        ids = sorted({str(capsule.get("run_id") or "") for capsule in store.all()} - {""})
        return ids[:2_000], max(0, len(ids) - 2_000)

    def _read_governed_evidence(project):
        """Keep policy corruption and unavailable evidence snapshots as distinct safe contracts."""
        from looplab.engine.governance_health import GovernanceLedgerUnavailable
        from looplab.events.eventstore import EventStoreLockError

        try:
            return project()
        except GovernanceLedgerUnavailable as exc:
            _raise_governance_error(exc)
        except (EventStoreLockError, OSError) as exc:
            _raise_evidence_read_error(exc)

    @router.get("/api/cross-run/atlas", response_model=CrossRunAtlasResponse)
    def atlas(limit: int = Query(24, ge=1, le=100),
              scope_task: str = Query("", max_length=500)):
        """Live structured Research Atlas projection, bounded per section."""
        from looplab.engine.claims import atlas_for_memory
        from looplab.engine.governance_health import project_governed_sources

        memory_dir, portfolio_id = _portfolio()
        def _project(governance):
            payload = atlas_for_memory(memory_dir, scope_task=scope_task, max_items=limit,
                                       structured=True, _governance=governance)
            payload.update({
                "projection": "live",
                "scope_task": cross_run_text(
                    scope_task, max_chars=500, single_line=True, entropy=False),
                "page": {"limit": limit},
                "revisions": payload["governance"]["revisions"],
                "portfolio_id": portfolio_id,
            })
            return payload

        payload = _read_governed_evidence(lambda: project_governed_sources(
            memory_dir, _project, include_concepts=True,
            source_names=(
                "concept_capsules.jsonl", "lessons.jsonl", "research_claims.jsonl"),
        ))
        _assert_portfolio_current(memory_dir, portfolio_id)
        # A BACKSTOP, not the primary bound: `atlas_for_memory(max_items=limit)` already caps each
        # section at `limit` (<=100), and every text field it emits is bounded by its own projection.
        # This is the second layer that keeps a malformed legacy row from expanding without bound —
        # so it has to be a real ceiling. It was 128,000,000 total chars / 500,000 items, three
        # orders of magnitude above the paged rows' 640,000 / 4,096 and enough to serialize hundreds
        # of MB to a browser, which is not a bound at all. Sized instead with roughly 2x headroom
        # over the worst reachable payload (~100 sectioned items x their 4,000-char field cap).
        return sanitize_cross_run_projection(
            payload, max_chars=8_000_000, max_items=256,
            max_total_items=65_536)

    @router.get("/api/cross-run/claims", response_model=CrossRunClaimsResponse)
    def claims(contested: bool = False,
               scope_task: str = Query("", max_length=500),
               limit: int = Query(80, ge=1, le=200),
               offset: int = Query(0, ge=0, le=1_000_000)):
        """Scope/polarity-safe claims with stable IDs and bounded offset pagination."""
        from looplab.engine.claims import (
            _filter_claim_assessments, _safe_claim_source_summary,
            _safe_research_source_summary, claims_for_memory,
        )
        from looplab.engine.governance_health import project_governed_sources

        memory_dir, portfolio_id = _portfolio()
        def _project(governance):
            rows = claims_for_memory(
                memory_dir, scope_task=scope_task,
                structured=True, decisions=governance["decisions"])
            research_source = _safe_research_source_summary(
                getattr(rows, "research_source", None)) or {}
            claim_source = _safe_claim_source_summary(
                getattr(rows, "claim_source", None)) or {}
            return rows, research_source, claim_source, governance["claim_revision"]

        rows, research_source, claim_source, revision = _read_governed_evidence(
            lambda: project_governed_sources(
                memory_dir, _project,
                source_names=("lessons.jsonl", "research_claims.jsonl"),
            ))
        if contested:
            rows = _filter_claim_assessments(
                rows, lambda row: row.get("epistemic") == "mixed")
        total = len(rows)
        page = [_public_cross_run_row(row) for row in rows[offset:offset + limit]]
        _assert_portfolio_current(memory_dir, portfolio_id)
        return {
            "claims": page,
            "n": total,
            "returned": len(page),
            "offset": offset,
            "limit": limit,
            "scope_task": cross_run_text(
                scope_task, max_chars=500, single_line=True, entropy=False),
            "research_source": research_source,
            "claim_source": claim_source,
            "revision": revision,
            "portfolio_id": portfolio_id,
        }

    @router.get("/api/cross-run/concept-policy", response_model=CrossRunConceptPolicyResponse)
    def concept_policy():
        """The alias/split registry as a lookup table a browser can APPLY, plus who is in memory.

        The GLOBAL concept map (`search/concept_lens.py::project_concept_map` and its browser half
        `ui/src/conceptForest.js`) takes per-run concept SETS and no governance, so the CALLER
        canonicalizes. Every server-side caller has `canonicalize_concepts`; the browser had nothing,
        which is why a merged pair still drew two nodes and still read as spelling drift. This route
        is what the browser applies, and `concept_registry.py::concept_canonicalization_policy`
        documents the two-field contract (`canonical` with chains resolved, `split_sources` declared
        UNAPPLIED because a split's answer depends on each run's own siblings).

        `capsule_run_ids` is a POPULATION disclosure, not decoration. Two surfaces of one lab count
        different runs: the map folds the live per-run rollup of every run the list is showing (46
        runs, 15 tagged on the development corpus), while durable cross-run memory — what agent
        priors, novelty grading, the atlas and the taxonomy steward all read — holds capsules for 3.
        Nothing said so anywhere. Returning the ids lets the map render the intersection with its own
        scope instead of implying that governance covers everything it draws.

        Read-only and revision-bearing: the whole table comes from ONE governance-locked snapshot, so
        a merge landing mid-render is wholly visible or wholly invisible, never half.
        """
        from looplab.engine.concept_registry import concept_canonicalization_policy

        memory_dir, portfolio_id = _portfolio()

        def _project():
            policy = concept_canonicalization_policy(memory_dir)
            run_ids, omitted = _capsule_run_ids(memory_dir)
            return {
                **policy,
                "capsule_run_ids": run_ids,
                "capsule_run_ids_omitted": omitted,
                "revisions": {
                    "claims": 0,
                    "concept_aliases": policy["alias_revision"],
                    "concept_splits": policy["split_revision"],
                    "concept_governance": policy["governance_revision"],
                },
                "portfolio_id": portfolio_id,
            }

        payload = _read_governed_evidence(_project)
        _assert_portfolio_current(memory_dir, portfolio_id)
        return sanitize_cross_run_projection(
            payload, max_chars=1_000_000, max_items=4_096, max_total_items=16_384)

    @router.post("/api/cross-run/claim-decide", response_model=ClaimDecisionResponse)
    def claim_decide(body: _ClaimDecision):
        """Ratify/reject/pin/clear exactly the claim ID and revision the operator observed."""
        from looplab.engine.claims import record_observed_claim_decision
        memory_dir, portfolio_id = _portfolio(body.expected_portfolio_id)
        try:
            rec = record_observed_claim_decision(
                memory_dir, statement=body.statement, claim_uid=body.claim_uid,
                scope=body.scope, metric=body.metric, evidence_digest=body.evidence_digest,
                decision=body.decision, note=body.note, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision, action_id=body.action_id,
            )
        except Exception as exc:  # noqa: BLE001 — converted to stable HTTP semantics below
            _raise_governance_error(exc)
        return {"ok": True, "decision": _public_cross_run_row(rec),
                "portfolio_id": portfolio_id,
                "revision": rec["revision"]}

    def _governed_mutation(body, record, result_key: str) -> dict:
        """Run one governance mutation and shape its response — the five ways to say the same thing.

        merge / purge / alias-clear / split / split-clear differ ONLY in which registry function
        runs; everything around it — resolving the portfolio, funnelling every failure through
        `_raise_governance_error`, and the five-key envelope carrying both revisions back — was
        written out per endpoint. The envelope is what a client CASes on next, so a copy that drifted
        (a missing `governance_revision`, a `revision` read off the wrong record) would break the
        next fenced write rather than this one, at a call site with no visible connection to the bug.

        `record` takes the resolved memory dir and returns the persisted row; `result_key` is the
        one field name that legitimately differs (`alias` vs `split`).
        """
        memory_dir, portfolio_id = _portfolio(body.expected_portfolio_id)
        try:
            rec = record(memory_dir)
        except Exception as exc:  # noqa: BLE001 — converted to stable HTTP semantics by _raise_governance_error
            _raise_governance_error(exc)
        return {
            "ok": True, result_key: _public_cross_run_row(rec), "revision": rec["revision"],
            "governance_revision": rec["governance_revision"], "portfolio_id": portfolio_id,
        }

    @router.post("/api/cross-run/concept-merge", response_model=ConceptAliasResponse)
    def concept_merge(body: _ConceptMerge):
        """Merge one non-empty concept into another; purge is a separate confirmed action."""
        from looplab.engine.concept_registry import record_concept_alias
        return _governed_mutation(body, lambda memory_dir: record_concept_alias(
            memory_dir, from_concept=body.from_concept, to_concept=body.to_concept,
            by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
            expected_governance_revision=body.expected_governance_revision,
            action_id=body.action_id, require_existing=True,
        ), "alias")

    @router.post("/api/cross-run/concept-purge", response_model=ConceptAliasResponse)
    def concept_purge(body: _ConceptPurge):
        """Explicitly tombstone one concept after a typed confirmation."""
        from looplab.engine.concept_registry import record_concept_alias
        # An EMPTY `to_concept` is what makes this a tombstone rather than a redirect.
        return _governed_mutation(body, lambda memory_dir: record_concept_alias(
            memory_dir, from_concept=body.from_concept, to_concept="",
            by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
            expected_governance_revision=body.expected_governance_revision,
            action_id=body.action_id, require_existing=True,
        ), "alias")

    @router.post("/api/cross-run/concept-alias-clear", response_model=ConceptAliasResponse)
    def concept_alias_clear(body: _ConceptSource):
        """Undo the current alias/purge policy without deleting its audit history."""
        from looplab.engine.concept_registry import clear_concept_alias
        return _governed_mutation(body, lambda memory_dir: clear_concept_alias(
            memory_dir, from_concept=body.from_concept, by=_actor(), at=_timestamp(),
            expected_revision=body.expected_revision,
            expected_governance_revision=body.expected_governance_revision,
            action_id=body.action_id, require_existing=True,
        ), "alias")

    @router.post("/api/cross-run/concept-split", response_model=ConceptSplitResponse)
    def concept_split(body: _ConceptSplit):
        """Record one bounded deterministic split rule set."""
        from looplab.engine.concept_registry import record_concept_split
        return _governed_mutation(body, lambda memory_dir: record_concept_split(
            memory_dir, from_concept=body.from_concept,
            rules=[rule.model_dump() for rule in body.rules], default=body.default,
            by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
            expected_governance_revision=body.expected_governance_revision,
            action_id=body.action_id, require_existing=True,
        ), "split")

    @router.post("/api/cross-run/concept-split-clear", response_model=ConceptSplitResponse)
    def concept_split_clear(body: _ConceptSource):
        """Undo the active split while preserving the append-only history."""
        from looplab.engine.concept_registry import clear_concept_split
        return _governed_mutation(body, lambda memory_dir: clear_concept_split(
            memory_dir, from_concept=body.from_concept, by=_actor(), at=_timestamp(),
            expected_revision=body.expected_revision,
            expected_governance_revision=body.expected_governance_revision,
            action_id=body.action_id, require_existing=True,
        ), "split")

    def _recent_log(name: str, limit: int, memory_dir: str, portfolio_id: str) -> dict:
        path = Path(memory_dir) / name
        latest: deque[dict] = deque(maxlen=limit)
        count = 0
        for row in _read_curation_rows(path):
            latest.append(_public_cross_run_row(row))
            count += 1
        return {
            "v": 1, "status": "complete", "complete": True,
            "entries": list(reversed(latest)), "n": count, "limit": limit,
            "portfolio_id": portfolio_id,
        }

    @router.get("/api/cross-run/curation-log", response_model=CurationLogResponse)
    def curation_log(limit: int = Query(20, ge=1, le=200)):
        memory_dir, portfolio_id = _portfolio()
        payload = _read_governance(lambda: _recent_log(
            "concept_curation_log.jsonl", limit, memory_dir, portfolio_id))
        _assert_portfolio_current(memory_dir, portfolio_id)
        return payload

    @router.get("/api/cross-run/claim-curation-log", response_model=CurationLogResponse)
    def claim_curation_log(limit: int = Query(20, ge=1, le=200)):
        memory_dir, portfolio_id = _portfolio()
        payload = _read_governance(lambda: _recent_log(
            "claim_curation_log.jsonl", limit, memory_dir, portfolio_id))
        _assert_portfolio_current(memory_dir, portfolio_id)
        return payload

    def _steward_client():
        try:
            engine = getattr(srv, "engine", None) or getattr(srv, "_engine", None)
            client = engine._reflect_client() if engine is not None else None
            if client is not None:
                return client
        except Exception:  # noqa: BLE001
            pass
        from looplab.core.llm import make_llm_client_for
        settings = srv.llm_settings()
        return make_llm_client_for(settings, factory=srv.make_llm_client)

    def _steward_response(record: dict, portfolio_id: str) -> dict:
        invocation = _public_cross_run_row({key: record.get(key) for key in
                                            ("action_id", "revision", "outcome", "by", "at")})
        if not invocation.get("action_id"):
            invocation["action_id"] = cross_run_text(
                record.get("invocation_id"), max_chars=160,
                single_line=True, entropy=False)
        if record.get("action") == "steward-invocation-begun":
            # replaying an ambiguous external call can charge twice. Same-key retries
            # therefore surface the durable begin claim and require an explicit new paid identity.
            raise HTTPException(409, detail={
                "code": "steward_invocation_outcome_unknown",
                "message": (
                    "the steward invocation may have run, but no durable outcome receipt exists; "
                    "the same action_id will not invoke it again. Submit a new action_id only after "
                    "reviewing this ambiguous attempt"
                ),
                "invocation": invocation,
            })
        if record.get("outcome") == "error":
            raise HTTPException(400, detail={
                "code": "steward_failed",
                "message": cross_run_text(
                    record.get("error") or "steward failed", max_chars=500,
                    single_line=True, entropy=True),
                "invocation": invocation,
            })
        return {"ok": True, "portfolio_id": portfolio_id,
                "proposals": sanitize_cross_run_projection(
                    record.get("proposals") or {}, max_chars=64_000,
                    max_items=128, max_total_items=2_048),
                "receipt": sanitize_cross_run_projection(
                    record.get("receipt"), max_chars=32_000,
                    max_items=128, max_total_items=1_024),
                "invocation": invocation}

    def _safe_steward_error(exc: Exception, *, phase: str) -> str:
        # Provider failures can embed endpoints, account ids, or credential fragments. Persist only the
        # assistant surface's allow-listed failure class, never the raw exception text.
        from looplab.serve.assistant import safe_assistant_failure
        return f"{phase}:{safe_assistant_failure(exc)['error_kind']}"

    def _run_steward(kind: str, action_id: str, expected_portfolio_id: str, apply: bool,
                     *, probe, steward) -> dict:
        """Drive one proposal-only steward invocation.

        The concept and claim stewards differ in exactly two things — which revision they probe for
        health and which steward function runs — and were otherwise identical, down to the paid-call
        ordering that is the whole point of the sequence:

        * `apply` is refused BEFORE anything else, because these endpoints only ever propose;
        * the health probe runs BEFORE client creation and before the durable paid-call claim, so an
          unhealthy projection cannot be paid to review — a steward given a guessed taxonomy returns
          confident proposals about a portfolio that does not exist;
        * every failure funnels through `_raise_governance_error`, and errors are recorded through
          `_safe_steward_error`, so a provider exception cannot carry an endpoint or credential
          fragment into a durable receipt.
        """
        from looplab.engine.steward_invocation import run_steward_invocation

        if apply:
            raise HTTPException(422, "steward endpoints are proposal-only; apply typed operator actions")
        memory_dir, portfolio_id = _portfolio(expected_portfolio_id)
        _read_governance(lambda: probe(memory_dir))
        try:
            record, _replayed = run_steward_invocation(
                memory_dir, kind, action_id, actor=_actor(), at=_timestamp(),
                prepare=_steward_client,
                invoke=lambda client: steward(
                    memory_dir, client, apply=False, by=_actor(), raise_on_failure=True),
                safe_error=lambda exc, phase: _safe_steward_error(exc, phase=phase),
                request={"surface": "owner-http"},
            )
        except Exception as exc:  # noqa: BLE001 — converted to stable HTTP semantics by _raise_governance_error
            _raise_governance_error(exc)
        return _steward_response(record, portfolio_id)

    @router.post("/api/cross-run/concept-steward", response_model=StewardProposalResponse)
    def concept_steward(action_id: str = Query(..., min_length=1, max_length=160),
                        expected_portfolio_id: str = Query(
                            ..., pattern=_PORTFOLIO_ID_PATTERN),
                        apply: bool = False):
        """Run a proposal-only taxonomy review; typed operator actions apply selected proposals."""
        from looplab.engine.concept_registry import concept_governance_snapshot
        from looplab.engine.concept_steward import steward_concepts
        return _run_steward("concept", action_id, expected_portfolio_id, apply,
                            probe=concept_governance_snapshot, steward=steward_concepts)

    @router.post("/api/cross-run/claim-steward", response_model=StewardProposalResponse)
    def claim_steward(action_id: str = Query(..., min_length=1, max_length=160),
                      expected_portfolio_id: str = Query(
                          ..., pattern=_PORTFOLIO_ID_PATTERN),
                      apply: bool = False):
        """Run a proposal-only claim review; typed operator actions apply selected proposals."""
        from looplab.engine.claims import claim_governance_revision
        from looplab.engine.claim_steward import steward_claims
        return _run_steward("claim", action_id, expected_portfolio_id, apply,
                            probe=claim_governance_revision, steward=steward_claims)

    return router
