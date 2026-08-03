"""E2 · Researcher panel + empirical ranking (ADR-2). Generate K candidate ideas (the base Researcher
called K times -> K diverse proposals), then keep the one ranked best by a CHEAP EMPIRICAL surrogate
over the observed (params->metric) history — NOT an LLM-judge (verified: an LLM-as-judge is ~random
at ranking top vs bottom ideas). The panel widens exploration; the surrogate, not a debate, decides.

Wraps any Researcher behind the same Protocol. K=1 is a transparent pass-through. Bootstraps via the
first proposal until there's enough history to rank.
"""
from __future__ import annotations

import math
from typing import Optional

from looplab.agents.roles import forward_hints
from looplab.core.models import Idea, Node, RunState
from looplab.events.digest import knn_idw, numeric_params


def _predict(params: dict, hist: list[tuple[dict, float]], bounds, k: int = 3) -> Optional[float]:
    """Inverse-distance-weighted k-NN prediction of an idea's metric over the history (in the shared
    numeric param space). None when there's no comparable point. Eligibility here: a neighbour must
    contain ALL the target's keys (distance over the target's subspace); the IDW core is knn_idw."""
    target = numeric_params(params, keys=bounds or None)
    if not target:
        return None
    tkeys = set(target)
    pts = []
    for p, m in hist:
        if not tkeys.issubset(p):          # only full-dimensional points are comparable
            continue
        pts.append((math.sqrt(sum((target[x] - p[x]) ** 2 for x in tkeys)), m))
    res = knn_idw(pts, k)
    if res is None:
        return None
    pred = res[0]
    # A NaN param survives `numeric_params` (isinstance-numeric), makes every distance NaN, and
    # `knn_idw` documents that a NaN distance degrades to a NaN prediction. `propose` screens only
    # for None, and `is_better` is a bare `<` — every NaN comparison is False — so a NaN would
    # become `best_pred` and then be undisplaceable, handing the panel to the malformed candidate.
    # Abstain instead: an uncomparable candidate is exactly the "no signal" case None already means.
    return None if pred != pred else pred


class PanelResearcher:
    def __init__(self, base, k: int = 3, bounds=None, warmup: int = 3):
        self.base = base
        self.k = max(1, k)
        self.bounds = bounds if bounds is not None else getattr(base, "bounds", None)
        self.warmup = max(1, warmup)

    @property
    def space_hint(self) -> str:
        return getattr(self.base, "space_hint", "")

    # Lightweight read-throughs to the wrapped base (mirroring ForesightPanelResearcher's ctor
    # inheritance): chain-walkers like `engine/lessons.py::_merge_prompt_opts` / `reflect_client`
    # getattr these off the ACTIVE researcher — which may be THIS wrapper — and a missing attr here
    # silently shadowed the run's configured PromptStore / parser / client behind the defaults.
    @property
    def parser(self):
        return getattr(self.base, "parser", None)

    @property
    def prompts(self):
        return getattr(self.base, "prompts", None)

    @property
    def client(self):
        return getattr(self.base, "client", None)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # P2 delivery contract: the engine setattrs ephemeral hints on the OUTERMOST active
        # researcher — which may be THIS wrapper — so mirror them onto the base before the K-way
        # fan-out (roles.forward_hints owns the registry + `track_hypotheses` rule).
        forward_hints(self, self.base)
        if self.k == 1:
            return self.base.propose(state, parent)
        # Build the history BEFORE the fan-out. It depends only on `state`, and the warmup gate below
        # discards every proposal but the first — so ranking it after K blocking Researcher calls
        # burned K-1 paid LLM calls per turn that the panel was structurally unable to use.
        # `breedable_nodes()`, not `feasible_nodes()`: under `trust_gate=gate` a hard-flagged node
        # keeps its inflated metric and stays FEASIBLE, so fitting on it teaches the k-NN to propose
        # near the cheated params. Both sibling predictors already exclude it for this exact reason
        # (search/surrogate.py, search/proxy.py); under `audit`/no flags the two pools are identical.
        hist = [(numeric_params(n.idea.params), n.metric)
                for n in state.breedable_nodes() if n.metric is not None]
        if len(hist) < self.warmup:
            return self.base.propose(state, parent)   # not enough signal to rank -> one proposal
        ideas = [self.base.propose(state, parent) for _ in range(self.k)]
        best, best_pred = None, None
        for idea in ideas:
            pred = _predict(idea.params, hist, self.bounds)
            if pred is None:
                continue
            if best_pred is None or state.is_better(pred, best_pred):
                best, best_pred = idea, pred
        if best is not None:
            best.rationale = (best.rationale + f" [panel: best of {self.k} by surrogate]").strip()
            return best
        return ideas[0]
