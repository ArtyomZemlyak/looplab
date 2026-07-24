"""Diversity archive (I22, quality-diversity). Buckets evaluated solutions into niches
by their (discretized) parameters and keeps the best per niche — so the run records a
*diverse* set of elites, not just the single best. Pure over RunState; recorded at run
end as provenance and queryable for seeding exploration.
"""
from __future__ import annotations

import math

from looplab.core.models import Node, RunState
from looplab.search.policy import rank_by_metric


class DiversityArchive:
    def __init__(self, resolution: float = 1.0):
        self.resolution = resolution  # niche bucket width in parameter space

    def _niche(self, params: dict) -> tuple:
        r = self.resolution or 1.0

        def _bucket(v):
            # Discretize only a FINITE numeric coordinate. `round(v / r)` on a non-finite value —
            # inf/NaN (a `1e309` param JSON-folds straight to inf; NaN is agent-supplied) or a huge int
            # whose float conversion overflows — raises (OverflowError/ValueError) and, because `build`
            # only skips aborted/null-metric/infeasible nodes (never guards the param VALUES), that crash
            # reaches the main run loop at the default-on coverage cadence and aborts the whole run. Every
            # other param consumer already guards this (digest.numeric_params, operators.merge_idea, the
            # surrogate). Bucket a non-bucketable coordinate under a stable token so the degenerate node
            # gets its own niche in the diversity/audit view instead of crashing.
            try:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    q = v / r
                    if math.isfinite(q):
                        return round(q)
            except (OverflowError, ValueError, TypeError, ZeroDivisionError):
                pass
            return f"\x00nonbucketable:{v!r}"

        return tuple(sorted((k, _bucket(v)) for k, v in params.items()))

    def build(self, state: RunState) -> dict[tuple, Node]:
        """niche-key -> best Node in that niche."""
        better = (lambda a, b: a < b) if state.direction == "min" else (lambda a, b: a > b)
        elites: dict[tuple, Node] = {}
        aborted = set(getattr(state, "aborted_nodes", None) or [])
        for n in state.evaluated_nodes():
            # skip constraint-violating (and null-metric) nodes: an infeasible node must not become the
            # recorded niche elite (run-end provenance) or inflate the coverage count. (None<float would
            # also crash the `better` compare.) NOTE: this is the run-end DIVERSITY/AUDIT view, so it
            # deliberately KEEPS trust-gate-flagged-but-feasible nodes (they stay `feasible` for exactly
            # this diversity/audit picture) — unlike the breeding pools, which use breedable_nodes().
            # evaluated_nodes preserves aborted attempts for audit compatibility; the current
            # diversity archive must share the selector's lifecycle boundary and exclude them explicitly.
            if n.id in aborted or n.metric is None or not n.feasible:
                continue
            niche = self._niche(n.idea.params)
            cur = elites.get(niche)
            if cur is None or better(n.metric, cur.metric):
                elites[niche] = n
        return elites

    def summary(self, state: RunState) -> dict:
        elites = self.build(state)
        ranked = rank_by_metric(state, elites.values())
        return {
            "resolution": self.resolution,
            "niches": len(elites),
            "elites": [{"node_id": n.id, "metric": n.metric, "params": n.idea.params}
                       for n in ranked],
        }
