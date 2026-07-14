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
`.holdout_metric` / `.feasible`) and constructed from a `direction` string, importing nothing from
`core.models` — so BOTH the fold (`events/replay.py`, which may import only `core`) and the search
policies can route through it with no layering violation and no import cycle.

Purity: it reads, it never mutates or does I/O, so it is safe to call inside the deterministic fold
(invariant #5). Behavior is BYTE-IDENTICAL to the inlined logic it replaces — this is a refactor seam, not
a policy change; the regression is locked by `tests/test_events_replay.py` + the golden replay fixture.
"""
from __future__ import annotations


def is_better(direction: str, a: float, b: float) -> bool:
    """Direction-aware strict improvement: lower wins when minimizing, higher when maximizing. THE
    comparator — `RunState.is_better` delegates here so there is exactly one spelling of "better"."""
    return a < b if direction == "min" else a > b


class SearchFitness:
    """The run's ordering owner, built from its optimize `direction`. Stateless beyond `direction`;
    construct one per fold / policy call (cheap)."""

    def __init__(self, direction: str):
        self.direction = direction
        self._reverse = (direction == "max")     # best-first ordering for `sorted(..., reverse=)`

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
        """The promotion ranked-scalar key: `(robust_metric, id)`. `robust_metric` is the multi-seed
        confirmed mean when present, else the raw metric (`models.Node.robust_metric`). This is the SEAM
        a gated tie-break term extends (R1-c); today it is exactly the tuple `_select_best` inlined."""
        return (node.robust_metric, node.id)

    @staticmethod
    def holdout_key(node):
        """The holdout-promotion key: `(holdout_metric, id)` — the unseen-partition signal that layers
        ON TOP of `selection_key` when the run recorded `holdout_select`."""
        return (node.holdout_metric, node.id)

    @staticmethod
    def eligible(node, flagged, aborted) -> bool:
        """The ONE "can this node be selected best" predicate: feasible, not trust-flagged, not aborted,
        and carrying a usable `robust_metric`. Duplicated verbatim by `_select_best` and `holdout_topk`
        before this owner existed — centralized so the holdout pool and the fold's pick can never drift."""
        return (node.feasible and node.id not in flagged and node.id not in aborted
                and node.robust_metric is not None)
