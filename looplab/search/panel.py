"""E2 · Researcher panel + empirical ranking (ADR-2). Generate K candidate ideas (the base Researcher
called K times -> K diverse proposals), then keep the one ranked best by a CHEAP EMPIRICAL surrogate
over the observed (params->metric) history — NOT an LLM-judge (verified: an LLM-as-judge is ~random
at ranking top vs bottom ideas). The panel widens exploration; the surrogate, not a debate, decides.

Wraps any Researcher behind the same Protocol. K=1 is a transparent pass-through. Bootstraps via the
first proposal until there's enough history to rank.
"""
from __future__ import annotations

from typing import Optional

from looplab.agents.roles import WrapsResearcher, forward_hints
from looplab.core.models import Idea, Node, RunState
from looplab.core.numeric import euclidean, knn_idw, numeric_params


def _predict_with_distance(params: dict, hist: list[tuple[dict, float]], bounds,
                           k: int = 3) -> Optional[tuple[float, float]]:
    """Inverse-distance-weighted k-NN prediction of an idea's metric over the history (in the shared
    numeric param space) AND the distance to its nearest evaluated neighbour — the one uncertainty
    proxy the search layer has (doc 51 §5: `knn_idw` returns both and this file kept only the
    first). None when there's no comparable point. Eligibility here: a neighbour must contain ALL
    the target's keys (distance over the target's subspace); the IDW core is knn_idw."""
    target = numeric_params(params, keys=bounds or None)
    if not target:
        return None
    tkeys = set(target)
    pts = []
    for p, m in hist:
        if not tkeys.issubset(p):          # only full-dimensional points are comparable
            continue
        pts.append((euclidean(target, p, tkeys), m))
    res = knn_idw(pts, k)
    if res is None:
        return None
    pred, nearest = res
    # A NaN param survives `numeric_params` (isinstance-numeric), makes every distance NaN, and
    # `knn_idw` documents that a NaN distance degrades to a NaN prediction. `propose` screens only
    # for None, and `is_better` is a bare `<` — every NaN comparison is False — so a NaN would
    # become `best_pred` and then be undisplaceable, handing the panel to the malformed candidate.
    # Abstain instead: an uncomparable candidate is exactly the "no signal" case None already means.
    return None if (pred != pred or nearest != nearest) else (pred, nearest)


def _predict(params: dict, hist: list[tuple[dict, float]], bounds, k: int = 3) -> Optional[float]:
    """The point estimate alone (the historical surface; `_predict_with_distance` is the pair)."""
    res = _predict_with_distance(params, hist, bounds, k)
    return None if res is None else res[0]


def acquisition(pred: float, nearest: float, explore: float, direction: str) -> float:
    """The UCB-style score the panel ranks by: the point estimate pushed toward the unexplored side
    by `explore` x the distance to the nearest evaluated point — `search/surrogate.py`'s own
    `acq = pred -/+ explore * nearest`, sign by objective direction, so the two callers of one IDW
    core spend its uncertainty the same way. `explore=0` is the historical point-estimate ranking."""
    return pred - explore * nearest if direction == "min" else pred + explore * nearest


class PanelResearcher(WrapsResearcher):
    def __init__(self, base, k: int = 3, bounds=None, warmup: int = 3, explore: float = 0.0):
        self.base = base
        self.k = max(1, k)
        self.bounds = bounds if bounds is not None else getattr(base, "bounds", None)
        self.warmup = max(1, warmup)
        # The exploration weight (doc 52 row 17): the CLI wires `Settings.surrogate_explore`, the
        # same knob the surrogate proposer spends, so the K diverse ideas are no longer ranked by
        # pure exploitation. The constructor default keeps every bare `PanelResearcher(...)` the
        # historical point-estimate ranking, byte for byte.
        self.explore = max(0.0, float(explore or 0.0))

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
            res = _predict_with_distance(idea.params, hist, self.bounds)
            if res is None:
                continue
            # Rank by the acquisition, not the point estimate: a candidate the history cannot
            # place is worth `explore` x its distance more than its prediction says.
            score = acquisition(res[0], res[1], self.explore, state.direction)
            if best_pred is None or state.is_better(score, best_pred):
                best, best_pred = idea, score
        if best is not None:
            best.rationale = (best.rationale + f" [panel: best of {self.k} by surrogate]").strip()
            return best
        return ideas[0]
