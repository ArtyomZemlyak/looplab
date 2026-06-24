"""E2 · Researcher panel + empirical ranking (ADR-2). Generate K candidate ideas (the base Researcher
called K times -> K diverse proposals), then keep the one ranked best by a CHEAP EMPIRICAL surrogate
over the observed (params->metric) history — NOT an LLM-judge (verified: an LLM-as-judge is ~random
at ranking top vs bottom ideas). The panel widens exploration; the surrogate, not a debate, decides.

Wraps any Researcher behind the same Protocol. K=1 is a transparent pass-through. Bootstraps via the
first proposal until there's enough history to rank.
"""
from __future__ import annotations

import math
from typing import Optional

from .models import Idea, Node, RunState


def _predict(params: dict, hist: list[tuple[dict, float]], bounds, k: int = 3) -> Optional[float]:
    """Inverse-distance-weighted k-NN prediction of an idea's metric over the history (in the shared
    numeric param space). None when there's no comparable point."""
    target = {key: float(v) for key, v in params.items()
              if (not bounds or key in bounds) and isinstance(v, (int, float))}
    if not target:
        return None
    pts = []
    for p, m in hist:
        keys = set(target) & set(p)
        if not keys:
            continue
        pts.append((math.sqrt(sum((target[x] - p[x]) ** 2 for x in keys)), m))
    if not pts:
        return None
    pts.sort(key=lambda t: t[0])
    nn = pts[:k]
    if nn[0][0] == 0.0:
        return nn[0][1]
    w = sum(1.0 / d for d, _ in nn)
    return sum((1.0 / d) * m for d, m in nn) / w


class PanelResearcher:
    def __init__(self, base, k: int = 3, bounds=None, warmup: int = 3):
        self.base = base
        self.k = max(1, k)
        self.bounds = bounds if bounds is not None else getattr(base, "bounds", None)
        self.warmup = max(1, warmup)

    @property
    def space_hint(self) -> str:
        return getattr(self.base, "space_hint", "")

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        ideas = [self.base.propose(state, parent) for _ in range(self.k)]
        if self.k == 1:
            return ideas[0]
        hist = [({k: float(v) for k, v in n.idea.params.items() if isinstance(v, (int, float))},
                 n.metric) for n in state.feasible_nodes() if n.metric is not None]
        if len(hist) < self.warmup:
            return ideas[0]            # not enough signal to rank -> first proposal
        best, best_pred = None, None
        for idea in ideas:
            pred = _predict(idea.params, hist, self.bounds)
            if pred is None:
                continue
            if best_pred is None or state.is_better(pred, best_pred):
                best, best_pred = idea, pred
        if best is not None:
            best.rationale = (best.rationale + f" [panel: best of {self.k} by surrogate]").strip()
            return best
        return ideas[0]
