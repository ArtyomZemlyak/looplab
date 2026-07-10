"""A6 · Proxy / predictive scoring (ADR-2). Cheaply RANK a candidate's potential from early-stage
signals so doomed candidates can be killed before a full eval — the cost lever that separates the
MLE-bench leaders (KompeteAI predictive scoring = 6.9x faster eval; ArchPilot proxy-guided MCTS).

The current eval contract is atomic (no partial-epoch hook), so the default proxy is a *surrogate
over the observed `(params -> metric)` history*: a k-NN-in-param-space prediction of a candidate's
metric. It is a pure function of the folded `RunState` + the candidate's params, so the skip decision
is deterministic and replay-safe (a skipped node is recorded as `node_failed reason="proxy_skipped"`
and reconstructed by `fold`; the proxy is never re-run on replay). When a richer eval contract exposes
a first-epoch/partial-data signal, `ProxyScorer.score` is the single seam to upgrade.

OFF by default (`proxy_kill_fraction=0.0` -> never skips): no behavior change.
"""
from __future__ import annotations

import math
from typing import Optional

from looplab.core.models import Node, RunState


class ProxyScorer:
    """Predict a candidate's metric from the nearest evaluated neighbours in parameter space and
    skip the bottom `kill_fraction` predicted to be doomed. `warmup` evaluated nodes are required
    before any skip (so the surrogate has signal and a baseline always survives)."""

    def __init__(self, kill_fraction: float = 0.0, warmup: int = 4, k: int = 3):
        self.kill_fraction = max(0.0, min(0.9, kill_fraction))
        self.warmup = max(1, warmup)
        self.k = max(1, k)

    @staticmethod
    def _numeric(params: dict) -> dict:
        out = {}
        for key, v in (params or {}).items():
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def score(self, state: RunState, node: Node) -> Optional[float]:
        """Inverse-distance-weighted k-NN prediction of `node`'s metric over evaluated BREEDABLE
        neighbours. Returns None when there's no numeric signal to predict from (proxy abstains).
        `breedable_nodes` (not feasible_nodes) drops trust-gate cheaters so their inflated metric
        can't pull the prediction toward the cheated params (§2.2); a no-op under audit."""
        target = self._numeric(node.idea.params)
        neighbours = []
        for n in state.breedable_nodes():
            if n.id == node.id or n.metric is None:
                continue
            p = self._numeric(n.idea.params)
            keys = set(target) & set(p)
            if not keys:
                continue
            dist = math.sqrt(sum((target[key] - p[key]) ** 2 for key in keys))
            neighbours.append((dist, n.metric))
        if not neighbours:
            return None
        neighbours.sort(key=lambda t: t[0])
        nn = neighbours[: self.k]
        if any(d == 0.0 for d, _ in nn):                  # exact param match -> its metric
            return next(m for d, m in nn if d == 0.0)
        wsum = sum(1.0 / d for d, _ in nn)
        return sum((1.0 / d) * m for d, m in nn) / wsum

    def should_skip(self, state: RunState, node: Node, predicted: float) -> bool:
        """Skip iff (a) past warmup, (b) kill_fraction > 0, and (c) the predicted metric falls in the
        worst `kill_fraction` quantile of the evaluated metrics — i.e. the candidate is predicted to
        be in the doomed bottom fraction. Deterministic; never skips when it would be the best."""
        if self.kill_fraction <= 0.0:
            return False
        # breedable (not feasible): a trust-gate cheater's inflated metric must not raise the kill
        # threshold and get honest candidates skipped as "doomed bottom fraction" (§2.2); no-op on audit.
        metrics = sorted(
            (n.metric for n in state.breedable_nodes() if n.metric is not None),
            reverse=(state.direction == "max"))   # best-first
        if len(metrics) < self.warmup:
            return False
        # boundary separating the top (1 - kill_fraction) from the doomed bottom kill_fraction
        idx = max(0, min(len(metrics) - 1,
                         int(math.ceil((1.0 - self.kill_fraction) * len(metrics))) - 1))
        threshold = metrics[idx]
        # skip only if the predicted metric is strictly WORSE than the boundary
        if predicted == threshold:
            return False
        return state.is_better(threshold, predicted)
