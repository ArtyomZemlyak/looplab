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

    def __init__(self, direction: str, *, verifier_tiebreak: bool = False, ci_tie: bool = False,
                 ci_z: float = 1.96):
        self.direction = direction
        self._reverse = (direction == "max")     # best-first ordering for `sorted(..., reverse=)`
        # R1-c: when on, the mean-pick key gains a calibrated-verifier tie-break slot BETWEEN the metric
        # and the id, oriented by direction so a HIGHER verifier score always wins a metric-tie under the
        # run's chooser (max picks the larger key, min the smaller). Off -> plain (robust_metric, id).
        self._verifier_tiebreak = bool(verifier_tiebreak)
        # R1-d (§21.19): widen the verifier tie-break from EXACT-metric to a STATISTICAL tie — a node is
        # "tied" with the metric-leader when its mean is within the LEADER's confirm-noise CI (|Δ| <=
        # ci_z·SE_leader, SE=confirmed_std/sqrt(confirmed_seeds)). Anchored on the LEADER's SE only (not a
        # pooled SE_diff), so a noisy candidate's own variance can't widen the band. Requires
        # `verifier_tiebreak`. NEVER widens beyond the leader's measured noise, so a SIGNIFICANT difference
        # is never a tie (§21.7 preserved). Off -> exact-tie.
        self._ci_tie = bool(ci_tie) and self._verifier_tiebreak
        self._ci_z = float(ci_z)
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
        confirmed mean when present, else the raw metric (`models.Node.robust_metric`). Byte-identical to
        the verifier-tie-break-OFF path of `promotion_key`/`holdout_key`; retained as the plain-tuple
        reference (no non-test callers today — holdout_topk now ranks by `promotion_key`)."""
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

    @staticmethod
    def _se(node):
        """The standard error of a node's confirmed MEAN = confirmed_std / sqrt(confirmed_seeds), or None
        when the confirm-noise data is missing/degenerate (single seed / no std -> no usable SE). Rejects
        `bool` explicitly (it is an `int`/`float`-passing subclass): a foreign/hand-edited `confirmed_std:
        true` must NOT become std=1.0 and inflate the band into a §21.7 violation (mirrors `_vc`'s guard)."""
        std, n = node.confirmed_std, node.confirmed_seeds
        if isinstance(std, bool) or not isinstance(std, (int, float)) or std != std or std < 0:
            return None
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            return None
        import math
        return float(std) / math.sqrt(n)

    def _statistically_tied(self, node, leader) -> bool:
        """Is `node` STATISTICALLY INDISTINGUISHABLE from the metric-leader — does its mean fall within the
        LEADER's confidence interval, |Δmean| <= ci_z·SE_leader? The band is anchored on the LEADER's own
        precision ONLY: a candidate's own (possibly inflated) confirmed_std can NOT widen the band, so a lone
        high-variance node can't drag a genuinely-better tight leader into a tie. When the leader's SE is
        unknown, fall back to EXACT-metric equality (never a fabricated band). §21.7: a node whose metric is
        MORE than the leader's noise away is NOT tied and can never be chosen over the leader by soundness."""
        if node is leader:
            return True
        lm, nm = leader.robust_metric, node.robust_metric
        if lm is None or nm is None:
            return False
        lse = self._se(leader)
        if lse is None:
            return nm == lm                       # no leader noise estimate -> exact-tie only (conservative)
        # CODEX AGENT: this 1.96*leader-SE policy conflicts with confirm's `significant` certificate, which
        # fires at >1 pooled SE. In the ordinary 1..1.96-SE region best_ci selects the sounder tied node, then
        # replay sees `significant=True` and overwrites it with the mean winner. Use one shared paired decision
        # certificate/predicate across confirm and selection. Also, with n≈3 a fixed normal z, leader-only
        # variance and winner selection are not the advertised 95% confidence test; retain per-seed deltas
        # for a paired t/bootstrap interval or name this honestly as a heuristic leader margin.
        return abs(nm - lm) <= self._ci_z * lse

    def ci_tie_set(self, nodes):
        """The statistical tie-set `best_ci` decides among: the metric-LEADER (`selection_key`) plus every
        node within the leader's confirm-noise CI (`_statistically_tied`; exact-metric equality when the
        noise is unknown). SHARED by the selector (`best_ci`) AND the producer (`_metric_tie_groups`), so
        both use the SAME tie predicate — else a CI-tied candidate would be COMPARED by best_ci yet never
        SCORED by the producer (it would sit at the neutral midpoint and best_ci would fall back to the
        leader, making R1-d a no-op)."""
        if not nodes:
            return []
        leader = self.best(nodes, self.selection_key)   # metric-first leader (soundness-blind)
        return [n for n in nodes if self._statistically_tied(n, leader)]

    def best_ci(self, nodes):
        """R1-d (§21.19) mean-pick with a STATISTICAL (CI-band) verifier tie-break, when `ci_tie` is on.
        Two-stage: (1) the metric-leader by `selection_key` (metric-first, id tie-break); (2) among nodes
        statistically INDISTINGUISHABLE from it (`ci_tie_set`), pick by soundness THEN the metric THEN id —
        `(±verifier_score, robust_metric, id)` — so an unscored / equal-soundness tie-set falls back to the
        METRIC LEADER (not an arbitrary id), while a sounder within-noise node still wins. A significantly-
        worse node is excluded from the tie-set, so this can NEVER promote a node over a genuinely-better
        metric (§21.7). Identical to the plain exact-tie pick when ci_tie is off."""
        if not self._ci_tie:
            return self.best(nodes, self.promotion_key)
        tied = self.ci_tie_set(nodes)
        return self.best(tied, key=lambda n: (self._vc(n), n.robust_metric, n.id))

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
        flagged filter — and ranks by `promotion_key` (the plain `(robust_metric, id)` when the verifier
        tie-break is off), not this predicate.)"""
        return (node.feasible and node.id not in flagged and node.id not in aborted
                and node.robust_metric is not None)
