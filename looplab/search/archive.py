"""Diversity archive (I22, quality-diversity). Buckets evaluated solutions into niches
by their (discretized) parameters and keeps the best per niche — so the run records a
*diverse* set of elites, not just the single best. Pure over RunState; recorded at run
end as provenance and queryable for seeding exploration.
"""
from __future__ import annotations

from looplab.core.models import Node, RunState


class DiversityArchive:
    def __init__(self, resolution: float = 1.0):
        self.resolution = resolution  # niche bucket width in parameter space

    def _niche(self, params: dict) -> tuple:
        r = self.resolution or 1.0
        return tuple(sorted((k, round(v / r)) for k, v in params.items()))

    def build(self, state: RunState) -> dict[tuple, Node]:
        """niche-key -> best Node in that niche."""
        better = (lambda a, b: a < b) if state.direction == "min" else (lambda a, b: a > b)
        elites: dict[tuple, Node] = {}
        for n in state.evaluated_nodes():
            if n.metric is None:   # a hand-edited/BYO node_evaluated can carry metric=null; None<float crashes
                continue
            niche = self._niche(n.idea.params)
            cur = elites.get(niche)
            if cur is None or better(n.metric, cur.metric):
                elites[niche] = n
        return elites

    def summary(self, state: RunState) -> dict:
        elites = self.build(state)
        ranked = sorted(elites.values(),
                        key=lambda n: (n.metric, n.id),
                        reverse=(state.direction == "max"))
        return {
            "resolution": self.resolution,
            "niches": len(elites),
            "elites": [{"node_id": n.id, "metric": n.metric, "params": n.idea.params}
                       for n in ranked],
        }
