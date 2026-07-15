"""Taxonomy-aware hypothesis-board dedup analysis (PART IV D4, §21.5/§21.12 — Phase 1d, offline analytic).

**Why this exists.** The hypothesis board (`RunState.hypotheses`) mirrors the node narrowing: measured on
the `rubertlite` run, **63 of 107 hypotheses touch `loss/decoupled-contrastive`** and 39 touch temperature
(§21.12). Blind board consolidation (`hybrid_merge`) is either too lax (redundant siblings survive) or,
tuned stricter, risks collapsing legitimate cross-branch variants. The right control is NOT a single
threshold but **taxonomy-awareness** (§21.5): merge aggressively *within* a concept, keep *cross-branch*
items distinct even when lexically similar.

**What this is.** A pure analysis over the board's concept tags:
  * **within-concept clusters** — the merge-aggressively groups (the big `loss/decoupled-contrastive`
    cluster is the compression opportunity);
  * **compression** — how many hypotheses collapse if same-concept-set paraphrases merge;
  * **false-merge risks** — pairs that a blind lexical/vector merge WOULD collapse (`HybridRetriever`
    candidates, ≥2 signals) but whose concept sets are DISJOINT, so a taxonomy-aware merge must keep them
    apart. (§21.12 honest negative: this risk was 0 pairs on the `rubertlite` run — the winning-region
    hypotheses used distinct vocabulary — but the *protective* value is what a taxonomy-aware merge buys.)

**Discipline.** Offline analytic (early lane): pure and deterministic over `(RunState, ConceptGraph, tags)`
with the default `hash_embed` retriever (deterministic — `hybrid_merge` is replay-safe by construction);
no I/O, no LLM, no wall-clock; writes nothing and never merges anything. Wiring the taxonomy distance into
the live `hybrid_merge`/`agent_merge` strictness is Phase 2.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from looplab.core.models import RunState
from looplab.search.concept_graph import ConceptGraph, tag_text


def dedup_analysis(state: RunState, graph: ConceptGraph, *,
                   tags: Optional[dict[str, frozenset[str]]] = None,
                   embed=None, min_signals: int = 2, max_pairs: int = 200) -> dict:
    """Taxonomy-aware dedup read over the hypothesis board (§21.5). Pure/deterministic. `tags` maps a
    hypothesis id -> its concept set (default: the deterministic `tag_text` over each statement, plural-
    aware). Returns:

      n_hypotheses     - board size
      tagged/untagged  - hypotheses with / without any concept tag
      concept_clusters - [{concept, count, frac, hyp_ids}] sorted by size — the within-concept merge groups
      top_cluster      - the biggest concept cluster (the redundancy the board should compress)
      redundancy_frac  - top_cluster.count / tagged (the "63/107 touch DCL" = 0.59 signal)
      compression      - tagged - distinct-concept-sets (same-concept-set paraphrases that can merge)
      false_merge_risks- [{a, b, similarity}] lexically/vector-similar but concept-DISJOINT pairs (keep-distinct)
      false_merge_count- len(false_merge_risks)
    """
    hyps = list((state.hypotheses or {}).values())
    n = len(hyps)
    if n == 0:
        return {"n_hypotheses": 0, "tagged": 0, "untagged": 0, "concept_clusters": [],
                "top_cluster": None, "redundancy_frac": 0.0, "compression": 0,
                "false_merge_risks": [], "false_merge_count": 0}
    if tags is None:
        # HT (§21.18): prefer the recorded AGENTIC hypothesis tags (`hypothesis_concepts`, the LLM tagger)
        # over the tag_text alias heuristic — per hypothesis, falling back to tag_text for any not yet
        # tagged (a board entry newer than the last cadence). Stays PURE (no LLM here; the tagging happened
        # at the cadence). No cache -> the deterministic alias tagger, exactly as before.
        cache = getattr(state, "hypothesis_concepts", None) or {}
        tags = {h.id: (frozenset(cache[h.id]) if h.id in cache
                       else tag_text(h.statement, graph, allow_plural=True)) for h in hyps}

    # within-concept clusters (a hypothesis with several concepts appears in each) — the merge groups
    by_concept: dict[str, list[str]] = defaultdict(list)
    for h in hyps:
        for c in tags.get(h.id, frozenset()):
            by_concept[c].append(h.id)
    concept_clusters = sorted(
        ({"concept": c, "count": len(ids), "frac": round(len(ids) / n, 4), "hyp_ids": sorted(ids)}
         for c, ids in by_concept.items()),
        key=lambda d: (-d["count"], d["concept"]))

    tagged_hyps = [h for h in hyps if tags.get(h.id)]
    n_tagged = len(tagged_hyps)
    distinct_sets = len({tags[h.id] for h in tagged_hyps})       # frozensets are hashable
    # `compression` is an UPPER-BOUND redundancy ESTIMATE, not a merge decision: it counts how many tagged
    # hypotheses share a coarse concept set (equal sets are only a merge SUGGESTION — two hypotheses in the
    # same concept branch may still be materially different or opposite). It is reported as "compression
    # AVAILABLE", never acted on; the actual merge adjudication is the agent + the lexical/BM25/vector
    # `risks` check below (disjoint-set pairs a blind merge would wrongly join).
    compression = n_tagged - distinct_sets
    top = concept_clusters[0] if concept_clusters else None
    redundancy_frac = round(top["count"] / n_tagged, 4) if (top and n_tagged) else 0.0

    # false-merge risks: pairs a blind merge would join (>=min_signals of lexical/BM25/vector) whose
    # concept sets are DISJOINT (so they belong to different branches and must stay apart).
    risks: list[dict] = []
    try:
        from looplab.search.hybrid_merge import HybridRetriever
        statements = [h.statement for h in hyps]
        retr = HybridRetriever(statements, embed=embed)
        seen: set[tuple[int, int]] = set()
        # Collect EVERY disjoint-concept candidate pair (bounded: k=5 candidates × n hypotheses), then
        # sort by similarity and cap — truncating during collection would keep the first-DISCOVERED pairs
        # (ascending hypothesis index), not the highest-similarity ones the report is meant to surface.
        for i, h in enumerate(hyps):
            for j, score in retr.candidates(h.statement, k=5, exclude=i, min_signals=min_signals):
                pair = (min(i, j), max(i, j))
                if pair in seen:
                    continue
                seen.add(pair)
                ci, cj = tags.get(hyps[i].id, frozenset()), tags.get(hyps[j].id, frozenset())
                if ci and cj and not (ci & cj):     # similar text, disjoint concepts -> keep distinct
                    risks.append({"a": hyps[i].statement, "b": hyps[j].statement,
                                  "similarity": round(score, 4)})
    except Exception:  # noqa: BLE001 — the retriever is optional; the concept analysis stands alone
        pass
    risks.sort(key=lambda r: (-r["similarity"], r["a"], r["b"]))
    risks = risks[:max_pairs]                       # cap AFTER sorting -> the top-similarity risks survive

    return {
        "n_hypotheses": n,
        "tagged": n_tagged,
        "untagged": n - n_tagged,
        "concept_clusters": concept_clusters,
        "top_cluster": top,
        "redundancy_frac": redundancy_frac,
        "compression": compression,
        "false_merge_risks": risks,
        "false_merge_count": len(risks),
    }


def dedup_report(state: RunState, graph: ConceptGraph, **kwargs) -> str:
    """A compact text diagnostic over the board dedup analysis — for the CLI. Pure."""
    a = dedup_analysis(state, graph, **kwargs)
    lines = [f"Taxonomy-aware board dedup  ({a['n_hypotheses']} hypotheses, {a['tagged']} tagged)"]
    if a["top_cluster"]:
        t = a["top_cluster"]
        lines.append(f"  most-redundant concept: {t['concept']} — {t['count']} hypotheses "
                     f"({a['redundancy_frac']} of tagged); merge aggressively WITHIN it")
    lines.append(f"  compression available: {a['compression']} same-concept-set paraphrases could merge")
    for c in a["concept_clusters"][:6]:
        if c["count"] > 1:
            lines.append(f"    · {c['concept']}: {c['count']}")
    if a["false_merge_count"]:
        lines.append(f"  ⚠ {a['false_merge_count']} cross-branch look-alike pair(s) a blind merge would "
                     "wrongly collapse — keep these distinct:")
        for r in a["false_merge_risks"][:5]:
            lines.append(f"    · {r['a'][:50]!r} vs {r['b'][:50]!r}")
    else:
        lines.append("  no cross-branch false-merge risk detected (winning-region items use distinct vocab)")
    return "\n".join(lines)
