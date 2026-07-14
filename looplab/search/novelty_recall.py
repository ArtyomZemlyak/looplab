"""Novelty-gate RECALL diagnostic — how many TRUE paraphrases slipped through the gate (§21.12 E3).

The live novelty gate is LLM-based (`engine/novelty.py::_llm_novelty_gate`), but the `rubertlite` audit
found it fired only ~5 times across 107 hypotheses + 68 nodes — so a natural question is its RECALL: of the
near-duplicate proposals that are genuinely the SAME experiment (a paraphrase, not a legitimate variant),
how many did the gate catch, and how many LEAKED through as separate executed nodes (wasted compute — the
"сколько шлака" question)?

This is an OFFLINE diagnostic (Phase 0/1 lane): it reads a completed run and reports; it writes no events
and never touches selection. It composes the PART IV keystones rather than adding infrastructure:
  * `hybrid_merge.cluster_near_duplicates` (RRF: lexical + BM25 + vector) surfaces candidate near-duplicate
    pairs among the created nodes' idea texts — the same recall-oriented retriever the board dedup uses;
  * the §12-style LLM adjudicator decides, per candidate pair, PARAPHRASE (same idea) vs VARIANT (a
    legitimately different experiment — `t=0.02` vs `t=0.05` is a variant, not a dup);
  * the gate's own audit trail (`RunState.novelty_events`) counts what it CAUGHT.

recall = caught / (caught + leaked). LEAKS are the actionable output: true-paraphrase pairs the gate let
BOTH through. Deterministic fallback (no client): report the candidate clusters only (no paraphrase/variant
adjudication — that needs the LLM). Fail-open throughout; never raises.
"""
from __future__ import annotations

from itertools import combinations
from typing import Optional

from looplab.core.models import RunState
from looplab.search.concept_graph import _experiment_nodes, _node_text


def _idea_texts(state: RunState) -> tuple[list[int], list[str]]:
    """(node ids, idea texts) for every idea-carrying node, in id order — the proposals that PASSED the
    gate (a rejected proposal never became a node)."""
    nodes = _experiment_nodes(state)
    return [n.id for n in nodes], [_node_text(n) for n in nodes]


def paraphrase_leaks(state: RunState, *, client=None, embed=None, parser: str = "tool_call",
                     max_pairs: int = 60) -> dict:
    """Measure the novelty gate's leakage. Returns:

      n_nodes          - idea-carrying experiments considered
      caught           - proposals the gate REJECTED as near-duplicates (RunState.novelty_events)
      candidate_pairs  - near-duplicate node pairs surfaced by hybrid retrieval (pre-adjudication)
      leaks            - [{a, b, reason}] candidate pairs the adjudicator judged TRUE paraphrases that
                         BOTH executed (the gate missed) — empty without a client (no adjudication)
      recall           - caught / (caught + len(leaks)); None when there's nothing to judge
      adjudicated      - whether the LLM pass ran (False => candidate_pairs are unjudged suggestions)

    Pure except the optional LLM adjudication; best-effort (any error degrades gracefully)."""
    ids, texts = _idea_texts(state)
    caught = len(state.novelty_events)
    out = {"n_nodes": len(ids), "caught": caught, "candidate_pairs": [], "leaks": [],
           "recall": None, "adjudicated": False}
    if len(ids) < 2:
        return out
    try:
        from looplab.search.hybrid_merge import cluster_near_duplicates
        clusters = cluster_near_duplicates(texts, embed=embed)
    except Exception:  # noqa: BLE001 — retriever hiccup => nothing to report, never crash
        return out
    # Candidate pairs = every within-cluster pair (a cluster is a SUGGESTION to adjudicate, §hybrid_merge).
    # Recall-oriented clustering yields MANY candidates on a same-family run (a DCL run pairs freely), so
    # sort by a cheap lexical similarity (token Jaccard) DESC and adjudicate the most-similar first — the
    # `max_pairs` LLM budget then covers the likeliest paraphrases, not an arbitrary slice.
    def _toks(s: str) -> set:
        return set(w for w in s.split() if len(w) >= 4)
    tokset = {i: _toks(texts[i]) for i in range(len(texts))}

    def _jac(i: int, j: int) -> float:
        a, b = tokset[i], tokset[j]
        return len(a & b) / len(a | b) if (a or b) else 0.0

    idx = {nid: k for k, nid in enumerate(ids)}
    pairs: list[tuple[int, int]] = []
    for cl in clusters:
        if len(cl) >= 2:
            for i, j in combinations(sorted(cl), 2):
                pairs.append((ids[i], ids[j]))
    pairs.sort(key=lambda p: -_jac(idx[p[0]], idx[p[1]]))
    out["candidate_pairs"] = pairs
    if client is None or not pairs:
        return out

    # LLM adjudication: PARAPHRASE (same experiment, a dup the gate should have caught) vs VARIANT.
    from pydantic import BaseModel

    from looplab.core.parse import parse_structured
    by_id = {i: t for i, t in zip(ids, texts)}

    class _V(BaseModel):
        is_paraphrase: bool = False
        reason: str = ""

    system = (
        "You judge whether TWO machine-learning experiments are the SAME idea (a PARAPHRASE the novelty gate "
        "should have deduplicated) or a LEGITIMATE VARIANT worth running separately. A different "
        "hyperparameter VALUE (temperature 0.02 vs 0.05), a different loss/architecture, or an added "
        "component is a VARIANT — NOT a paraphrase. Only call it a paraphrase when they propose the SAME "
        "change with no material difference (same method, same values, only reworded). Call `emit` with "
        "`is_paraphrase` and a one-line `reason`."
    )
    leaks: list[dict] = []
    for a, b in pairs[:max_pairs]:
        try:
            v = parse_structured(client, [
                {"role": "system", "content": system},
                {"role": "user", "content": f"A (node {a}): {by_id.get(a, '')[:500]}\n\n"
                                            f"B (node {b}): {by_id.get(b, '')[:500]}"}], _V, parser)
        except Exception:  # noqa: BLE001 — skip a bad adjudication, keep going
            continue
        if getattr(v, "is_paraphrase", False):
            leaks.append({"a": a, "b": b, "reason": (v.reason or "").strip()[:160]})
    out["leaks"] = leaks
    out["adjudicated"] = True
    out["adjudicated_count"] = min(len(pairs), max_pairs)   # how many of candidate_pairs were LLM-judged
    denom = caught + len(leaks)
    # Recall over the ADJUDICATED (most-similar) pairs — a bounded estimate; leaks in the un-judged tail
    # would only LOWER it, so this is an UPPER bound on recall (a lower bound on leakage).
    out["recall"] = round(caught / denom, 3) if denom else None
    return out


def novelty_recall_report(state: RunState, **kwargs) -> str:
    """Compact text diagnostic for the CLI."""
    r = paraphrase_leaks(state, **kwargs)
    lines = [f"Novelty-gate recall  ({r['n_nodes']} experiments, gate caught {r['caught']} near-dups)"]
    if not r["adjudicated"]:
        lines.append(f"  {len(r['candidate_pairs'])} candidate near-duplicate pairs (unjudged — pass a "
                     "client / drop --offline for the paraphrase-vs-variant adjudication).")
        return "\n".join(lines)
    leaks = r["leaks"]
    rec = r["recall"]
    adj = r.get("adjudicated_count", 0)
    lines.append(f"  candidate near-dup pairs: {len(r['candidate_pairs'])}  "
                 f"(adjudicated the {adj} most-similar)   TRUE paraphrases that LEAKED: {len(leaks)}")
    if rec is not None:
        lines.append(f"  estimated gate recall: {rec}  (caught {r['caught']} / "
                     f"{r['caught'] + len(leaks)} true dups among adjudicated) — upper bound")
    for lk in leaks[:8]:
        lines.append(f"    · leak: node {lk['a']} ≈ node {lk['b']} — {lk['reason']}")
    if not leaks:
        lines.append("  no leaked paraphrases found — the gate's recall looks healthy on this run.")
    return "\n".join(lines)
