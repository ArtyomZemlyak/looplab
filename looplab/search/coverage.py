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
from looplab.search.archive import DiversityArchive


def _node_theme(node):
    """A node's theme exactly as the canonical `theme_rollup` reads it (idea.theme, untitled -> None
    skipped). Kept identical so the coverage themes the Strategist sees match the digest's theme map
    and the /runs API — one theme vocabulary across every surface."""
    # REVIEW(2026-07-16): dead on new runs since Phase 0 (bd816a5) — the Researcher no longer authors
    # `idea.theme`, so this yields None for every fresh node and the recent-window narrowing signal
    # (`recent_dominant_frac`) is permanently 0. Needs the same concepts fallback as
    # events/digest.py::theme_rollup (see the REVIEW note there) so both surfaces keep one vocabulary.
    return getattr(getattr(node, "idea", None), "theme", None) or None


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

    The theme VOCABULARY (which themes / their counts) is the CANONICAL one
    (`events.digest.theme_rollup`), so the themes the Strategist reads here match the digest's theme
    map and the /runs API exactly. The dominant-* FRACTIONS are over ALL idea-carrying nodes (not
    just themed ones): an untitled idea is real effort NOT on a named theme, so it DILUTES the
    fraction — a mostly-untitled run correctly does not read as concentrated, and the fraction shares
    the same denominator as `_rule_novelty_stance`'s node-count trust guard.

    Keys:
      nodes                - ideas proposed so far (nodes that carry an idea)
      themes               - distinct themes (canonical theme_rollup vocabulary)
      niches               - distinct parameter-niches among evaluated nodes (DiversityArchive)
      operators            - distinct operator kinds used
      theme_entropy        - in [0,1], 1 = spread across themes, 0 = single theme (broad<->narrow)
      dominant_theme_frac  - in [0,1], share of ALL idea-nodes on the most-explored theme (narrowing)
      recent_dominant_frac - in [0,1], same over the last `recent` idea-nodes (narrowing NOW?)
      top_themes           - [[theme, count], ...] top few, so the LLM can name the concentration
    """
    nodes = [n for n in state.nodes.values() if getattr(n, "idea", None) is not None]
    if not nodes:
        return {"nodes": 0, "themes": 0, "niches": 0, "operators": 0,
                "theme_entropy": 0.0, "dominant_theme_frac": 0.0,
                "recent_dominant_frac": 0.0, "top_themes": []}
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
    recent_dom = Counter(t for t in (_node_theme(n) for n in recent_nodes) if t).most_common(1)
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
