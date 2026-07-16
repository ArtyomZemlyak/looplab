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

import hashlib
import json
import math

VERIFIER_SELECTION_CONTRACT = "selection-criteria:v1"


def is_better(direction: str, a: float, b: float) -> bool:
    """Direction-aware strict improvement: lower wins when minimizing, higher when maximizing. THE
    comparator — `RunState.is_better` delegates here so there is exactly one spelling of "better"."""
    return a < b if direction == "min" else a > b


def standard_error_difference(std: float, n: int, incumbent_std: float, incumbent_n: int) -> float:
    """Pooled SE of two independent mean estimates; shared by confirm and verifier CI selection."""
    def _se(value, count):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value <= 0
                or isinstance(count, bool) or not isinstance(count, int) or count <= 1):
            return 0.0
        return float(value) / math.sqrt(count)
    a, b = _se(std, n), _se(incumbent_std, incumbent_n)
    return math.sqrt(a * a + b * b)


# R1-c: the neutral verifier "score" for an unscored node (the §12 verifier's own `unclear` midpoint).
# In a metric-tie, a node scored ABOVE this beats an unscored node and one scored BELOW loses to it —
# so an unverified contender is treated as "no signal", neither promoted nor penalized past the midpoint.
_NEUTRAL_VERIFIER = 0.5


def verifier_evidence_snapshot(direction: str, node) -> dict:
    """Return the canonical evidence revision used by the selection verifier.

    The verifier is selection-affecting, so its score must be bound to the exact realized evidence it
    judged. Keep this projection in ``core`` so the live producer and the pure replay reader use one
    spelling. ``generalization_gap`` is derived here rather than read from the node because replay computes
    that display field only in its final selection pass, after individual events have folded.
    """

    def _finite(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    rationale = " ".join((getattr(getattr(node, "idea", None), "rationale", "") or "").split())[:250]
    metric = _finite(getattr(node, "metric", None))
    confirmed_mean = _finite(getattr(node, "confirmed_mean", None))
    confirmed_std = _finite(getattr(node, "confirmed_std", None))
    holdout_metric = _finite(getattr(node, "holdout_metric", None))
    robust = holdout_metric if holdout_metric is not None else confirmed_mean
    gap = None
    if metric is not None and robust is not None:
        try:
            gap = ((float(metric) - float(robust)) if direction == "max"
                   else (float(robust) - float(metric)))
            if not math.isfinite(gap):
                gap = None
        except (TypeError, ValueError, OverflowError):
            gap = None
    return {
        "v": 1,
        "direction": direction if direction in ("min", "max") else "min",
        "node_id": getattr(node, "id", None),
        "generation": getattr(node, "attempt", None),
        "rationale": rationale,
        "metric": metric,
        "confirmed_mean": confirmed_mean,
        "confirmed_std": confirmed_std,
        "confirmed_seeds": (getattr(node, "confirmed_seeds", None)
                            if isinstance(getattr(node, "confirmed_seeds", None), int)
                            and not isinstance(getattr(node, "confirmed_seeds", None), bool) else None),
        "holdout_metric": holdout_metric,
        "generalization_gap": gap,
    }


def verifier_evidence_digest(direction: str, node) -> str:
    """Stable SHA-256 identity for :func:`verifier_evidence_snapshot`."""

    raw = json.dumps(verifier_evidence_snapshot(direction, node), sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SearchFitness:
    """The run's ordering owner, built from its optimize `direction`. Stateless beyond `direction`
    (+ the R1-c `verifier_tiebreak` flag); construct one per fold / policy call (cheap)."""

    def __init__(self, direction: str, *, verifier_tiebreak: bool = False, ci_tie: bool = False):
        self.direction = direction
        self._reverse = (direction == "max")     # best-first ordering for `sorted(..., reverse=)`
        # R1-c: when on, the mean-pick key gains a calibrated-verifier tie-break slot BETWEEN the metric
        # and the id, oriented by direction so a HIGHER verifier score always wins a metric-tie under the
        # run's chooser (max picks the larger key, min the smaller). Off -> plain (robust_metric, id).
        self._verifier_tiebreak = bool(verifier_tiebreak)
        # R1-d (§21.19): widen the verifier tie-break from EXACT-metric to a STATISTICAL tie — a node is
        # "tied" with the metric leader inside a conservative one-SE band. The band is capped by the
        # leader's own precision and by trust.confirm's pooled SE-of-difference certificate: candidate
        # variance can never widen it, and the verifier can never cross a significant-difference boundary.
        # Requires `verifier_tiebreak`; off -> exact-tie.
        self._ci_tie = bool(ci_tie) and self._verifier_tiebreak
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

    @staticmethod
    def _usable_verifier(node) -> bool:
        vs = node.verifier_score
        return (isinstance(vs, (int, float)) and not isinstance(vs, bool)
                and math.isfinite(float(vs)) and 0.0 <= float(vs) <= 1.0)

    def _vc(self, node):
        """The direction-oriented calibrated-verifier tie-break component for `node` (goes BETWEEN the
        ranked metric and the id). A usable score is a real number in [0,1]; anything else (None, bool —
        a subclass of int —, NaN, out-of-range) contributes the neutral midpoint. Multiplied by the
        direction sign so a HIGHER score always wins a tie under the run's chooser (max/min). Self-guarding
        keeps every key a total, deterministic order regardless of how `verifier_score` was set (no NaN
        comparison can perturb min/max) — the fold's `_on_node_verified` also enforces the [0,1]-float rule
        before storing."""
        vs = node.verifier_score
        usable = self._usable_verifier(node)
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

    def rank_promotion(self, nodes) -> list:
        """Best-first robust ranking with verifier scores applied only to complete exact ties.

        ``holdout_topk`` consumes this ranking before holdout evidence exists. It cannot call ``best_ci``
        one node at a time, so precompute which exact-metric groups have one usable score for every member;
        torn/expanded groups fall back uniformly to the id order instead of mixing scores with neutrality.
        """
        nodes = list(nodes)
        if not self._verifier_tiebreak:
            return sorted(nodes, key=self.selection_key, reverse=self._reverse)
        by_metric: dict[object, list] = {}
        for node in nodes:
            by_metric.setdefault(node.robust_metric, []).append(node)
        complete = {node.id for group in by_metric.values() if len(group) >= 2
                    and all(self._usable_verifier(node) for node in group) for node in group}
        def key(node):
            return (node.robust_metric, self._vc(node) if node.id in complete else 0.0, node.id)
        return sorted(nodes, key=key, reverse=self._reverse)

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
        return float(std) / math.sqrt(n)

    def _statistically_tied(self, node, leader) -> bool:
        """Whether two confirmed means are within the conservative verifier tie band.

        The threshold is the smaller of the metric leader's SE and the pooled SE-of-difference used by
        ``trust.gate.one_se_better``. Missing variance fails back to exact equality. This keeps the tie a
        subset of statistically non-significant differences without letting a noisy challenger manufacture
        a wide band from its own variance.
        """
        if node is leader:
            return True
        lm, nm = leader.robust_metric, node.robust_metric
        if lm is None or nm is None:
            return False
        lse, nse = self._se(leader), self._se(node)
        if lse is None or nse is None:
            return nm == lm
        pooled = standard_error_difference(node.confirmed_std, node.confirmed_seeds,
                                           leader.confirmed_std, leader.confirmed_seeds)
        threshold = min(pooled, lse)
        return nm == lm if threshold <= 0.0 else abs(nm - lm) <= threshold

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
        leader = self.best(nodes, self.selection_key)
        tied = (self.ci_tie_set(nodes) if self._ci_tie else
                [n for n in nodes if n.robust_metric == leader.robust_metric])
        # A verifier treatment is meaningful only for the complete selector tie-set. Fail closed on a
        # torn legacy prefix or a newly-expanded tie until one atomic group event scores every
        # member; otherwise process timing could decide which node receives the artificial neutral value.
        if not self._verifier_tiebreak or not all(self._usable_verifier(n) for n in tied):
            return leader
        if self._ci_tie:
            return self.best(tied, key=lambda n: (self._vc(n), n.robust_metric, n.id))
        return self.best(tied, self.promotion_key)

    def best_holdout(self, nodes):
        """Exact holdout winner, using verifier soundness only when the whole tie was scored."""
        def plain(n):
            return (n.holdout_metric, n.id)
        leader = self.best(nodes, plain)
        tied = [n for n in nodes if n.holdout_metric == leader.holdout_metric]
        if not self._verifier_tiebreak or not all(self._usable_verifier(n) for n in tied):
            return leader
        return self.best(tied, self.holdout_key)

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
