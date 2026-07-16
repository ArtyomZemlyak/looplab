"""D3 · Tabular binary-classification TaskAdapter (ADR-2). A maximize-direction task: tune a
logistic-regression learner's (learning_rate, L2, iterations) to maximize K-fold CV accuracy on a
synthetic 2-feature, 2-class dataset. Pure-Python end to end (the generated solution embeds the data
+ a self-contained gradient-descent logistic regression + CV), so it runs in the sandbox with no
scientific-stack dependency. Exercises the `direction="max"` selection path and grows the demo surface.
"""
from __future__ import annotations

import random
from typing import Optional

from pydantic import BaseModel

from looplab.core.comparison import ComparisonContract
from looplab.core.models import Idea, Node, RunState
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMResearcher


def make_blobs(seed: int, n: int, sep: float) -> tuple[list[list[float]], list[int]]:
    """Two Gaussian blobs (`sep` apart) labelled 0/1 — a linearly-separable-ish classification set."""
    rng = random.Random(seed)
    X, Y = [], []
    for i in range(n):
        c = i % 2
        cx = sep if c else -sep
        X.append([round(rng.gauss(cx, 1.0), 4), round(rng.gauss(0.0, 1.0), 4)])
        Y.append(c)
    return X, Y


_CLF_TEMPLATE = '''\
import json, math

X = {X}
Y = {Y}
LR = {lr}
L2 = {l2}
ITERS = {iters}
K = {k}


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def train(Xtr, Ytr, lr, l2, iters):
    w = [0.0, 0.0]; b = 0.0
    for _ in range(int(iters)):
        gw = [0.0, 0.0]; gb = 0.0
        for xi, yi in zip(Xtr, Ytr):
            err = sigmoid(w[0]*xi[0] + w[1]*xi[1] + b) - yi
            gw[0] += err*xi[0]; gw[1] += err*xi[1]; gb += err
        n = max(1, len(Xtr))
        w[0] -= lr*(gw[0]/n + l2*w[0]); w[1] -= lr*(gw[1]/n + l2*w[1]); b -= lr*gb/n
    return w, b


def accuracy(Xs, Ys, w, b):
    if not Ys:
        return 0.0
    c = sum(1 for xi, yi in zip(Xs, Ys) if (1 if w[0]*xi[0] + w[1]*xi[1] + b >= 0 else 0) == yi)
    return c / len(Ys)


n = len(X); idx = list(range(n)); folds = [idx[i::K] for i in range(K)]
accs = []
for fold in folds:
    test = set(fold)
    Xtr = [X[i] for i in idx if i not in test]; Ytr = [Y[i] for i in idx if i not in test]
    Xte = [X[i] for i in fold]; Yte = [Y[i] for i in fold]
    if not Xtr or not Xte:
        continue
    w, b = train(Xtr, Ytr, LR, L2, ITERS)
    accs.append(accuracy(Xte, Yte, w, b))
print(json.dumps({{"metric": sum(accs)/len(accs) if accs else 0.0}}))
'''


class ClassificationResearcher:
    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft",
                        params={"lr": round(self.rng.choice([0.01, 0.05, 0.1, 0.3]), 3),
                                "l2": round(self.rng.choice([0.0, 0.001, 0.01]), 4),
                                "iters": float(self.rng.choice([50, 100, 200]))},
                        rationale="random learner config")
        pl = parent.idea.params.get("lr", 0.1)
        lr = min(1.0, max(0.001, round(pl * self.rng.choice([0.5, 1.0, 2.0]), 4)))
        return Idea(operator="improve",
                    params={"lr": lr, "l2": parent.idea.params.get("l2", 0.0),
                            "iters": parent.idea.params.get("iters", 100.0)},
                    rationale=f"perturb node {parent.id}")


class ClassificationDeveloper:
    def __init__(self, X, Y, k: int = 5):
        self.X = X
        self.Y = Y
        self.k = k

    def implement(self, idea: Idea) -> str:
        return _CLF_TEMPLATE.format(
            X=self.X, Y=self.Y, lr=float(idea.params.get("lr", 0.1)),
            l2=float(idea.params.get("l2", 0.0)),
            iters=int(round(idea.params.get("iters", 100))), k=self.k)


class ClassificationTask(BaseModel):
    kind: str = "classification"
    id: str = "blob_classification"
    goal: str = "tune a logistic-regression learner to maximize K-fold CV accuracy"
    direction: str = "max"
    comparison_contract: ComparisonContract | None = None
    n: int = 80
    sep: float = 1.5
    seed: int = 0
    cv_k: int = 5

    def _data(self):
        return make_blobs(self.seed, self.n, self.sep)

    def columns(self) -> dict[str, list]:
        X, Y = self._data()
        return {"x1": [r[0] for r in X], "x2": [r[1] for r in X], "y": Y}

    def build_roles(self):
        X, Y = self._data()
        return (ClassificationResearcher(seed=self.seed), ClassificationDeveloper(X, Y, k=self.cv_k))

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        X, Y = self._data()
        hint = ("Choose 'lr' (learning rate, 0.001..1), 'l2' (ridge strength >= 0) and 'iters' "
                "(gradient steps) for a logistic-regression classifier. HIGHER CV accuracy is better.")
        bounds = {"lr": (0.001, 1.0), "l2": (0.0, 1.0), "iters": (10.0, 500.0)}
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                ClassificationDeveloper(X, Y, k=self.cv_k))
