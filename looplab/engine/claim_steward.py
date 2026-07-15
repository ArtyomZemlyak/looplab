"""AGENTIC claim steward (§22.4) — the LLM counterpart of the operator's manual claim decisions.

`record_claim_decision` (ratify / reject / pin) is precise but needs a human to READ the claim set and
judge. This module lets an LLM do the judging: it reviews the evidence-grounded claim assessments — with
their support/oppose counts, epistemic state and current operator maturity — and PROPOSES decisions
(ratify a well-evidenced consistent claim, reject a contradicted/over-generalized/noise claim, pin a
critical one).

Same invariant as the concept steward: the LLM only PROPOSES; every write goes through the deterministic,
append-only, reversible `record_claim_decision` (scope-precise via the structured claim key). Proposals are
surfaced for operator ratification by default; a gated `apply` records them. Degrades to no proposals on no
client / any failure; never raises, never blocks the caller.
"""
from __future__ import annotations

_MAX_PROPOSALS = 10
_MAX_CLAIMS = 60             # bounded prompt — the most-evidenced / contested claims first


def propose_claim_curation(claims: list[dict], client, *, parser: str = "tool_call",
                           max_proposals: int = _MAX_PROPOSALS) -> dict:
    """Ask an LLM to review evidence-grounded `claims` (from `claim_assessments`) and PROPOSE operator
    decisions. Returns `{"decisions": [{statement, decision, scope, why}]}` of VALIDATED proposals (each
    references an existing claim + a valid decision; the machine already-decided claims are excluded from
    review so the steward doesn't churn them). Advisory: nothing is written here. Empty on no client / any
    failure."""
    empty: dict = {"decisions": []}
    # Only review claims the operator has NOT already decided (machine-proposed) — don't re-litigate a human
    # verdict. Contested/most-evidenced first (claims are already sorted that way).
    reviewable = [c for c in (claims or [])
                  if c.get("statement") and c.get("maturity", "machine-proposed") == "machine-proposed"]
    if client is None or not reviewable:
        return empty
    reviewable = reviewable[:_MAX_CLAIMS]
    known = {(str(c["statement"]), _scope_of(c)) for c in reviewable}
    try:
        from pydantic import BaseModel, Field

        from looplab.core.parse import parse_structured

        class _Decision(BaseModel):
            statement: str
            decision: str            # ratified | rejected | pinned
            scope: str = ""
            why: str = ""

        class _Curation(BaseModel):
            decisions: list[_Decision] = Field(default_factory=list)

        def _line(c: dict) -> str:
            return (f"- [{c.get('epistemic', '?')}: {c.get('n_support', 0)}↑/{c.get('n_oppose', 0)}↓"
                    f"{', scope=' + _scope_of(c) if _scope_of(c) else ''}] {str(c['statement'])[:200]}")
        # PROMPT CONTRACT (CLAUDE.md): the steward JUDGES the accumulated evidence — it may only decide on the
        # LISTED claims and must copy the statement text verbatim. Conservative: ratify only well-evidenced,
        # internally-consistent claims; reject contradicted/over-generalized/noise; pin the load-bearing few.
        system = (
            "You are the CLAIM steward for a cross-run ML research memory. You review evidence-grounded "
            "claims (each shows its epistemic state and support↑/oppose↓ evidence counts) and propose a few "
            "high-confidence operator DECISIONS to keep the memory trustworthy:\n"
            "- ratified: a well-evidenced, internally-consistent claim worth surfacing FIRST to future runs.\n"
            "- rejected: a claim that is contradicted by stronger evidence, over-generalized from one "
            "failure, or noise — it is dropped from agent context.\n"
            "- pinned: a load-bearing claim to always keep visible.\n"
            "Decide ONLY on the listed claims; copy the `statement` verbatim and its `scope` if shown. Be "
            f"conservative — call `emit` ONCE with at most {max_proposals} decisions (fewer is better; an "
            "empty list is fine).\n\nCLAIMS:\n" + ("\n".join(_line(c) for c in reviewable) or "(none)"))
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": "Propose the operator decisions (ratify / reject / pin)."}]
        out = parse_structured(client, msgs, _Curation, parser)
        return _validate(out, known, max_proposals=max_proposals)
    except Exception:  # noqa: BLE001 — agentic curation is best-effort; never block the caller
        return empty


def _scope_of(c: dict) -> str:
    """A claim's governing scope for the structured key — its first (sorted) task scope, or ""."""
    scopes = c.get("scopes") or []
    return str(scopes[0]) if scopes else ""


def _validate(out, known: set, *, max_proposals: int) -> dict:
    """Guardrails: the decision must be valid, the statement must match a REVIEWED claim (by text; scope is
    taken from the matching claim so the structured key is exact), and the total is capped."""
    from looplab.engine.claims import CLAIM_DECISIONS
    by_stmt = {}
    for (stmt, scope) in known:
        by_stmt.setdefault(stmt, scope)      # map statement -> its canonical scope
    seen, decisions = set(), []
    for d in (out.decisions or []):
        stmt = str(d.statement or "").strip()
        dec = str(d.decision or "").strip().lower()
        if stmt not in by_stmt or dec not in CLAIM_DECISIONS or stmt in seen:
            continue
        seen.add(stmt)
        decisions.append({"statement": stmt, "decision": dec, "scope": by_stmt[stmt],
                          "why": str(d.why or "")[:200]})
        if len(decisions) >= max(1, int(max_proposals)):
            break
    return {"decisions": decisions}


def curation_is_empty(curation: dict) -> bool:
    return not (curation.get("decisions"))


def apply_claim_curation(memory_dir, curation: dict, *, by: str = "steward", at: str = "") -> dict:
    """Record the proposed decisions through the SAME reversible `record_claim_decision` the operator uses
    (scope-precise via the structured claim key). Returns `{"applied", "skipped"}`; one bad proposal is
    skipped with its reason, never sinking the batch."""
    from looplab.engine.claims import record_claim_decision
    applied, skipped = [], []
    for d in (curation.get("decisions") or []):
        try:
            record_claim_decision(memory_dir, statement=d["statement"], decision=d["decision"],
                                  scope=d.get("scope", ""), note=d.get("why", ""), by=by, at=at)
            applied.append({"statement": d["statement"], "decision": d["decision"], "scope": d.get("scope", "")})
        except Exception as e:  # noqa: BLE001 — one invalid proposal must not sink the batch
            skipped.append({"statement": d.get("statement"), "reason": str(e)[:160]})
    return {"applied": applied, "skipped": skipped}


def steward_claims(memory_dir, client, *, lessons=None, apply: bool = False, by: str = "steward",
                   at: str = "", structured: bool = True, max_proposals: int = _MAX_PROPOSALS) -> dict:
    """One-call agentic claim steward over a memory dir: load the claim assessments (structured key by
    default, so decisions are scope-precise), ask the LLM to propose decisions, and — when `apply` — record
    them. Returns `{"proposals", "receipt"}` (receipt None when not applied)."""
    from looplab.engine.claims import claims_for_memory
    claims = claims_for_memory(memory_dir, lessons=lessons, structured=structured)
    proposals = propose_claim_curation(claims, client, max_proposals=max_proposals)
    receipt = None
    if apply and not curation_is_empty(proposals):
        receipt = apply_claim_curation(memory_dir, proposals, by=by, at=at)
    return {"proposals": proposals, "receipt": receipt}
