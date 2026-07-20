"""Bounded HTTP surface for Part IV/V cross-run knowledge and operator governance.

Reads expose live, revision-labelled projections. Mutations are explicit, append-only operator actions:
every request carries a stable action id and the exact ledger revision observed by the caller. Agents can
ask stewards for proposals, but only the typed operator endpoints below may change portfolio meaning.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from looplab.engine.governance_health import read_curation_rows as _read_curation_rows
from looplab.trust.cross_run import cross_run_text, sanitize_cross_run_projection


def _public_cross_run_row(value):
    """One schema-preserving, bounded/redacted cross-run row for an HTTP response."""
    return sanitize_cross_run_projection(
        value, max_chars=640_000, max_items=256, max_total_items=4_096)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _GovernedBody(_StrictBody):
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


def build_router(srv) -> APIRouter:
    router = APIRouter()
    def _memory_dir() -> str:
        memory_dir = getattr(srv.global_settings(), "memory_dir", None)
        if not memory_dir:
            raise HTTPException(400, "no memory_dir configured")
        return str(memory_dir)

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
            # CODEX AGENT: never reflect poisoned JSON, parser text, or a filesystem path. The
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

    @router.get("/api/cross-run/atlas")
    def atlas(limit: int = Query(24, ge=1, le=100),
              scope_task: str = Query("", max_length=500)):
        """Live structured Research Atlas projection, bounded per section."""
        from looplab.engine.claims import atlas_for_memory
        from looplab.engine.governance_health import project_governed_sources

        memory_dir = _memory_dir()
        def _project(governance):
            payload = atlas_for_memory(memory_dir, scope_task=scope_task, max_items=limit,
                                       structured=True, _governance=governance)
            payload.update({
                "projection": "live",
                "scope_task": cross_run_text(
                    scope_task, max_chars=500, single_line=True, entropy=False),
                "page": {"limit": limit},
                "revisions": payload["governance"]["revisions"],
            })
            return payload

        payload = _read_governed_evidence(lambda: project_governed_sources(
            memory_dir, _project, include_concepts=True,
            source_names=(
                "concept_capsules.jsonl", "lessons.jsonl", "research_claims.jsonl"),
        ))
        return sanitize_cross_run_projection(
            payload, max_chars=128_000_000, max_items=256,
            max_total_items=500_000)

    @router.get("/api/cross-run/claims")
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

        memory_dir = _memory_dir()
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
        }

    @router.post("/api/cross-run/claim-decide")
    def claim_decide(body: _ClaimDecision):
        """Ratify/reject/pin/clear exactly the claim ID and revision the operator observed."""
        from looplab.engine.claims import record_observed_claim_decision
        memory_dir = _memory_dir()
        try:
            rec = record_observed_claim_decision(
                memory_dir, statement=body.statement, claim_uid=body.claim_uid,
                scope=body.scope, metric=body.metric, evidence_digest=body.evidence_digest,
                decision=body.decision, note=body.note, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision, action_id=body.action_id,
            )
        except Exception as exc:  # converted to stable HTTP semantics below
            _raise_governance_error(exc)
        return {"ok": True, "decision": _public_cross_run_row(rec),
                "revision": rec["revision"]}

    @router.post("/api/cross-run/concept-merge")
    def concept_merge(body: _ConceptMerge):
        """Merge one non-empty concept into another; purge is a separate confirmed action."""
        from looplab.engine.concept_registry import record_concept_alias

        try:
            rec = record_concept_alias(
                _memory_dir(), from_concept=body.from_concept, to_concept=body.to_concept,
                by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
                expected_governance_revision=body.expected_governance_revision,
                action_id=body.action_id, require_existing=True,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {
            "ok": True, "alias": _public_cross_run_row(rec), "revision": rec["revision"],
            "governance_revision": rec["governance_revision"],
        }

    @router.post("/api/cross-run/concept-purge")
    def concept_purge(body: _ConceptPurge):
        """Explicitly tombstone one concept after a typed confirmation."""
        from looplab.engine.concept_registry import record_concept_alias

        try:
            rec = record_concept_alias(
                _memory_dir(), from_concept=body.from_concept, to_concept="",
                by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
                expected_governance_revision=body.expected_governance_revision,
                action_id=body.action_id, require_existing=True,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {
            "ok": True, "alias": _public_cross_run_row(rec), "revision": rec["revision"],
            "governance_revision": rec["governance_revision"],
        }

    @router.post("/api/cross-run/concept-alias-clear")
    def concept_alias_clear(body: _ConceptSource):
        """Undo the current alias/purge policy without deleting its audit history."""
        from looplab.engine.concept_registry import clear_concept_alias

        try:
            rec = clear_concept_alias(
                _memory_dir(), from_concept=body.from_concept, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision,
                expected_governance_revision=body.expected_governance_revision,
                action_id=body.action_id, require_existing=True,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {
            "ok": True, "alias": _public_cross_run_row(rec), "revision": rec["revision"],
            "governance_revision": rec["governance_revision"],
        }

    @router.post("/api/cross-run/concept-split")
    def concept_split(body: _ConceptSplit):
        """Record one bounded deterministic split rule set."""
        from looplab.engine.concept_registry import record_concept_split

        try:
            rec = record_concept_split(
                _memory_dir(), from_concept=body.from_concept,
                rules=[rule.model_dump() for rule in body.rules], default=body.default,
                by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
                expected_governance_revision=body.expected_governance_revision,
                action_id=body.action_id, require_existing=True,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {
            "ok": True, "split": _public_cross_run_row(rec), "revision": rec["revision"],
            "governance_revision": rec["governance_revision"],
        }

    @router.post("/api/cross-run/concept-split-clear")
    def concept_split_clear(body: _ConceptSource):
        """Undo the active split while preserving the append-only history."""
        from looplab.engine.concept_registry import clear_concept_split

        try:
            rec = clear_concept_split(
                _memory_dir(), from_concept=body.from_concept, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision,
                expected_governance_revision=body.expected_governance_revision,
                action_id=body.action_id, require_existing=True,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {
            "ok": True, "split": _public_cross_run_row(rec), "revision": rec["revision"],
            "governance_revision": rec["governance_revision"],
        }

    def _iter_log(path: Path):
        """Yield a curation ledger only after its complete paid-call history validates."""
        yield from _read_curation_rows(path)

    def _recent_log(name: str, limit: int) -> dict:
        path = Path(_memory_dir()) / name
        latest: deque[dict] = deque(maxlen=limit)
        count = 0
        for row in _iter_log(path):
            latest.append(_public_cross_run_row(row))
            count += 1
        return {
            "v": 1, "status": "complete", "complete": True,
            "entries": list(reversed(latest)), "n": count, "limit": limit,
        }

    @router.get("/api/cross-run/curation-log")
    def curation_log(limit: int = Query(20, ge=1, le=200)):
        return _read_governance(lambda: _recent_log("concept_curation_log.jsonl", limit))

    @router.get("/api/cross-run/claim-curation-log")
    def claim_curation_log(limit: int = Query(20, ge=1, le=200)):
        return _read_governance(lambda: _recent_log("claim_curation_log.jsonl", limit))

    def _steward_client():
        try:
            engine = getattr(srv, "engine", None) or getattr(srv, "_engine", None)
            client = engine._reflect_client() if engine is not None else None
            if client is not None:
                return client
        except Exception:  # noqa: BLE001
            pass
        from looplab.core.llm import make_llm_client
        return make_llm_client(srv.llm_settings())

    def _steward_response(record: dict) -> dict:
        invocation = _public_cross_run_row({key: record.get(key) for key in
                                            ("action_id", "revision", "outcome", "by", "at")})
        if not invocation.get("action_id"):
            invocation["action_id"] = cross_run_text(
                record.get("invocation_id"), max_chars=160,
                single_line=True, entropy=False)
        if record.get("action") == "steward-invocation-begun":
            # CODEX AGENT: replaying an ambiguous external call can charge twice. Same-key retries
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
        return {"ok": True,
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

    @router.post("/api/cross-run/concept-steward")
    def concept_steward(action_id: str = Query(..., min_length=1, max_length=160),
                        apply: bool = False):
        """Run a proposal-only taxonomy review; typed operator actions apply selected proposals."""
        if apply:
            raise HTTPException(422, "steward endpoints are proposal-only; apply typed operator actions")
        from looplab.engine.concept_registry import concept_governance_snapshot
        from looplab.engine.steward_invocation import run_steward_invocation

        # Refuse before client creation / durable paid-call claim: an unhealthy taxonomy cannot
        # produce a trustworthy prompt, and paying a steward to review a guessed projection is waste.
        _read_governance(lambda: concept_governance_snapshot(_memory_dir()))
        from looplab.engine.concept_steward import steward_concepts
        try:
            record, _replayed = run_steward_invocation(
                _memory_dir(), "concept", action_id, actor=_actor(), at=_timestamp(),
                prepare=_steward_client,
                invoke=lambda client: steward_concepts(
                    _memory_dir(), client, apply=False, by=_actor(), raise_on_failure=True),
                safe_error=lambda exc, phase: _safe_steward_error(exc, phase=phase),
                request={"surface": "owner-http"},
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return _steward_response(record)

    @router.post("/api/cross-run/claim-steward")
    def claim_steward(action_id: str = Query(..., min_length=1, max_length=160),
                      apply: bool = False):
        """Run a proposal-only claim review; typed operator actions apply selected proposals."""
        if apply:
            raise HTTPException(422, "steward endpoints are proposal-only; apply typed operator actions")
        from looplab.engine.claims import claim_governance_revision
        from looplab.engine.steward_invocation import run_steward_invocation

        _read_governance(lambda: claim_governance_revision(_memory_dir()))
        from looplab.engine.claim_steward import steward_claims
        try:
            record, _replayed = run_steward_invocation(
                _memory_dir(), "claim", action_id, actor=_actor(), at=_timestamp(),
                prepare=_steward_client,
                invoke=lambda client: steward_claims(
                    _memory_dir(), client, apply=False, by=_actor(), raise_on_failure=True),
                safe_error=lambda exc, phase: _safe_steward_error(exc, phase=phase),
                request={"surface": "owner-http"},
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return _steward_response(record)

    return router
