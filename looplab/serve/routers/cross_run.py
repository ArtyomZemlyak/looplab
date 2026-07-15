"""Cross-run knowledge HTTP surface (PART V §22): the Research Atlas DATA for the UI + the OPERATOR
governance write (ratify/reject/pin a claim). Read is portfolio-wide over `settings.memory_dir`; the
POST is the §22.4 operator action — the only way to change cross-run MEANING by hand. Agents never touch
this router; it is the human/UI surface. All handlers degrade gracefully when no memory dir is configured.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


class _ClaimDecision(BaseModel):
    statement: str
    decision: str            # "ratified" | "rejected" | "pinned"
    note: str = ""


def build_router(srv) -> APIRouter:
    router = APIRouter()

    def _memory_dir():
        md = getattr(srv.llm_settings(), "memory_dir", None)   # effective Settings (UI overrides merged)
        if not md:
            raise HTTPException(400, "no memory_dir configured")
        return md

    def _load():
        from looplab.engine.memory import ConceptCapsuleStore
        from looplab.events.eventstore import read_jsonl_lenient
        import json
        base = Path(_memory_dir())
        lp, cp = base / "lessons.jsonl", base / "concept_capsules.jsonl"
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if lp.exists() else []
        caps = ConceptCapsuleStore(cp).all() if cp.exists() else []
        return base, lessons, caps

    @router.get("/api/cross-run/atlas")
    def atlas():
        """The Research Atlas payload — explored / thin / contradictory + the bounded context pack —
        with D8 research claims + operator decisions overlaid. Read-only, portfolio-wide."""
        from looplab.engine.claims import load_claim_decisions, load_research_claims, portfolio_atlas
        base, lessons, caps = _load()
        return portfolio_atlas(lessons, caps, decisions=load_claim_decisions(base),
                               research_claims=load_research_claims(base))

    @router.get("/api/cross-run/claims")
    def claims(contested: bool = False):
        """Evidence-grounded claims (support/oppose + epistemic + operator maturity)."""
        from looplab.engine.claims import claims_for_memory
        base, lessons, _ = _load()
        out = claims_for_memory(base, lessons=lessons)   # + D8 claims + decisions
        if contested:
            out = [c for c in out if c["epistemic"] == "mixed"]
        return {"claims": out, "n": len(out)}

    @router.post("/api/cross-run/claim-decide")
    def claim_decide(body: _ClaimDecision):
        """OPERATOR governance write (§22.4): ratify / reject / pin a claim. Append-only, reversible."""
        from looplab.engine.claims import record_claim_decision
        try:
            rec = record_claim_decision(_memory_dir(), statement=body.statement,
                                        decision=body.decision, note=body.note, by="ui")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "decision": rec}

    return router
