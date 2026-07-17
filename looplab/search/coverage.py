"""Coverage / diversity read-model — the run's BREADTH signal (extends the I22 diversity archive).

A PURE, fold-derived summary of HOW BROADLY the run has explored the hypothesis space so far:
distinct themes and parameter-niches, operator spread, and two narrowing SCALARS — the normalized
theme entropy (1 = spread evenly across themes, 0 = everything on one theme) and the dominant-theme
fraction (how concentrated the run is on its single most-explored theme). It is *context*, never a
decision: the Strategist reads it to decide how much novelty pressure to apply (the LLM decides —
this only informs it), and it is recorded at the strategist cadence as a `coverage_snapshot`
sidecar event so the run's narrowing curve is queryable over the log and replayable historically.

No I/O, no embeddings, no wall-clock — deterministic over `RunState`, so a replay recomputes it
byte-identically and a historical log can be re-measured offline (fold -> coverage_signal). This is
why the signal deliberately uses only structural facts (themes/params/operators the fold already
holds) and NOT embedding similarity, which would need a model call and break replay determinism.

Motivation: current research agents tend to NARROW rather than broaden the hypotheses they explore
(arXiv 2605.27905). LoopLab's default path is seed-then-hill-climb whose only breadth signal to the
meta-controller was metric STAGNATION (`improves_since_best`) — blind to coverage collapse. This
gives the controller eyes on the space it is (or isn't) covering.
"""
from __future__ import annotations

import math
from collections import Counter

from looplab.core.models import RunState
from looplab.events.digest import node_theme
from looplab.search.archive import DiversityArchive


def _node_theme(node):
    """A node's theme exactly as the canonical `theme_rollup` reads it — delegated to the ONE shared
    derivation (`events.digest.node_theme`: legacy `idea.theme`, else the first concept's axis) so the
    coverage themes the Strategist sees match the digest's theme map and the /runs API. Phase 0
    (bd816a5) moved authoring from `theme` to `concepts`; sharing the helper keeps the fallback identical
    across surfaces instead of drifting."""
    return node_theme(node)


def normalized_entropy(counts) -> float:
    """Shannon entropy of a category distribution, normalized to [0,1] by log(#NON-EMPTY categories).
    1.0 = perfectly even spread across the observed (non-zero) categories; 0.0 = all mass on one
    (or <=1 non-empty category). Empty bins are dropped, not treated as categories — so this measures
    concentration among the themes ACTUALLY tried. Deterministic; empty / single-category -> 0.0."""
    total = sum(counts)
    cats = [c for c in counts if c > 0]
    if total <= 0 or len(cats) <= 1:
        return 0.0
    h = -sum((c / total) * math.log(c / total) for c in cats)
    return round(h / math.log(len(cats)), 4)


def coverage_signal(state: RunState, *, resolution: float = 1.0, recent: int = 4) -> dict:
    """Compact, deterministic breadth read over the folded run (see module docstring). Every key is
    additive/reader-defaulted; an empty run yields zeros. `recent` bounds the recency window for the
    trailing "narrowing NOW?" signal.

    PART V Phase 6a: the breadth VOCABULARY is now the folded CONCEPT AXES (`events.digest.theme_rollup`
    → `node_axes`, MULTI-membership: a node is counted under every axis it occupies, post-rename), so
    the breadth the Strategist reads matches the /concepts map and the /runs API exactly and no longer
    depends on the Researcher's first-concept AUTHORING ORDER. The dict keys keep the historical
    `theme_*` names (a wire contract the Strategist prompt / proposal_cues / UI string-match) — only the
    derivation moved. The dominant-* FRACTIONS are over ALL idea-carrying nodes (not just tagged ones):
    an untagged idea is real effort on NO axis, so it DILUTES the fraction — a mostly-untagged run
    correctly does not read as concentrated, and the fraction shares the same denominator as
    `_rule_novelty_stance`'s node-count trust guard. Because axes multi-count, `themes` and the counts
    can exceed the node total; the FRACTIONS stay in [0,1] (a single axis appears at most once per node).

    Keys:
      nodes                - ideas proposed so far (nodes that carry an idea)
      themes               - distinct concept AXES occupied (canonical theme_rollup vocabulary)
      niches               - distinct parameter-niches among evaluated nodes (DiversityArchive)
      operators            - distinct operator kinds used
      theme_entropy        - in [0,1], 1 = spread across axes, 0 = single axis (broad<->narrow)
      dominant_theme_frac  - in [0,1], share of ALL idea-nodes touching the most-explored axis (narrowing)
      recent_dominant_frac - in [0,1], same over the last `recent` idea-nodes (narrowing NOW?)
      top_themes           - [[axis, count], ...] top few, so the LLM can name the concentration
    """
    nodes = [n for n in state.nodes.values() if getattr(n, "idea", None) is not None]
    if not nodes:
        return {"nodes": 0, "themes": 0, "niches": 0, "operators": 0,
                "theme_entropy": 0.0, "dominant_theme_frac": 0.0,
                "recent_dominant_frac": 0.0, "top_themes": []}
    # REVIEW(2026-07-16): this deliberately-local import (and its "keep `search` import-time free of
    # digest" rationale) is now contradicted three screens up — eba3cc6 added a MODULE-level
    # `from looplab.events.digest import node_theme`, so `search.coverage` imports digest at import
    # time anyway (cycle-safe: digest only imports core). Either both imports go top-level and this
    # stale load-bearing comment is dropped, or the constraint is real and node_theme's import must
    # become local too — as written, the comment documents a rule the module no longer follows.
    from looplab.events.digest import theme_rollup     # local: keep `search` import-time free of digest
    rollup = theme_rollup(state)                        # {theme: {count, best_metric}} — canonical
    counts = [v["count"] for v in rollup.values()]
    ops = {getattr(n, "operator", None) for n in nodes}
    ops.discard(None)
    niches = len(DiversityArchive(resolution).build(state))
    dominant = max(counts) if counts else 0
    # Recency window: the last `recent` NODES (by id) — then how concentrated their THEMES are. Taking
    # nodes-first (not themed-first) keeps this a genuine "narrowing NOW?" measure: a window of fresh
    # UNTITLED drafts reads as un-concentrated (0.0), not as the dominant OLD theme reaching forward.
    recent_nodes = sorted(nodes, key=lambda n: n.id)[-max(1, recent):]
    from looplab.events.digest import node_axes         # local: Phase 6a multi-membership over folded axes
    recent_axes: Counter = Counter()
    for n in recent_nodes:
        for a in node_axes(state, n):                   # count a recent node under EVERY axis it touches
            recent_axes[a] += 1
    recent_dom = recent_axes.most_common(1)
    recent_dom_count = recent_dom[0][1] if recent_dom else 0
    top = sorted(rollup.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:3]
    return {
        "nodes": len(nodes),
        "themes": len(rollup),
        "niches": niches,
        "operators": len(ops),
        "theme_entropy": normalized_entropy(counts),
        "dominant_theme_frac": round(dominant / len(nodes), 4),
        "recent_dominant_frac": round(recent_dom_count / len(recent_nodes), 4),
        "top_themes": [[t, v["count"]] for t, v in top],
    }
