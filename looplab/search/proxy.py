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


# ------------------------------------------------------- IS THE PROXY ANY GOOD (doc 52 row 31)
#
# This module KILLS: a candidate below the kill fraction is recorded `node_failed
# reason="proxy_skipped"` and never runs. The pre-execution judges the field ships measure
# themselves before they are trusted with that — Meta's research preference models report 0.684 ->
# 0.729, predict-before-execute 61.5 % pairwise, and Rehearse measured its judge decaying 82.8 ->
# 56.9 % late in a loop while remaining willing to decide. LoopLab's proxy had no accuracy number at
# all, on any run, which is the state a kill switch may not be in.
#
# `pairwise_accuracy` is that number, computed from the run's own folded record: over every PAIR of
# scored-and-then-evaluated nodes whose realized metrics differ, did the proxy order them the way
# the evaluation did? Pairwise rather than a correlation coefficient because ordering is what the
# kill actually uses, and because it is the measure the field's own numbers are quoted in.
#
# THE MEASUREMENT IS BIASED OPTIMISTIC AND SAYS SO. A node the proxy killed has no realized metric —
# that is what killing means — so it can never enter a pair, and the accuracy is computed over the
# survivors the proxy already approved. That is not a flaw to be corrected here (the counterfactual
# does not exist in the record); it is a caveat the report has to CARRY, because an accuracy quoted
# without it reads as "the kill is 78 % right" when what was measured is "the ordering among the
# ones it let through is 78 % right".
def pairwise_accuracy(state: RunState) -> dict:
    """How often the proxy ordered two candidates the way their evaluations later did.

    `{"scored": n, "evaluated": n, "pairs": n, "concordant": n, "tied_predictions": n,
      "accuracy": float | None, "killed": n, "killed_evaluated": n, "unscored": n}` — `accuracy` is
    None below one ordered pair, which is a different answer from 0.0 and must not render the same.
    """
    scored = {nid: value for nid, value in (state.proxy_scores or {}).items()
              if isinstance(value, (int, float))}
    killed = list(state.proxy_skipped or ())
    # The realized side: the same pool every other measurement in this repo counts — a tombstoned,
    # aborted or gate-flagged node's metric is not evidence about anything.
    realized = {}
    for nid in scored:
        node = (state.nodes or {}).get(nid)
        if node is None or node.metric is None or not node.feasible or node.tombstoned:
            continue
        if nid in state.aborted_nodes or nid in state.breed_excluded:
            continue
        realized[nid] = node.metric
    ids = sorted(realized)
    pairs = concordant = tied = 0
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            if realized[left] == realized[right]:
                continue          # a tie carries no ordering to be right or wrong about
            predicted = scored[left] - scored[right]
            if predicted == 0:
                # A PREDICTION tie is not a wrong ordering, it is no ordering — counting it as
                # discordant would report a proxy that predicts a constant (every candidate the
                # same k-NN mean, which happens with one neighbour) as 0 % accurate rather than as
                # what it is: a scorer offering nothing for the kill to rank by.
                tied += 1
                continue
            pairs += 1
            observed = realized[left] - realized[right]
            if predicted * observed > 0:
                concordant += 1
    return {
        "scored": len(scored),
        "evaluated": len(realized),
        "pairs": pairs,
        "concordant": concordant,
        "tied_predictions": tied,
        # None, never 0.0: "the proxy got none right" and "there was no pair to be right about"
        # are opposite facts, and this number is read by a person deciding whether to arm a kill.
        "accuracy": (concordant / pairs) if pairs else None,
        "killed": len(killed),
        # A killed node with a metric is the ONLY counterfactual the record can hold (a re-run, an
        # injected node, a salvage). Counted separately because it is the evidence that would make
        # the number unbiased, and there is normally none of it.
        "killed_evaluated": sum(1 for nid in killed
                                if nid in realized),
        # NOT "abstained": a node carries no proxy score for three different reasons — the proxy
        # was never consulted about it (a seed, or anything before `warmup`), it returned None for
        # want of numeric signal, or `should_skip` abstained beyond the support radius. The record
        # does not separate them, so the field is named for what it counts.
        "unscored": max(0, len(state.nodes or {}) - len(scored)),
    }
