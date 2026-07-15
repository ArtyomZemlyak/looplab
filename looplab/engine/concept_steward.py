"""AGENTIC concept-taxonomy steward (§21.20.13, §22.4) — the LLM counterpart of the operator's manual
merge/split/purge.

The operator writes (`concept-merge`/`concept-split`) are precise but require a human to NOTICE that two
slugs are the same technique, or that one coarse slug conflates two. This module lets an LLM do the
noticing: it reviews the cross-run concept graph (the deterministic `portfolio_concept_overview`) and
PROPOSES a curation — merges, splits, purges — as structured data.

Architectural invariant (why this is a steward, not a fold-time mutation): the LLM only ever PROPOSES.
Every write goes through the SAME deterministic, append-only, reversible `record_concept_alias` /
`record_concept_split` the operator CLI uses, so `fold()` and the read-models stay pure and replay-safe.
Proposals are surfaced for operator ratification by default; a gated `auto` flag records them directly
(the append-only reversibility is the safety net). Degrades to an empty curation on no client / any
failure; never raises, never blocks the caller (mirrors `tag_text_llm`).
"""
from __future__ import annotations

from typing import Optional

_MAX_PROPOSALS = 12          # a bounded curation per pass — the steward suggests the highest-value few
_MAX_GRAPH = 200             # cap the concepts shown to the model (most-explored first) — bounded prompt


def propose_concept_curation(overview: dict, client, *, parser: str = "tool_call",
                             max_proposals: int = _MAX_PROPOSALS) -> dict:
    """Ask an LLM to review the portfolio concept graph (`overview` from `portfolio_concept_overview`) and
    PROPOSE a taxonomy curation. Returns `{"merges", "splits", "purges"}` of VALIDATED proposals (each
    references only concepts present in the overview; self/no-op proposals dropped — cycles are rejected
    later at record time). Advisory: nothing is written here. Empty curation on no client / any failure."""
    empty = {"merges": [], "splits": [], "purges": []}
    concepts = [e for e in (overview.get("concepts") or []) if e.get("concept")]
    if client is None or not concepts:
        return empty
    known = {str(e["concept"]) for e in concepts}
    try:
        from pydantic import BaseModel, Field

        from looplab.core.parse import parse_structured

        class _Merge(BaseModel):
            from_concept: str
            to_concept: str
            why: str = ""

        class _SplitRule(BaseModel):
            to: str
            when_any: list[str] = Field(default_factory=list)

        class _Split(BaseModel):
            from_concept: str
            rules: list[_SplitRule] = Field(default_factory=list)
            default: str = ""
            why: str = ""

        class _Curation(BaseModel):
            merges: list[_Merge] = Field(default_factory=list)
            splits: list[_Split] = Field(default_factory=list)
            purges: list[str] = Field(default_factory=list)

        # PROMPT CONTRACT (CLAUDE.md): the model curates an EXISTING taxonomy — it may only reference the
        # listed slugs (merge targets/split sources), never invent unrelated ones; a split's finer targets
        # ARE new labels (that is the point of a split). It is told to be conservative (few, high-confidence
        # proposals) because every write is reversible but operator-visible.
        lines = [f"- {e['concept']} ({e.get('n_runs', 0)} run(s))" for e in concepts[:_MAX_GRAPH]]
        system = (
            "You are the taxonomy STEWARD for a cross-run ML research memory. You review the list of "
            "CONCEPT slugs explored across runs and propose a small, high-confidence CURATION so the "
            "portfolio's concept graph stays clean. Propose only what you are confident about:\n"
            "- MERGE: two listed slugs that name the SAME technique/family (different spelling/abbreviation) "
            "-> pick one as canonical `to_concept` (a slug already in the list).\n"
            "- SPLIT: ONE listed slug that conflates DISTINCT techniques -> finer `to` labels, each with "
            "`when_any` trigger terms; a run is re-tagged to a finer label when its OTHER concepts contain "
            "any trigger term. Provide a `default` (usually the original slug) for runs matching no rule.\n"
            "- PURGE: a listed slug that is noise / not a real research concept.\n"
            "Key on the underlying METHOD, not the surface name. Reference ONLY listed slugs as merge "
            f"targets and split/purge sources. Call `emit` ONCE with at most {max_proposals} total proposals "
            "(fewer is better). Empty lists are fine if the graph is already clean.\n\nCONCEPTS:\n"
            + ("\n".join(lines) or "(empty)"))
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": "Propose the curation (merges / splits / purges)."}]
        out = parse_structured(client, msgs, _Curation, parser)
        return _validate_curation(out, known, max_proposals=max_proposals)
    except Exception:  # noqa: BLE001 — agentic curation is best-effort; never block the caller
        return empty


def _validate_curation(out, known: set, *, max_proposals: int) -> dict:
    """Deterministic guardrails over the LLM proposal: sources/targets must be KNOWN concepts, drop self /
    no-op proposals and empty splits, cap the total. (Cycle/self-link rejection is enforced again at record
    time by `record_concept_alias`.) The steward can only ever propose reversible, in-vocabulary edits."""
    from looplab.engine.concept_registry import normalize_key
    kn = {normalize_key(k) for k in known}
    merges, splits, purges, budget = [], [], [], max(1, int(max_proposals))

    for m in (out.merges or []):
        src, dst = normalize_key(m.from_concept), normalize_key(m.to_concept)
        # BOTH ends must be in the known vocabulary — the prompt asks the model to pick a canonical FROM the
        # list, so a merge target that isn't a listed concept is a hallucination, dropped (mega-review).
        if src in kn and dst in kn and src != dst and len(merges) + len(splits) + len(purges) < budget:
            merges.append({"from_concept": src, "to_concept": dst, "why": str(m.why or "")[:200]})
    for s in (out.splits or []):
        src = normalize_key(s.from_concept)
        rules = [{"to": normalize_key(r.to),
                  "when_any": [normalize_key(t) for t in (r.when_any or []) if normalize_key(t)]}
                 for r in (s.rules or []) if normalize_key(r.to) and normalize_key(r.to) != src]
        rules = [r for r in rules if r["when_any"]]
        if src in kn and rules and len(merges) + len(splits) + len(purges) < budget:
            splits.append({"from_concept": src, "rules": rules,
                           "default": normalize_key(s.default), "why": str(s.why or "")[:200]})
    for p in (out.purges or []):
        pk = normalize_key(p)
        if pk in kn and pk not in {x["from_concept"] for x in purges} \
                and len(merges) + len(splits) + len(purges) < budget:
            purges.append({"from_concept": pk})
    return {"merges": merges, "splits": splits, "purges": purges}


def curation_is_empty(curation: dict) -> bool:
    return not (curation.get("merges") or curation.get("splits") or curation.get("purges"))


def apply_concept_curation(memory_dir, curation: dict, *, by: str = "steward", at: str = "") -> dict:
    """Record a curation as GOVERNANCE writes through the SAME deterministic, reversible `record_*` the
    operator uses (merge/split/purge). Returns a receipt `{"applied":[...], "skipped":[{action, reason}]}`.
    A record that raises (e.g. a cycle-closing merge) is skipped with its reason, never aborting the batch —
    the steward's other, valid proposals still land. Idempotent-friendly: re-applying a merge is a harmless
    duplicate alias (last-write-wins)."""
    from looplab.engine.concept_registry import record_concept_alias, record_concept_split
    applied, skipped = [], []
    for m in (curation.get("merges") or []):
        try:
            record_concept_alias(memory_dir, from_concept=m["from_concept"], to_concept=m["to_concept"],
                                 by=by, at=at)
            applied.append({"action": "merge", **{k: m[k] for k in ("from_concept", "to_concept")}})
        except Exception as e:  # noqa: BLE001 — one invalid proposal must not sink the batch
            skipped.append({"action": "merge", "from_concept": m.get("from_concept"), "reason": str(e)[:160]})
    for s in (curation.get("splits") or []):
        try:
            record_concept_split(memory_dir, from_concept=s["from_concept"], rules=s["rules"],
                                 default=s.get("default", ""), by=by, at=at)
            applied.append({"action": "split", "from_concept": s["from_concept"],
                            "into": [r["to"] for r in s["rules"]]})
        except Exception as e:  # noqa: BLE001
            skipped.append({"action": "split", "from_concept": s.get("from_concept"), "reason": str(e)[:160]})
    for p in (curation.get("purges") or []):
        try:
            record_concept_alias(memory_dir, from_concept=p["from_concept"], to_concept="", by=by, at=at)
            applied.append({"action": "purge", "from_concept": p["from_concept"]})
        except Exception as e:  # noqa: BLE001
            skipped.append({"action": "purge", "from_concept": p.get("from_concept"), "reason": str(e)[:160]})
    return {"applied": applied, "skipped": skipped}


def steward_concepts(memory_dir, client, *, aliases: Optional[dict] = None, splits: Optional[dict] = None,
                     apply: bool = False, by: str = "steward", at: str = "",
                     max_proposals: int = _MAX_PROPOSALS) -> dict:
    """One-call agentic steward over a memory dir: load the concept overview (honoring EXISTING
    aliases/splits so already-curated concepts are not re-proposed), ask the LLM to propose a curation, and
    — when `apply` — record it through the deterministic writes. Returns `{"proposals", "receipt"}` (receipt
    is None when not applied). Pure-ish: reads the store + one LLM call; writes only when `apply`."""
    from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    from pathlib import Path
    base = Path(memory_dir) if memory_dir else None
    cp = base / "concept_capsules.jsonl" if base else None
    caps = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    aliases = load_concept_aliases(memory_dir) if aliases is None else aliases
    splits = load_concept_splits(memory_dir) if splits is None else splits
    overview = portfolio_concept_overview(caps, aliases=aliases, splits=splits)
    proposals = propose_concept_curation(overview, client, max_proposals=max_proposals)
    receipt = None
    if apply and not curation_is_empty(proposals):
        receipt = apply_concept_curation(memory_dir, proposals, by=by, at=at)
    return {"proposals": proposals, "receipt": receipt}
