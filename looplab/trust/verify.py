"""D8 · Decoupled research Verifier (Phase 4). Kosmos's expert evaluation found cross-evidence
SYNTHESIS claims only ~57.9% accurate (vs ~85% for per-analysis statements) — generation and
verification must be decoupled (Aletheia: "essential for identifying flaws the model initially
overlooked"). This module checks a ResearchMemo's claims against their CITED evidence:

1. `check_claims` — deterministic layer, no model: does every claim cite evidence at all, do the
   cited node ids exist, and do metric numbers quoted in the statement match the cited nodes?
2. `verify_memo` — adds a single rubric-prompt LLM pass over the claims that survived layer 1
   (one call, one rubric — more consistent than an ensemble of judges, per Anthropic's
   multi-agent research evaluation), grading each claim supported/unsupported/unclear against
   the evidence text assembled from the run itself.

Audit-only: verdicts ride inside the memo dict on the `research_completed` event; they never
touch nodes or best-selection. Best-effort: any model failure downgrades to the deterministic
verdicts rather than blocking the memo.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from looplab.core.models import NodeStatus, RunState


def _evidence_text(claim: dict, state: RunState) -> str:
    """Assemble the checkable evidence a claim cites: the actual outcome of each cited node."""
    parts: list[str] = []
    for nid in (claim.get("node_ids") or [])[:8]:
        n = state.nodes.get(nid)
        if n is None:
            parts.append(f"#{nid}: (no such experiment)")
            continue
        if n.status is NodeStatus.failed:
            parts.append(f"#{nid} {n.operator}: FAILED ({n.error_reason or 'error'}) — "
                         f"{' '.join((n.idea.rationale or '').split())[:80]}")
        else:
            parts.append(f"#{nid} {n.operator}: metric={n.metric} params={n.idea.params} — "
                         f"{' '.join((n.idea.rationale or '').split())[:80]}")
    for u in (claim.get("urls") or [])[:4]:
        parts.append(f"source: {u}")
    return "\n".join(parts)


def check_claims(claims: list[dict], state: RunState) -> list[dict]:
    """Deterministic verification layer (pure, offline). Verdicts:
      - `unsupported` — no evidence cited at all, or every cited node id is unknown;
      - `cited`       — evidence exists (node ids resolve and/or a url is given); whether it
                        SEMANTICALLY supports the claim is the LLM rubric layer's (or a human's)
                        call, not this deterministic pass's.
    Returns [{statement, verdict, note}] aligned with `claims`.

    NOTE: this layer deliberately does NOT try to match numbers quoted in the statement against
    node metrics — a research claim legitimately quotes non-metric decimals (arXiv ids like
    2506.12928, percentages like 37.9, dataset sizes, p-values), and a regex can't tell those from
    a metric, so a numeric "confabulation" heuristic here produces false 'fabricated' labels on
    well-supported claims. Numeric correctness is left to the semantic (LLM) verifier."""
    out: list[dict] = []
    for c in claims or []:
        stmt = str(c.get("statement", "") or "")
        nids = [i for i in (c.get("node_ids") or []) if isinstance(i, int)]
        urls = [u for u in (c.get("urls") or []) if u]
        known = [i for i in nids if i in state.nodes]
        if not nids and not urls:
            out.append({"statement": stmt, "verdict": "unsupported",
                        "note": "no evidence cited"})
            continue
        if nids and not known and not urls:
            out.append({"statement": stmt, "verdict": "unsupported",
                        "note": f"cited experiments do not exist: {nids}"})
            continue
        out.append({"statement": stmt, "verdict": "cited", "note": ""})
    return out


class _VerdictOut(BaseModel):
    verdicts: list[str] = Field(default_factory=list)   # per-claim: supported|unsupported|unclear
    notes: list[str] = Field(default_factory=list)


_RUBRIC = (
    "You are a strict research verifier. For EACH numbered claim below, judge whether the cited "
    "evidence actually SUPPORTS the claim — not whether the claim sounds plausible. Rubric: "
    "supported = the evidence directly backs the claim; unsupported = the evidence is absent, "
    "contradicts it, or does not establish it; unclear = the evidence is related but "
    "insufficient. Default to unsupported when uncertain. Call `emit` exactly once with "
    "`verdicts` (one of supported|unsupported|unclear per claim, in order) and `notes` "
    "(one short reason per claim, in order)."
)


def verify_memo(memo: dict, state: RunState, client=None,
                parser: str = "tool_call") -> Optional[dict]:
    """Verify a memo's claims. Deterministic layer always runs; the LLM rubric pass upgrades
    `cited` claims to supported/unsupported/unclear when a client is wired. Returns
    {"verdicts": [{statement, verdict, note}], "method": "deterministic"|"llm",
     "unsupported": n} or None when the memo has no claims (nothing to verify)."""
    claims = list((memo or {}).get("claims") or [])
    if not claims:
        return None
    verdicts = check_claims(claims, state)
    method = "deterministic"
    todo = [(i, c) for i, (c, v) in enumerate(zip(claims, verdicts))
            if v["verdict"] == "cited"]
    if client is not None and todo:
        try:
            from looplab.core.parse import parse_structured
            lines = []
            for k, (i, c) in enumerate(todo, start=1):
                lines.append(f"CLAIM {k}: {str(c.get('statement', ''))[:300]}\n"
                             f"EVIDENCE:\n{_evidence_text(c, state)}")
            out = parse_structured(
                client,
                [{"role": "system", "content": _RUBRIC},
                 {"role": "user", "content": "\n\n".join(lines)}],
                _VerdictOut, parser)
            for k, (i, _c) in enumerate(todo):
                if k < len(out.verdicts) and out.verdicts[k] in ("supported", "unsupported",
                                                                 "unclear"):
                    verdicts[i]["verdict"] = out.verdicts[k]
                    if k < len(out.notes):
                        verdicts[i]["note"] = str(out.notes[k])[:200]
            method = "llm"
        except Exception:  # noqa: BLE001 — verification degrades, never blocks the memo
            pass
    bad = sum(1 for v in verdicts if v["verdict"] == "unsupported")
    return {"verdicts": verdicts, "method": method, "unsupported": bad}
