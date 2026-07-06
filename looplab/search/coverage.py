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


def _theme(node) -> str:
    """The node's theme slug (lower-cased), or a stable placeholder for an unset theme so untitled
    ideas still count as ONE bucket rather than vanishing from the distribution."""
    idea = getattr(node, "idea", None)
    t = (getattr(idea, "theme", "") or "").strip().lower()
    return t or "(untitled)"


def normalized_entropy(counts) -> float:
    """Shannon entropy of a category distribution, normalized to [0,1] by log(#categories).
    1.0 = perfectly even spread across the observed categories; 0.0 = all mass on one (or <=1
    category). Deterministic; empty / single-category -> 0.0."""
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

    Keys:
      nodes                - ideas proposed so far (nodes that carry an idea)
      themes               - distinct theme slugs
      niches               - distinct parameter-niches among evaluated nodes (DiversityArchive)
      operators            - distinct operator kinds used
      theme_entropy        - in [0,1], 1 = spread across themes, 0 = single theme (broad<->narrow)
      dominant_theme_frac  - in [0,1], share of nodes on the most-explored theme (narrowing)
      recent_dominant_frac - in [0,1], same over the last `recent` nodes (is it narrowing NOW?)
      top_themes           - [[theme, count], ...] top few, so the LLM can name the concentration
    """
    nodes = [n for n in state.nodes.values() if getattr(n, "idea", None) is not None]
    if not nodes:
        return {"nodes": 0, "themes": 0, "niches": 0, "operators": 0,
                "theme_entropy": 0.0, "dominant_theme_frac": 0.0,
                "recent_dominant_frac": 0.0, "top_themes": []}
    themes = Counter(_theme(n) for n in nodes)
    ops = {getattr(n, "operator", None) for n in nodes}
    ops.discard(None)
    niches = len(DiversityArchive(resolution).build(state))
    dominant = themes.most_common(1)[0][1]
    recent_nodes = sorted(nodes, key=lambda n: n.id)[-max(1, recent):]
    recent_themes = Counter(_theme(n) for n in recent_nodes)
    recent_dom = recent_themes.most_common(1)[0][1]
    return {
        "nodes": len(nodes),
        "themes": len(themes),
        "niches": niches,
        "operators": len(ops),
        "theme_entropy": normalized_entropy(list(themes.values())),
        "dominant_theme_frac": round(dominant / len(nodes), 4),
        "recent_dominant_frac": round(recent_dom / len(recent_nodes), 4),
        "top_themes": [[t, c] for t, c in themes.most_common(3)],
    }
