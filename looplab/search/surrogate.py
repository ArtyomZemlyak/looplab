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

import math
import random
from typing import Optional

from looplab.agents.roles import forward_hints
from looplab.core.models import Idea, Node, RunState


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


class SurrogateResearcher:
    def __init__(self, bounds: dict, fallback=None, *, seed: int = 0,
                 n_candidates: int = 96, explore: float = 0.1, warmup: int = 4, k: int = 3):
        self.bounds = bounds or {}
        self.fallback = fallback
        self.rng = random.Random(seed)
        self.n_candidates = max(8, n_candidates)
        self.explore = max(0.0, explore)
        self.warmup = max(2, warmup)
        self.k = max(1, k)

    # forward the hooks make_roles / prompt store poke at, to the fallback
    @property
    def space_hint(self) -> str:
        return getattr(self.fallback, "space_hint", "")

    # Outbound predictive telemetry reads (and consume-writes) through to the wrapped fallback —
    # see _fallback_telemetry for why these are explicit properties and not a generic __getattr__.
    last_hyp_priority = _fallback_telemetry("last_hyp_priority")
    last_foresight = _fallback_telemetry("last_foresight")
    last_foresight_pick = _fallback_telemetry("last_foresight_pick")

    def _history(self, state: RunState) -> list[tuple[dict, float]]:
        hist = []
        for n in state.feasible_nodes():
            if n.metric is None:
                continue
            p = {k: float(v) for k, v in n.idea.params.items()
                 if k in self.bounds and isinstance(v, (int, float))}
            if len(p) == len(self.bounds):
                hist.append((p, n.metric))
        return hist

    def _predict(self, x: dict, hist: list[tuple[dict, float]]) -> tuple[float, float]:
        """Inverse-distance-weighted k-NN prediction + distance to the nearest sample (the
        exploration signal). Returns (predicted_metric, nearest_distance)."""
        dists = sorted(((math.sqrt(sum((x[k] - p[k]) ** 2 for k in self.bounds)), m)
                        for p, m in hist), key=lambda t: t[0])
        nn = dists[: self.k]
        nearest = nn[0][0]
        if nearest == 0.0:
            return nn[0][1], 0.0
        wsum = sum(1.0 / d for d, _ in nn)
        pred = sum((1.0 / d) * m for d, m in nn) / wsum
        return pred, nearest

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # P2 delivery contract: the engine setattrs ephemeral hints on the OUTERMOST active
        # researcher — which may be THIS wrapper — so mirror them onto the fallback before any
        # delegation (roles.forward_hints owns the registry + `track_hypotheses` rule).
        if self.fallback is not None:
            forward_hints(self, self.fallback)
        hist = self._history(state)
        if not self.bounds or len(hist) < self.warmup:
            if self.fallback is not None:                 # bootstrap / non-numeric -> delegate
                return self.fallback.propose(state, parent)
            params = {k: round(self.rng.uniform(lo, hi), 4) for k, (lo, hi) in self.bounds.items()}
            return Idea(operator="draft", params=params, rationale="surrogate bootstrap (random)")
        # Sample candidates over the bounds; score each by the acquisition (predicted metric adjusted
        # by an exploration bonus toward sparsely-sampled regions), and pick the optimum for the
        # objective direction. A simple, dependency-free EI/UCB surrogate.
        best_acq, best_params = None, None
        for _ in range(self.n_candidates):
            x = {k: self.rng.uniform(lo, hi) for k, (lo, hi) in self.bounds.items()}
            pred, nearest = self._predict(x, hist)
            # exploration: reward distance from known points (UCB-style); sign by direction.
            acq = pred - self.explore * nearest if state.direction == "min" else pred + self.explore * nearest
            if best_acq is None or state.is_better(acq, best_acq):
                best_acq, best_params = acq, x
        params = {k: round(v, 4) for k, v in best_params.items()}
        op = "improve" if parent is not None else "draft"
        return Idea(operator=op, params=params,
                    rationale=f"surrogate-guided (k-NN BO-lite, predicted={best_acq:.4g} over {len(hist)} obs)")
