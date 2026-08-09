"""D3 · Tabular binary-classification TaskAdapter (ADR-2). A maximize-direction task: choose a
polynomial feature-map `degree` plus the learner's (lr, l2, iters) to maximize K-fold CV accuracy on
a synthetic 2-coordinate, 2-class dataset. Pure-Python end to end (the generated solution embeds the
data + a self-contained gradient-descent logistic regression + CV), so it runs in the sandbox with no
scientific-stack dependency. Exercises the `direction="max"` selection path and grows the demo surface.

WHY CONCENTRIC RINGS AND WHY `degree` IS A PARAMETER (2026-08-05). This adapter used to generate two
Gaussian BLOBS `sep` apart and template only (lr, l2, iters). Both halves were degenerate, and they
were degenerate in a way that reinforced itself:

  * The data was linearly separable, so the objective was FLAT. Measured over a 175-point grid
    (lr 0.001..1 x l2 0..1 x iters 10..500) the shipped defaults produced exactly TWO distinct
    accuracies, and 163 of the 175 configurations scored the identical 0.925. Every node tied, so
    the champion was whichever node happened to be evaluated — the search had nothing to find.
  * The template had no structural knob, so the one idea the Researcher kept proposing on this
    task — expand the features to degree 2 — could not be built. `implement()` ignored it and
    emitted the same plain linear learner, under a rationale that described the expansion. The
    engine reported work it had not done.

Fixing either half alone fixes nothing: `degree` is a no-op knob on separable blobs, and harder data
still leaves the Researcher's structural idea unbuildable. So both moved together. The rings' true
boundary IS a circle (x1^2 + x2^2 = c), i.e. exactly a degree-2 polynomial. Measured THROUGH THIS
DEVELOPER: a degree-1 learner never exceeds 0.5550 anywhere in the advertised lr/l2/iters grid,
while degree >= 2 reaches 0.905 — a real gradient for the search to climb, and the
Researcher's feature-expansion idea is now a parameter the Developer actually substitutes. A
700-point grid over the shipped defaults (degree 1..4 x the same lr/l2/iters ranges) now returns 79
distinct accuracies spanning 0.445..0.905, with the most common value covering 8% of the grid
instead of 93%.

A FIXED TEMPLATE CAN ONLY EVER BUILD ITS OWN PARAMETERIZATION. Widening it to cover `degree` closes
today's gap, not the general one — so `llm_roles`'s `space_hint` now also STATES the build surface
("the Developer fills a fixed template from exactly these four numbers"), which is what stops the
Researcher proposing kernels and ensembles nobody can implement. A task that wants genuinely
open-ended structure needs an LLM Developer that writes the code (see
`adapters/regression.py::CodeRegressionTask`), not a wider template.

The SAME defect had a second route, found by re-running this example live and comparing each node's
rationale against the code it ran: the intra-node SWEEP. `idea.space` is honoured only by
`LLMDeveloper`, but `make_roles` offered it here anyway, so the Researcher put `degree` in `space`
rather than `params`, `implement` fell back to its `degree` default, and two of eight nodes ran a
degree-1 program at chance under a "Sweep degree {2,3}" rationale. Fixed at the source rather than
here — `agents/factory.py::_offer_sweep` now asks the Developer (`roles.py::honors_idea_space`)
instead of enumerating the backends that cannot sweep.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from pydantic import BaseModel, field_validator

from looplab.core.comparison import ComparisonContract
from looplab.core.models import Idea, Node, RunState, validate_direction
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMResearcher


def make_rings(seed: int, n: int, gap: float, noise: float) -> tuple[list[list[float]], list[int]]:
    """Two CONCENTRIC rings labelled 0/1: class 0 near radius 1, class 1 near radius 1+`gap`, each
    smeared by Gaussian `noise` and spread uniformly over the angle.

    The classes differ ONLY in their distance from the origin, so the decision boundary is a circle
    and a straight line is the wrong hypothesis class. TWO SEPARATE FACTS, kept apart because the
    stronger one is tempting and false: brute-forcing every orientation x every threshold, the best
    possible line on the shipped sample scores 0.71 (against 0.915 for the radial boundary) — so a
    line is NOT literally at chance. But the LOG-LOSS learner this task fits cannot find that line
    on symmetric rings, and tops out at 0.5550 across the whole lr/l2/iters grid. The second number
    is the one the search actually faces, and its gap to degree 2 is what makes accuracy a genuine
    function of the feature-map degree (see the module docstring)."""
    rng = random.Random(seed)
    X, Y = [], []
    for i in range(n):
        c = i % 2
        r = (1.0 if c == 0 else 1.0 + gap) + rng.gauss(0.0, noise)
        th = rng.uniform(0.0, 2.0 * math.pi)
        X.append([round(r * math.cos(th), 4), round(r * math.sin(th), 4)])
        Y.append(c)
    return X, Y


_CLF_TEMPLATE = '''\
import json, math

X = {X}
Y = {Y}
DEGREE = {degree}
LR = {lr}
L2 = {l2}
ITERS = {iters}
K = {k}


def features(x, d):
    """Polynomial feature map over the two raw coordinates: every monomial x1**i * x2**j with
    1 <= i+j <= d, in ascending total order. d=1 is the plain linear model (2 features); d=2 adds
    x2**2, x1*x2, x1**2 (5 features) — the terms a circular boundary needs. The intercept is
    carried separately by `b`, so no constant term appears here."""
    f = []
    for total in range(1, int(d) + 1):
        for i in range(total + 1):
            f.append((x[0] ** i) * (x[1] ** (total - i)))
    return f


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def train(Ftr, Ytr, lr, l2, iters):
    w = [0.0] * len(Ftr[0]); b = 0.0
    for _ in range(int(iters)):
        gw = [0.0] * len(w); gb = 0.0
        for fi, yi in zip(Ftr, Ytr):
            err = sigmoid(sum(wj*fj for wj, fj in zip(w, fi)) + b) - yi
            for j in range(len(w)):
                gw[j] += err*fi[j]
            gb += err
        n = max(1, len(Ftr))
        for j in range(len(w)):
            w[j] -= lr*(gw[j]/n + l2*w[j])
        b -= lr*gb/n
        # The high-degree monomials are UNSCALED, so a too-large `lr` genuinely diverges here — that
        # is real signal about the (degree, lr) pair and the search should see it as a bad score, not
        # as an infrastructure failure. Stop at the first non-finite weight and report chance-level
        # accuracy instead of crashing the eval with an OverflowError.
        if not all(math.isfinite(v) for v in w) or not math.isfinite(b):
            return None, 0.0
    return w, b


def accuracy(Fs, Ys, w, b):
    if not Ys or w is None:
        return 0.0
    c = sum(1 for fi, yi in zip(Fs, Ys)
            if (1 if sum(wj*fj for wj, fj in zip(w, fi)) + b >= 0 else 0) == yi)
    return c / len(Ys)


F = [features(x, DEGREE) for x in X]
n = len(F); idx = list(range(n)); folds = [idx[i::K] for i in range(K)]
accs = []
for fold in folds:
    test = set(fold)
    Ftr = [F[i] for i in idx if i not in test]; Ytr = [Y[i] for i in idx if i not in test]
    Fte = [F[i] for i in fold]; Yte = [Y[i] for i in fold]
    if not Ftr or not Fte:
        continue
    w, b = train(Ftr, Ytr, LR, L2, ITERS)
    accs.append(accuracy(Fte, Yte, w, b))
print(json.dumps({{"metric": sum(accs)/len(accs) if accs else 0.0}}))
'''


class ClassificationResearcher:
    """Blind optimizer over (degree, lr, l2, iters) — the offline (`backend=toy`) fallback.

    `degree` moves like `RegressionResearcher`'s: a +/-1 random walk over the integer complexity
    axis, because it is the lever that decides whether the model can represent the boundary at all.
    Perturbing only `lr` (what this role did while the template had no degree) can never leave the
    linear family, so it could never clear chance on this data."""

    def __init__(self, max_degree: int = 4, seed: int = 0):
        self.max_degree = max_degree
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft",
                        params={"degree": float(self.rng.randint(1, self.max_degree)),
                                "lr": round(self.rng.choice([0.01, 0.05, 0.1, 0.3]), 3),
                                "l2": round(self.rng.choice([0.0, 0.001, 0.01]), 4),
                                "iters": float(self.rng.choice([50, 100, 200]))},
                        rationale="random learner config")
        pd = int(round(parent.idea.params.get("degree", 1)))
        degree = max(1, min(self.max_degree, pd + self.rng.choice([-1, 0, 1])))
        pl = parent.idea.params.get("lr", 0.1)
        lr = min(1.0, max(0.001, round(pl * self.rng.choice([0.5, 1.0, 2.0]), 4)))
        return Idea(operator="improve",
                    params={"degree": float(degree), "lr": lr,
                            "l2": parent.idea.params.get("l2", 0.0),
                            "iters": parent.idea.params.get("iters", 100.0)},
                    rationale=f"perturb node {parent.id} (degree={pd})")


class ClassificationDeveloper:
    def __init__(self, X, Y, k: int = 5, max_degree: int = 4):
        self.X = X
        self.Y = Y
        self.k = k
        self.max_degree = max_degree

    def implement(self, idea: Idea) -> str:
        # `degree` is clamped HERE as well as in the Researcher's bounds because this is the last
        # place before the code is written: a degree of 0 emits a feature-free model that always
        # predicts one class, and an unbounded degree makes the eval quadratic in the monomial count
        # for no accuracy gain. Clamping keeps the emitted program a valid member of the family the
        # rationale claims it is.
        degree = max(1, min(self.max_degree, int(round(idea.params.get("degree", 1)))))
        return _CLF_TEMPLATE.format(
            X=self.X, Y=self.Y, degree=degree, lr=float(idea.params.get("lr", 0.1)),
            l2=float(idea.params.get("l2", 0.0)),
            iters=int(round(idea.params.get("iters", 100))), k=self.k)


class ClassificationTask(BaseModel):
    kind: str = "classification"
    # RENAMED from `blob_classification` when the dataset changed from blobs to rings (2026-08-05).
    # The id is the cross-run comparison SCOPE (`looplab claims --scope ...`), so keeping it would
    # have silently pooled accuracies measured on two different datasets into one ranking.
    id: str = "ring_classification"
    goal: str = ("choose a polynomial feature-map degree + the learner's lr/l2/iters to maximize "
                 "K-fold CV accuracy on two concentric rings")
    direction: str = "max"

    @field_validator("direction")
    @classmethod
    def _direction_valid(cls, v):
        return validate_direction(v)
    comparison_contract: ComparisonContract | None = None
    n: int = 200
    gap: float = 1.6
    noise: float = 0.6
    seed: int = 0
    cv_k: int = 5
    max_degree: int = 4

    def _data(self):
        return make_rings(self.seed, self.n, self.gap, self.noise)

    def columns(self) -> dict[str, list]:
        X, Y = self._data()
        return {"x1": [r[0] for r in X], "x2": [r[1] for r in X], "y": Y}

    def build_roles(self):
        X, Y = self._data()
        return (ClassificationResearcher(max_degree=self.max_degree, seed=self.seed),
                ClassificationDeveloper(X, Y, k=self.cv_k, max_degree=self.max_degree))

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        X, Y = self._data()
        # The second paragraph is the BUILD SURFACE, and it is load-bearing prose, not decoration.
        # Without it the Researcher repeatedly proposed feature expansions, kernels and ensembles
        # that `ClassificationDeveloper` silently dropped on the floor, so the run's rationales
        # described work the emitted code never contained. State what the Developer can build and
        # the proposals stay inside it.
        hint = (f"Hyperparameters to choose: 'degree' (integer 1..{self.max_degree}, the order of "
                "the polynomial feature map over the two raw coordinates x1,x2 — degree 1 is a "
                "plain linear boundary, degree 2 adds x1^2, x1*x2 and x2^2), 'lr' (learning rate, "
                "0.001..1), 'l2' (ridge strength >= 0) and 'iters' (gradient steps). HIGHER CV "
                "accuracy is better.\n"
                "The Developer fills a FIXED template from exactly those four numbers: a "
                "gradient-descent logistic regression on the degree-`degree` monomials, scored by "
                "K-fold CV. It cannot add a different model family, a kernel, feature scaling, an "
                "ensemble or extra data — those proposals cannot be built. 'degree' is the only "
                "structural lever; the features are unscaled, so a high degree needs a smaller 'lr'.")
        bounds = {"degree": (1.0, float(self.max_degree)), "lr": (0.001, 1.0),
                  "l2": (0.0, 1.0), "iters": (10.0, 500.0)}
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                ClassificationDeveloper(X, Y, k=self.cv_k, max_degree=self.max_degree))

    def external_fallback_uses_llm(self) -> bool:
        return False  # the fallback fills a deterministic local template

    def gpu_capable(self) -> bool:
        """Both role pairs end in `ClassificationDeveloper`, a fixed pure-Python logistic-regression
        template — the roles only pick `degree`/`lr`/`l2`/`iters`, never code. Keeps this task out of
        the host GPU pool lease (`engine/resources.py::_task_gpu_capable`)."""
        return False
