"""A2 · Surrogate-guided proposal (BO-lite, ADR-2). When the action space is numeric params, fit a
cheap surrogate over the observed `(params -> metric)` history and propose the next point by
optimizing an acquisition function — instead of random/hill-climb. Pure-Python (zero-dep): an
inverse-distance-weighted k-NN surrogate + a distance (exploration) bonus, sampled over the bounds.

Behind the same `Researcher` Protocol, so it drops into the loop with no orchestrator change. Wraps a
`fallback` Researcher used to BOOTSTRAP (until there's enough history) and for non-numeric spaces.
Deterministic given the seed; like every Researcher its proposal is recorded in `node_created`, so
replay never re-runs it.
"""
from __future__ import annotations

import random
from typing import Optional

from looplab.agents.roles import WrapsResearcher, forward_hints
from looplab.core.models import Idea, Node, RunState
# Module scope, like every other search module that needs the digest primitives
# (`search/coverage.py`, `search/panel.py`). These three used to be function-local, annotated
# "search stays digest-free at import time" — but that was never a package invariant and no longer
# describes the code: `events/digest.py` imports only `core.models`, so there is no cycle to dodge
# and nothing above `core` is pulled in. Keeping the claim in three places invited a future reader
# to "restore" it in the modules that legitimately import at module scope.
from looplab.core.numeric import euclidean, knn_idw, numeric_params


def _fallback_telemetry(name: str) -> property:
    """A read/write property delegating a predictive-telemetry attr to `self.fallback`. The engine's
    `_emit_role_telemetry` getattrs these off the OUTERMOST researcher — which `_ensure_surrogate`
    can make THIS wrapper mid-run, over a `ForesightPanelResearcher` fallback — and a missing attr
    here silently dropped the panel's `hypothesis_ranked` / `foresight_selected` audit events. The
    setter delegates too: the engine CONSUMES a pick by setattr-ing None back onto the same handle.
    Deliberately per-attr properties, NOT a generic `__getattr__`: the cli/engine foresight wiring
    probes `getattr(researcher, "client", None)` and must keep falling through to None on a bare
    surrogate wrapper (a catch-all delegate would surface the fallback's client and flip that gate)."""
    def _get(self):
        return getattr(self.fallback, name, None)

    def _set(self, value):
        if self.fallback is not None:
            setattr(self.fallback, name, value)
    return property(_get, _set)


class SurrogateResearcher(WrapsResearcher):
    def __init__(self, bounds: dict, fallback=None, *, seed: int = 0,
                 n_candidates: int = 96, explore: float = 0.1, warmup: int = 4, k: int = 3,
                 infer_bounds: bool = True):
        self.bounds = bounds or {}
        # A task that DECLARES numeric bounds (the built-in benchmarks) keeps using them verbatim.
        # Everything else — repo and dataset tasks, i.e. the ones real operators run — declared none,
        # so `surrogate_proposer` / `policy=bohb` constructed nothing and the setting silently did
        # nothing at all. With this on, the ranges are learned from the run's OWN evaluated params
        # once there is enough history, so the setting means the same thing on every task.
        self.infer_bounds = infer_bounds
        self.fallback = fallback
        self.rng = random.Random(seed)
        self.n_candidates = max(8, n_candidates)
        self.explore = max(0.0, explore)
        self.warmup = max(2, warmup)
        self.k = max(1, k)

    @property
    def _delegate(self):
        # The wrapped researcher is `fallback` here, not `base` — and it may legitimately be None.
        return self.fallback

    # Outbound predictive telemetry reads (and consume-writes) through to the wrapped fallback —
    # see _fallback_telemetry for why these are explicit properties and not a generic __getattr__.
    last_hyp_priority = _fallback_telemetry("last_hyp_priority")
    last_foresight = _fallback_telemetry("last_foresight")
    last_foresight_pick = _fallback_telemetry("last_foresight_pick")

    def _observed_bounds(self, state: RunState) -> dict:
        """Numeric ranges learned from this run's own evaluated experiments.

        Only keys present with a numeric value in EVERY usable point are kept — a key that appears
        in half the experiments would otherwise define a dimension most history cannot fill, and
        `_history` drops any point that is not full-dimensional, starving the predictor. Ranges are
        padded by 10% of their own width so the optimizer can step just outside what was tried; a key
        whose observed values never varied is dropped rather than padded into a fake interval.
        """
        points = [numeric_params(n.idea.params, keys=None) for n in state.breedable_nodes()
                  if n.metric is not None]
        points = [p for p in points if p]
        if len(points) < self.warmup:
            return {}
        keys = set(points[0])
        for p in points[1:]:
            keys &= set(p)
        out = {}
        for key in sorted(keys):
            vals = [p[key] for p in points]
            lo, hi = min(vals), max(vals)
            if not (hi > lo):                  # never varied -> no interval to search
                continue
            pad = (hi - lo) * 0.1
            out[key] = (lo - pad, hi + pad)
        return out

    def _effective_bounds(self, state: RunState) -> dict:
        """Declared bounds when the task has them, else ranges inferred from the run so far."""
        if self.bounds:
            return self.bounds
        return self._observed_bounds(state) if self.infer_bounds else {}

    def _history(self, state: RunState, bounds: dict | None = None) -> list[tuple[dict, float]]:
        # Never fit the surrogate on a CHEATED metric: `breedable_nodes()` drops the gate-flagged set
        # (a hard-flagged node keeps its inflated metric — e.g. 0.99 from reading the answer key — AND
        # stays feasible under gate), so the predictor can't learn to propose near the cheated params.
        # audit / no flags -> identical to feasible_nodes(), a no-op in the default posture.
        bounds = self.bounds if bounds is None else bounds
        hist = []
        for n in state.breedable_nodes():
            if n.metric is None:
                continue
            p = numeric_params(n.idea.params, keys=bounds)
            if len(p) == len(bounds):          # only FULL-dimensional points are usable history
                hist.append((p, n.metric))
        return hist

    def _predict(self, x: dict, hist: list[tuple[dict, float]],
                 bounds: dict | None = None) -> tuple[float, float]:
        """Inverse-distance-weighted k-NN prediction + distance to the nearest sample (the
        exploration signal). Returns (predicted_metric, nearest_distance). Eligibility here is
        "full bounds dimensionality" (enforced by _history); the IDW core is the shared knn_idw."""
        keys = self.bounds if bounds is None else bounds
        pairs = [(euclidean(x, p, keys), m) for p, m in hist]
        pred, nearest = knn_idw(pairs, self.k)    # hist is non-empty (propose() gates on warmup)
        return pred, nearest

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # P2 delivery contract: the engine setattrs ephemeral hints on the OUTERMOST active
        # researcher — which may be THIS wrapper — so mirror them onto the fallback before any
        # delegation (roles.forward_hints owns the registry + `track_hypotheses` rule).
        if self.fallback is not None:
            forward_hints(self, self.fallback)
        bounds = self._effective_bounds(state)
        hist = self._history(state, bounds)
        if not bounds or len(hist) < self.warmup:
            if self.fallback is not None:                 # bootstrap / non-numeric -> delegate
                return self.fallback.propose(state, parent)
            params = {k: round(self.rng.uniform(lo, hi), 4) for k, (lo, hi) in bounds.items()}
            return Idea(operator="draft", params=params, rationale="surrogate bootstrap (random)")
        # Sample candidates over the bounds; score each by the acquisition (predicted metric adjusted
        # by an exploration bonus toward sparsely-sampled regions), and pick the optimum for the
        # objective direction. A simple, dependency-free EI/UCB surrogate.
        best_acq, best_params = None, None
        for _ in range(self.n_candidates):
            x = {k: self.rng.uniform(lo, hi) for k, (lo, hi) in bounds.items()}
            pred, nearest = self._predict(x, hist, bounds)
            # exploration: reward distance from known points (UCB-style); sign by direction.
            acq = pred - self.explore * nearest if state.direction == "min" else pred + self.explore * nearest
            if best_acq is None or state.is_better(acq, best_acq):
                best_acq, best_params = acq, x
        params = {k: round(v, 4) for k, v in best_params.items()}
        op = "improve" if parent is not None else "draft"
        return Idea(operator=op, params=params,
                    rationale=f"surrogate-guided (k-NN BO-lite, predicted={best_acq:.4g} over {len(hist)} obs)")
