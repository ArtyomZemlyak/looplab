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
from looplab.core.numeric import euclidean, knn_idw


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
        # Deliberately NOT digest.numeric_params: this variant also coerces numeric STRINGS
        # ("0.1" -> 0.1) via try/float, because generated solutions sometimes emit params as
        # strings and the proxy should still rank them. Do not "unify" the two.
        out = {}
        for key, v in (params or {}).items():
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def score(self, state: RunState, node: Node) -> Optional[float]:
        """The point estimate alone (`score_with_uncertainty` is the pair the kill decision reads)."""
        res = self.score_with_uncertainty(state, node)
        return None if res is None else res[0]

    def score_with_uncertainty(self, state: RunState, node: Node) -> Optional[tuple[float, float]]:
        """Inverse-distance-weighted k-NN prediction of `node`'s metric over evaluated BREEDABLE
        neighbours, WITH the distance to the nearest of them — the uncertainty the kill must
        respect (doc 51 §5). Returns None when there's no numeric signal to predict from (proxy
        abstains). `breedable_nodes` (not feasible_nodes) drops trust-gate cheaters so their
        inflated metric can't pull the prediction toward the cheated params (§2.2); a no-op
        under audit."""
        target = self._numeric(node.idea.params)
        neighbours = []
        for n in state.breedable_nodes():
            if n.id == node.id or n.metric is None:
                continue
            p = self._numeric(n.idea.params)
            keys = set(target) & set(p)
            if not keys:
                continue
            dist = euclidean(target, p, keys)
            neighbours.append((dist, n.metric))
        # Shared IDW core (exact param match -> its metric via the zero-distance short-circuit;
        # the pre-extraction `any(d==0)` + first-zero pick is the same sample after the sort).
        res = knn_idw(neighbours, self.k)
        return None if res is None else (res[0], res[1])

    def support_radius(self, state: RunState, node: Node) -> Optional[float]:
        """How far apart the evaluated points themselves are: the LARGEST leave-one-out
        nearest-neighbour distance among the breedable evaluated nodes, over the same per-pair key
        subspaces `score_with_uncertainty` measures in. None below two such nodes."""
        pts = []
        for n in state.breedable_nodes():
            if n.id == node.id or n.metric is None:
                continue
            p = self._numeric(n.idea.params)
            if p:
                pts.append(p)
        if len(pts) < 2:
            return None
        radii = []
        for i, a in enumerate(pts):
            best = None
            for j, b in enumerate(pts):
                if i == j:
                    continue
                keys = set(a) & set(b)
                if not keys:
                    continue
                d = euclidean(a, b, keys)
                if best is None or d < best:
                    best = d
            if best is not None:
                radii.append(best)
        return max(radii) if radii else None

    def abstains(self, state: RunState, node: Node, nearest: Optional[float]) -> bool:
        """The ABSTAIN band (doc 52 row 17): never skip a candidate the surrogate cannot see.
        A candidate whose nearest evaluated neighbour is farther than any evaluated point is from
        its own nearest sibling sits outside the explored region — its prediction is an
        extrapolation, and killing what the surrogate understands least is exactly backwards
        (`should_skip`'s own promise, "never skips when it would be the best", about a candidate
        it cannot place). Also abstains when there is no radius to compare against."""
        if nearest is None:
            return False                   # no distance handed in: the historical decision stands
        if nearest != nearest:
            return True                    # a NaN distance is no evidence at all
        if nearest <= 0.0:
            return False                   # an exact match is the best-seen case
        radius = self.support_radius(state, node)
        return radius is None or nearest > radius

    def should_skip(self, state: RunState, node: Node, predicted: float,
                    nearest: Optional[float] = None) -> bool:
        """Skip iff (a) past warmup, (b) kill_fraction > 0, (c) the candidate is INSIDE the explored
        region (`abstains` is False — a far candidate is never skipped on an extrapolated number),
        and (d) the predicted metric falls in the worst `kill_fraction` quantile of the evaluated
        metrics — i.e. the candidate is predicted to be in the doomed bottom fraction.
        Deterministic; never skips when it would be the best."""
        if self.kill_fraction <= 0.0:
            return False
        if self.abstains(state, node, nearest):
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
