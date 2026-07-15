"""SearchFitness — THE single owner of the run's ordering / eligibility decision (R1/R4 "one fitness
owner", docs/17 §6.5/§6.6).

Before this, the "who is better / who is eligible / how to rank" logic was copy-inlined at ~half a dozen
sites — the fold's best-selection post-pass (`events/replay.py::_select_best`), the search policies'
`rank_by_metric`, `engine/holdout.py::holdout_topk`, and `RunState.is_better` — where the copies could
drift (docs/16's "search/promotion fitness split"). This object owns them ONCE, so a later scored term (a
capability-expansion operator's fitness, a calibrated-verifier tie-break, a cost-aware reward) is composed
in exactly ONE place instead of edited across every ranker. That single seam is the R1/SearchFitness gate
Part IV's deferred selection pieces wait on (§21.13/§21.16).

Layering: lives in `core` — duck-typed on nodes (reads only `.metric` / `.id` / `.robust_metric` /
`.holdout_metric` / `.feasible`, plus `.verifier_score` on the R1-c tie-break path) and constructed from
a `direction` string, importing nothing from `core.models` — so BOTH the fold (`events/replay.py`, which
may import only `core`) and the search policies can route through it with no layering violation and no
import cycle.

Purity: it reads, it never mutates or does I/O, so it is safe to call inside the deterministic fold
(invariant #5). Behavior is BYTE-IDENTICAL to the inlined logic it replaces — this is a refactor seam, not
a policy change; the regression is locked by `tests/test_events_replay.py` + the golden replay fixture.
"""
from __future__ import annotations


def is_better(direction: str, a: float, b: float) -> bool:
    """Direction-aware strict improvement: lower wins when minimizing, higher when maximizing. THE
    comparator — `RunState.is_better` delegates here so there is exactly one spelling of "better"."""
    return a < b if direction == "min" else a > b


# R1-c: the neutral verifier "score" for an unscored node (the §12 verifier's own `unclear` midpoint).
# In a metric-tie, a node scored ABOVE this beats an unscored node and one scored BELOW loses to it —
# so an unverified contender is treated as "no signal", neither promoted nor penalized past the midpoint.
_NEUTRAL_VERIFIER = 0.5


class SearchFitness:
    """The run's ordering owner, built from its optimize `direction`. Stateless beyond `direction`
    (+ the R1-c `verifier_tiebreak` flag); construct one per fold / policy call (cheap)."""

    def __init__(self, direction: str, *, verifier_tiebreak: bool = False):
        self.direction = direction
        self._reverse = (direction == "max")     # best-first ordering for `sorted(..., reverse=)`
        # R1-c: when on, the mean-pick key gains a calibrated-verifier tie-break slot BETWEEN the metric
        # and the id, oriented by direction so a HIGHER verifier score always wins a metric-tie under the
        # run's chooser (max picks the larger key, min the smaller). Off -> plain (robust_metric, id).
        self._verifier_tiebreak = bool(verifier_tiebreak)
        self._vsign = 1.0 if direction == "max" else -1.0

    # --- comparator -------------------------------------------------------------------------------
    def is_better(self, a: float, b: float) -> bool:
        return is_better(self.direction, a, b)

    def best(self, nodes, key):
        """The single best node under `key` (deterministic argmin/argmax over the run's direction).
        `key` MUST place the node id as its final tie-break component so equal-metric ties resolve
        deterministically (hash-seed independent)."""
        chooser = min if self.direction == "min" else max
        return chooser(nodes, key=key)

    # --- search-side ranking (raw metric) ---------------------------------------------------------
    def rank(self, nodes) -> list:
        """Best-first by RAW observed metric with an id tie-break — the ordering every search policy
        shares (was `rank_by_metric`). Nodes must carry a non-None `metric` (the feasible/evaluated
        pools the policies rank always do)."""
        return sorted(nodes, key=lambda n: (n.metric, n.id), reverse=self._reverse)

    # --- promotion-side keys ----------------------------------------------------------------------
    @staticmethod
    def selection_key(node):
        """The PLAIN promotion ranked-scalar key: `(robust_metric, id)`. `robust_metric` is the multi-seed
        confirmed mean when present, else the raw metric (`models.Node.robust_metric`). Used by holdout_topk
        and as the tie-break-OFF path of `promotion_key`."""
        return (node.robust_metric, node.id)

    def _vc(self, node):
        """The direction-oriented calibrated-verifier tie-break component for `node` (goes BETWEEN the
        ranked metric and the id). A usable score is a real number in [0,1]; anything else (None, bool —
        a subclass of int —, NaN, out-of-range) contributes the neutral midpoint. Multiplied by the
        direction sign so a HIGHER score always wins a tie under the run's chooser (max/min). Self-guarding
        keeps every key a total, deterministic order regardless of how `verifier_score` was set (no NaN
        comparison can perturb min/max) — the fold's `_on_node_verified` also enforces the [0,1]-float rule
        before storing."""
        vs = node.verifier_score
        usable = (isinstance(vs, (int, float)) and not isinstance(vs, bool)
                  and vs == vs and 0.0 <= vs <= 1.0)
        return self._vsign * (float(vs) if usable else _NEUTRAL_VERIFIER)

    def promotion_key(self, node):
        """The mean-pick key `_select_best` ranks by. Plain `(robust_metric, id)` unless the R1-c
        verifier tie-break is on, in which case a direction-oriented calibrated-verifier slot sits BETWEEN
        the metric and the id: `(robust_metric, ±verifier_score, id)`. Because the metric stays the FIRST
        component, the verifier can only ever break a tie among metric-EQUAL nodes — it can NEVER move a
        node ahead of a strictly-better `robust_metric` (§21.7: the advisory score never overrides ground
        truth). An unscored node contributes the neutral midpoint, so it is neither promoted nor penalized."""
        if not self._verifier_tiebreak:
            return (node.robust_metric, node.id)
        return (node.robust_metric, self._vc(node), node.id)

    def holdout_key(self, node):
        """The holdout-promotion key: `(holdout_metric, id)`, with the SAME R1-c verifier tie-break slot
        inserted when enabled — `(holdout_metric, ±verifier_score, id)`. So a tie on the UNSEEN-signal
        metric is broken by soundness too, consistent with the mean pick; the verifier still only breaks
        holdout-metric TIES and never overrides a better holdout metric. Layers on top of the mean pick
        when the run recorded `holdout_select`."""
        if not self._verifier_tiebreak:
            return (node.holdout_metric, node.id)
        return (node.holdout_metric, self._vc(node), node.id)

    @staticmethod
    def eligible(node, flagged, aborted) -> bool:
        """The "can this node be selected best" predicate `_select_best` filters its mean-pick pool by:
        feasible, not trust-flagged, not aborted, and carrying a usable `robust_metric`. (`holdout_topk`
        expresses the same eligibility through a different-but-agreeing base — `feasible_nodes()` + the
        flagged filter — and shares only the ranked-scalar `selection_key`, not this predicate.)"""
        return (node.feasible and node.id not in flagged and node.id not in aborted
                and node.robust_metric is not None)
