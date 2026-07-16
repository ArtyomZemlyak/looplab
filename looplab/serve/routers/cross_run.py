"""Bounded HTTP surface for Part IV/V cross-run knowledge and operator governance.

Reads expose live, revision-labelled projections. Mutations are explicit, append-only operator actions:
every request carries a stable action id and the exact ledger revision observed by the caller. Agents can
ask stewards for proposals, but only the typed operator endpoints below may change portfolio meaning.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt


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
    # One proposal invocation per kind at a time in this server process. This closes the common ASGI
    # retry race where two requests with the same action_id both pay for an LLM call before either receipt
    # exists. The durable append still supplies cross-process receipt idempotency/fail-closed conflicts.
    _steward_locks = {"concept": threading.Lock(), "claim": threading.Lock()}

    def _memory_dir() -> str:
        memory_dir = getattr(srv.llm_settings(), "memory_dir", None)
        if not memory_dir:
            raise HTTPException(400, "no memory_dir configured")
        return str(memory_dir)

    def _actor() -> str:
        return "deployment-owner" if getattr(srv, "owner_auth_enabled", False) else "local-operator"

    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _revisions(memory_dir: str) -> dict:
        from looplab.engine.claims import claim_governance_revision
        from looplab.engine.concept_registry import concept_governance_revision
        return {
            "claims": claim_governance_revision(memory_dir),
            "concept_aliases": concept_governance_revision(memory_dir, "aliases"),
            "concept_splits": concept_governance_revision(memory_dir, "splits"),
        }

    def _raise_governance_error(exc: Exception) -> None:
        from looplab.engine.claims import ClaimDecisionConflict, ClaimDecisionIdempotencyConflict
        from looplab.engine.concept_registry import (
            ConceptGovernanceConflict,
            ConceptGovernanceIdempotencyConflict,
        )
        from looplab.events.eventstore import EventStoreLockError

        if isinstance(exc, ClaimDecisionConflict):
            raise HTTPException(409, detail={
                "code": "claim_revision_conflict",
                "expected_revision": exc.expected_revision,
                "current_revision": exc.current_revision,
            }) from exc
        if isinstance(exc, ConceptGovernanceConflict):
            raise HTTPException(409, detail={
                "code": "concept_revision_conflict",
                "ledger": exc.path.name,
                "expected_revision": exc.expected,
                "current_revision": exc.actual,
            }) from exc
        if isinstance(exc, (ClaimDecisionIdempotencyConflict,
                            ConceptGovernanceIdempotencyConflict)):
            raise HTTPException(409, detail={
                "code": "action_id_reused",
                "message": str(exc),
            }) from exc
        if isinstance(exc, EventStoreLockError):
            raise HTTPException(503, detail={
                "code": "governance_lock_unavailable",
                "message": "governance storage cannot guarantee an exclusive write",
            }) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(422, str(exc)) from exc
        raise exc

    @router.get("/api/cross-run/atlas")
    def atlas(limit: int = Query(24, ge=1, le=100),
              scope_task: str = Query("", max_length=500)):
        """Live structured Research Atlas projection, bounded per section."""
        from looplab.engine.claims import atlas_for_memory

        memory_dir = _memory_dir()
        payload = atlas_for_memory(memory_dir, scope_task=scope_task, max_items=limit,
                                   structured=True)
        payload.update({
            "projection": "live",
            "scope_task": scope_task,
            "page": {"limit": limit},
            "revisions": _revisions(memory_dir),
        })
        return payload

    @router.get("/api/cross-run/claims")
    def claims(contested: bool = False,
               scope_task: str = Query("", max_length=500),
               limit: int = Query(80, ge=1, le=200),
               offset: int = Query(0, ge=0, le=1_000_000)):
        """Scope/polarity-safe claims with stable IDs and bounded offset pagination."""
        from looplab.engine.claims import claims_for_memory, claim_governance_revision

        memory_dir = _memory_dir()
        rows = claims_for_memory(memory_dir, scope_task=scope_task, structured=True)
        if contested:
            rows = [row for row in rows if row.get("epistemic") == "mixed"]
        total = len(rows)
        page = rows[offset:offset + limit]
        return {
            "claims": page,
            "n": total,
            "returned": len(page),
            "offset": offset,
            "limit": limit,
            "scope_task": scope_task,
            "revision": claim_governance_revision(memory_dir),
        }

    @router.post("/api/cross-run/claim-decide")
    def claim_decide(body: _ClaimDecision):
        """Ratify/reject/pin/clear exactly the claim ID and revision the operator observed."""
        from looplab.engine.claim_key import claim_uid
        from looplab.engine.claims import claims_for_memory, load_claim_decisions, record_claim_decision

        expected_uid = claim_uid(body.statement, scope=body.scope, metric=body.metric)
        if body.claim_uid != expected_uid:
            raise HTTPException(409, detail={
                "code": "claim_target_changed",
                "expected_claim_uid": expected_uid,
            })

        memory_dir = _memory_dir()

        def _validate_target() -> None:
            # A content-addressed UID proves only that body fields agree with each other. The
            # claim must also exist in the current evidence projection, and that projection must be the one
            # the operator reviewed. This callback runs after action-id replay and governance CAS while the
            # decision ledger lock is held, so lost-response retries remain idempotent.
            if body.decision == "clear" and body.claim_uid in load_claim_decisions(memory_dir):
                # Clearing targets an existing policy record, not live evidence. It must stay possible after
                # retention retires the claim and for a global fallback whose UID differs from a scoped row.
                return
            current = next((claim for claim in claims_for_memory(memory_dir, structured=True)
                            if claim.get("claim_uid") == body.claim_uid), None)
            if current is None:
                raise HTTPException(409, detail={"code": "claim_target_missing"})
            if current.get("evidence_digest") != body.evidence_digest:
                raise HTTPException(409, detail={
                    "code": "claim_evidence_changed",
                    "current_evidence_digest": current.get("evidence_digest"),
                })
        try:
            rec = record_claim_decision(
                memory_dir, statement=body.statement, scope=body.scope, metric=body.metric,
                decision=body.decision, note=body.note, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision, action_id=body.action_id,
                evidence_digest=body.evidence_digest, validate=_validate_target,
            )
        except Exception as exc:  # converted to stable HTTP semantics below
            _raise_governance_error(exc)
        return {"ok": True, "decision": rec, "revision": rec["revision"]}

    @router.post("/api/cross-run/concept-merge")
    def concept_merge(body: _ConceptMerge):
        """Merge one non-empty concept into another; purge is a separate confirmed action."""
        from looplab.engine.concept_registry import record_concept_alias

        try:
            rec = record_concept_alias(
                _memory_dir(), from_concept=body.from_concept, to_concept=body.to_concept,
                by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
                action_id=body.action_id,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {"ok": True, "alias": rec, "revision": rec["revision"]}

    @router.post("/api/cross-run/concept-purge")
    def concept_purge(body: _ConceptPurge):
        """Explicitly tombstone one concept after a typed confirmation."""
        from looplab.engine.concept_registry import record_concept_alias

        try:
            rec = record_concept_alias(
                _memory_dir(), from_concept=body.from_concept, to_concept="",
                by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
                action_id=body.action_id,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {"ok": True, "alias": rec, "revision": rec["revision"]}

    @router.post("/api/cross-run/concept-alias-clear")
    def concept_alias_clear(body: _ConceptSource):
        """Undo the current alias/purge policy without deleting its audit history."""
        from looplab.engine.concept_registry import clear_concept_alias

        try:
            rec = clear_concept_alias(
                _memory_dir(), from_concept=body.from_concept, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision, action_id=body.action_id,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {"ok": True, "alias": rec, "revision": rec["revision"]}

    @router.post("/api/cross-run/concept-split")
    def concept_split(body: _ConceptSplit):
        """Record one bounded deterministic split rule set."""
        from looplab.engine.concept_registry import record_concept_split

        try:
            rec = record_concept_split(
                _memory_dir(), from_concept=body.from_concept,
                rules=[rule.model_dump() for rule in body.rules], default=body.default,
                by=_actor(), at=_timestamp(), expected_revision=body.expected_revision,
                action_id=body.action_id,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {"ok": True, "split": rec, "revision": rec["revision"]}

    @router.post("/api/cross-run/concept-split-clear")
    def concept_split_clear(body: _ConceptSource):
        """Undo the active split while preserving the append-only history."""
        from looplab.engine.concept_registry import clear_concept_split

        try:
            rec = clear_concept_split(
                _memory_dir(), from_concept=body.from_concept, by=_actor(), at=_timestamp(),
                expected_revision=body.expected_revision, action_id=body.action_id,
            )
        except Exception as exc:
            _raise_governance_error(exc)
        return {"ok": True, "split": rec, "revision": rec["revision"]}

    def _iter_log(path: Path):
        """Stream valid object rows without materializing an unbounded curation ledger."""
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 - mutable audit ledgers skip damaged rows
                    continue
                if isinstance(row, dict):
                    yield row

    def _recent_log(name: str, limit: int) -> dict:
        path = Path(_memory_dir()) / name
        latest: deque[dict] = deque(maxlen=limit)
        count = 0
        for row in _iter_log(path):
            latest.append(row)
            count += 1
        return {"entries": list(reversed(latest)), "n": count, "limit": limit}

    @router.get("/api/cross-run/curation-log")
    def curation_log(limit: int = Query(20, ge=1, le=200)):
        return _recent_log("concept_curation_log.jsonl", limit)

    @router.get("/api/cross-run/claim-curation-log")
    def claim_curation_log(limit: int = Query(20, ge=1, le=200)):
        return _recent_log("claim_curation_log.jsonl", limit)

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

    def _steward_log(name: str, kind: str, action_id: str, *, proposals=None,
                      receipt=None, error: str = "") -> dict:
        """Durably retain every on-demand steward outcome, including empty/error results."""
        from looplab.engine.concept_registry import _append_governance
        path = Path(_memory_dir()) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        has_proposals = isinstance(proposals, dict) and any(
            isinstance(value, list) and value for value in proposals.values())
        rec = {
            "v": 1, "action": "steward-invocation", "from": kind,
            "action_id": action_id, "by": _actor(), "at": _timestamp(),
            "outcome": "error" if error else ("proposed" if has_proposals else "empty"),
            "proposals": proposals or {}, "receipt": receipt,
        }
        if error:
            from looplab.trust.redact import redact_secrets
            rec["error"] = redact_secrets(str(error), entropy=True)[:500]
        try:
            return _append_governance(path, rec)
        except Exception as exc:
            _raise_governance_error(exc)

    def _cached_steward(name: str, action_id: str) -> dict | None:
        path = Path(_memory_dir()) / name
        for row in _iter_log(path):
            if (row.get("action") == "steward-invocation"
                    and str(row.get("action_id") or "") == action_id):
                return row
        return None

    def _steward_response(record: dict) -> dict:
        invocation = {key: record.get(key) for key in
                      ("action_id", "revision", "outcome", "by", "at")}
        if record.get("outcome") == "error":
            raise HTTPException(400, detail={
                "code": "steward_failed", "message": record.get("error") or "steward failed",
                "invocation": invocation,
            })
        return {"ok": True, "proposals": record.get("proposals") or {},
                "receipt": record.get("receipt"), "invocation": invocation}

    def _safe_steward_error(exc: Exception, *, phase: str) -> str:
        # Provider failures can embed endpoints, account ids, or credential fragments. Persist only the
        # assistant surface's allow-listed failure class, never the raw exception text.
        from looplab.serve.assistant import safe_assistant_failure
        return f"{phase}:{safe_assistant_failure(exc)['error_kind']}"

    @contextmanager
    def _steward_guard(kind: str, name: str):
        from looplab.events.eventstore import EventStoreLockError, _interprocess_lock

        # Durable action-id dedupe happens only after the LLM returns. Hold a separate cross-process
        # invocation lock across cache-check + call + receipt; the process-local lock alone
        # still lets two ASGI workers pay for the same nondeterministic request.
        try:
            base = Path(_memory_dir())
            base.mkdir(parents=True, exist_ok=True)
            with _steward_locks[kind]:
                with _interprocess_lock(base / f"{name}.invoke.lock", required=True):
                    yield
        except EventStoreLockError as exc:
            _raise_governance_error(exc)

    @router.post("/api/cross-run/concept-steward")
    def concept_steward(action_id: str = Query(..., min_length=1, max_length=160),
                        apply: bool = False):
        """Run a proposal-only taxonomy review; typed operator actions apply selected proposals."""
        if apply:
            raise HTTPException(422, "steward endpoints are proposal-only; apply typed operator actions")
        with _steward_guard("concept", "concept_curation_log.jsonl"):
            cached = _cached_steward("concept_curation_log.jsonl", action_id)
            if cached is not None:
                return _steward_response(cached)
            try:
                client = _steward_client()
            except Exception as exc:  # noqa: BLE001
                return _steward_response(_steward_log(
                    "concept_curation_log.jsonl", "concept", action_id,
                    error=_safe_steward_error(exc, phase="client")))
            from looplab.engine.concept_steward import steward_concepts
            try:
                output = steward_concepts(_memory_dir(), client, apply=False, by=_actor())
            except Exception as exc:  # noqa: BLE001
                record = _steward_log("concept_curation_log.jsonl", "concept", action_id,
                                      error=_safe_steward_error(exc, phase="steward"))
            else:
                record = _steward_log("concept_curation_log.jsonl", "concept", action_id,
                                      proposals=output.get("proposals"), receipt=output.get("receipt"))
        return _steward_response(record)

    @router.post("/api/cross-run/claim-steward")
    def claim_steward(action_id: str = Query(..., min_length=1, max_length=160),
                      apply: bool = False):
        """Run a proposal-only claim review; typed operator actions apply selected proposals."""
        if apply:
            raise HTTPException(422, "steward endpoints are proposal-only; apply typed operator actions")
        with _steward_guard("claim", "claim_curation_log.jsonl"):
            cached = _cached_steward("claim_curation_log.jsonl", action_id)
            if cached is not None:
                return _steward_response(cached)
            try:
                client = _steward_client()
            except Exception as exc:  # noqa: BLE001
                return _steward_response(_steward_log(
                    "claim_curation_log.jsonl", "claim", action_id,
                    error=_safe_steward_error(exc, phase="client")))
            from looplab.engine.claim_steward import steward_claims
            try:
                output = steward_claims(_memory_dir(), client, apply=False, by=_actor())
            except Exception as exc:  # noqa: BLE001
                record = _steward_log("claim_curation_log.jsonl", "claim", action_id,
                                      error=_safe_steward_error(exc, phase="steward"))
            else:
                record = _steward_log("claim_curation_log.jsonl", "claim", action_id,
                                      proposals=output.get("proposals"), receipt=output.get("receipt"))
        return _steward_response(record)

    return router
