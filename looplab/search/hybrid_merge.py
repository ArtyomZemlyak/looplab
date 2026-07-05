"""Hybrid retrieval + agent-decided merge — the quality core shared by every place LoopLab used to
merge "similar" items on a single blind signal (an exact hash, or one cosine threshold).

Two pieces:
  * `HybridRetriever` — find near-duplicate CANDIDATES for a query across a corpus by fusing THREE
    complementary signals with Reciprocal Rank Fusion (RRF): lexical token-overlap (grep-like), BM25
    keyword scoring, and vector cosine. Each signal misses dups the others catch (cosine misses a
    rare-but-decisive shared token; BM25 misses a paraphrase; lexical misses a synonym) — fusing them
    maximizes candidate RECALL, which is what a merge step needs (precision is the agent's job).
  * `agent_merge` — hand the candidate group to an LLM (the Researcher) and let it make the FINAL
    call: which items are truly the same, and the single synthesized text to keep. A threshold can't
    tell "raise LR to 2e-3" from "raise LR to 3e-3" (different) apart from "increase the learning
    rate" / "use a higher LR" (same); the agent can.

Zero new dependencies: BM25 is ~30 lines here; embeddings reuse `tools.vectorstore` (hash_embed by
default, a real embedder when `embed_model` is set). Deterministic given the same embedder, so callers
on the engine's write-path stay replay-safe (the fold never calls this — see the engine merge passes).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Optional

from pydantic import BaseModel, Field

from looplab.core.parse import parse_structured
from looplab.tools.vectorstore import Vector, cosine, hash_embed

_TOK = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _TOK.findall((s or "").lower())


class BM25:
    """Okapi BM25 over a small in-memory corpus of pre-tokenized docs (no external dependency). Built
    once per corpus; `scores(query_tokens)` returns a per-doc relevance score (0 = no shared term)."""

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs_tokens
        self.N = len(docs_tokens)
        self.k1, self.b = k1, b
        self.avgdl = (sum(len(d) for d in docs_tokens) / self.N) if self.N else 0.0
        self.df: Counter = Counter()
        for d in docs_tokens:
            for t in set(d):
                self.df[t] += 1

    def scores(self, query_tokens: list[str]) -> list[float]:
        out = [0.0] * self.N
        if not self.avgdl:
            return out
        q = set(query_tokens)
        for i, d in enumerate(self.docs):
            if not d:
                continue
            tf = Counter(d)
            dl = len(d)
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out[i] = s
        return out


def _rank(scores: list[float]) -> dict[int, int]:
    """Map doc-index -> 0-based rank by DESCENDING score (stable on ties). Only ranks docs with a
    positive score; the rest are absent (they contribute nothing to RRF, as if ranked at infinity)."""
    order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], i))
    return {idx: r for r, idx in enumerate(order)}


class HybridRetriever:
    """Fuse lexical + BM25 + vector retrieval over a fixed corpus of texts. `candidates(query)` returns
    the top items by RRF-fused rank across the three signals."""

    def __init__(self, corpus: list[str], embed: Optional[Callable[[str], Vector]] = None):
        self.corpus = list(corpus)
        self._toks = [_tokens(c) for c in self.corpus]
        self._tsets = [set(t) for t in self._toks]
        self._bm25 = BM25(self._toks)
        self._embed = embed or hash_embed
        self._vecs = [self._embed(c) for c in self.corpus]

    def candidates(self, query: str, k: int = 8, *, rrf_k: int = 60,
                   exclude: Optional[int] = None) -> list[tuple[int, float]]:
        """Top-k (corpus_index, fused_score), best first. `rrf_k` is the standard RRF damping constant
        (60 in the original paper). `exclude` drops one index (e.g. the query's own row when the query
        IS a corpus member). Fused score is unitless — use it only to RANK, not threshold."""
        if not self.corpus:
            return []
        qt = _tokens(query)
        qset = set(qt)
        lex = [(len(qset & ts) / len(qset | ts)) if (qset or ts) else 0.0 for ts in self._tsets]
        bm = self._bm25.scores(qt)
        qv = self._embed(query)
        vec = [cosine(qv, v) for v in self._vecs]
        ranks = [_rank(lex), _rank(bm), _rank(vec)]
        fused: dict[int, float] = {}
        for i in range(len(self.corpus)):
            if i == exclude:
                continue
            score = sum(1.0 / (rrf_k + r[i]) for r in ranks if i in r)
            if score > 0:
                fused[i] = score
        return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:k]


def cluster_near_duplicates(texts: list[str], *, embed: Optional[Callable[[str], Vector]] = None,
                            k: int = 6) -> list[list[int]]:
    """Group corpus indices into candidate near-duplicate CLUSTERS using hybrid retrieval as the
    adjacency signal (a connected-components pass over each item's top-k hybrid neighbours). Recall-
    oriented on purpose: a cluster is a SUGGESTION for the agent to adjudicate, not a final merge.
    Singletons are returned as 1-element clusters so the caller can iterate uniformly."""
    n = len(texts)
    if n <= 1:
        return [[i] for i in range(n)]
    r = HybridRetriever(texts, embed=embed)
    # Adjacency: i ~ j if j is in i's top-k hybrid neighbours OR vice versa (symmetrize for stability).
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j, _s in r.candidates(texts[i], k=k, exclude=i):
            adj[i].add(j)
            adj[j].add(i)
    seen: set[int] = set()
    clusters: list[list[int]] = []
    for i in range(n):
        if i in seen:
            continue
        stack, comp = [i], []
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            comp.append(u)
            stack.extend(adj[u] - seen)
        clusters.append(sorted(comp))
    return clusters


# --------------------------------------------------------------------------- agent-decided merge
class _MergeGroup(BaseModel):
    members: list[int] = Field(default_factory=list)   # indices (into the presented list) that ARE the same
    merged: str = ""                                    # the single synthesized text to keep for the group


class _MergePlan(BaseModel):
    groups: list[_MergeGroup] = Field(default_factory=list)


_MERGE_SYSTEM = (
    "You are a meticulous research librarian. You are shown a small set of candidate {kind} that a "
    "retriever flagged as POSSIBLY duplicates. Decide which are TRULY the same underlying "
    "idea/claim/lesson and which are genuinely DIFFERENT — a paraphrase ('use a higher learning rate' "
    "vs 'increase the LR') is the SAME; a different value or mechanism ('LR 2e-3' vs 'LR 3e-3', "
    "'dropout' vs 'weight decay') is DIFFERENT and must NOT be merged. For each group of ≥2 that are "
    "the same, call `emit` with `groups`: each has `members` (the 0-based indices that are the same) "
    "and `merged` (ONE clear sentence that preserves every specific detail — thresholds, numbers, "
    "caveats — from the merged items). Do NOT include singletons; anything you omit is kept as-is. "
    "When in doubt, DON'T merge (a wrong merge loses information).")


def agent_merge(client, items: list[str], *, kind: str = "items", goal: str = "",
                parser: str = "tool_call", prompts=None) -> list[dict]:
    """Let the agent make the FINAL merge decision over `items` (a candidate near-duplicate cluster
    surfaced by hybrid retrieval). Returns a partition as a list of groups
    `[{"members": [i, ...], "merged": <text>}]` covering EVERY index EXACTLY once — genuinely-merged
    groups carry the agent's synthesized text; everything else comes back as a singleton whose
    `merged` is its own original text. Fail-open: no client / <2 items / any error -> all singletons
    (nothing merged, no information lost). Never raises."""
    n = len(items)
    singletons = [{"members": [i], "merged": items[i]} for i in range(n)]
    if client is None or n < 2:
        return singletons
    try:
        from looplab.core.prompts import render
        blocks = "\n".join(f"[{i}] {c[:600]}" for i, c in enumerate(items))
        sysmsg = render(prompts, "merge_system", _MERGE_SYSTEM).replace("{kind}", kind)
        user = ((f"Goal context: {goal}\n\n" if goal else "")
                + f"Candidate {kind} (decide which indices are the SAME):\n" + blocks)
        plan = parse_structured(client, [{"role": "system", "content": sysmsg},
                                         {"role": "user", "content": user}], _MergePlan, parser or "tool_call")
    except Exception:  # noqa: BLE001 — advisory: a merge failure must never lose or corrupt data
        return singletons
    # Rebuild a clean partition: honor only VALID, DISJOINT groups of >=2; every unclaimed index stays
    # a singleton. This repairs a model that double-claims an index, references out-of-range, or drops
    # some — so downstream never sees an item vanish or appear twice.
    claimed: set[int] = set()
    out: list[dict] = []
    for g in (plan.groups or []):
        members = [i for i in (g.members or []) if isinstance(i, int) and 0 <= i < n and i not in claimed]
        members = sorted(set(members))
        if len(members) < 2:
            continue                                    # a "group" of 0/1 isn't a merge
        merged = (g.merged or "").strip() or items[members[0]]
        claimed.update(members)
        out.append({"members": members, "merged": merged})
    out.extend({"members": [i], "merged": items[i]} for i in range(n) if i not in claimed)
    out.sort(key=lambda grp: grp["members"][0])
    return out


def consolidate(texts: list[str], client=None, *, kind: str = "items",
                embed: Optional[Callable[[str], Vector]] = None, cluster_k: int = 6,
                goal: str = "") -> list[dict]:
    """One-call hybrid + agent consolidation over `texts`. Hybrid-cluster candidate near-duplicates
    (recall), then the agent adjudicates each multi-item cluster (precision + synthesis). Returns
    groups `[{"members": [orig_idx, ...], "merged": <text>}]` covering EVERY index exactly once.

    No client (or <2 texts) -> every item is its own singleton: we NEVER merge on the blind retrieval
    signal alone — the agent is the decider — so an offline/degraded run loses nothing. Callers keep
    any deterministic exact-dedup as a base and layer this on top for paraphrase-level merges."""
    n = len(texts)
    if n <= 1 or client is None:
        return [{"members": [i], "merged": texts[i]} for i in range(n)]
    out: list[dict] = []
    for cl in cluster_near_duplicates(texts, embed=embed, k=cluster_k):
        if len(cl) < 2:
            out.append({"members": list(cl), "merged": texts[cl[0]]})
            continue
        for g in agent_merge(client, [texts[i] for i in cl], kind=kind, goal=goal):
            out.append({"members": [cl[j] for j in g["members"]], "merged": g["merged"]})
    out.sort(key=lambda grp: grp["members"][0])
    return out
