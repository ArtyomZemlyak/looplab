"""A real (small) ML TaskAdapter (ADR-2): polynomial-degree + ridge model selection
by K-fold cross-validation. Pure-Python end to end — the generated solution embeds
the dataset and a self-contained ridge-regression + CV routine, so it runs in the
sandbox with no scientific-stack dependency.

The loop optimizes the hyperparameters (degree, ridge lambda) to minimize CV MSE;
the optimum degree should recover the data's true generating degree — i.e. the agent
discovers the right model complexity, the canonical ML-research move.
"""
from __future__ import annotations

import json
import random
from typing import Optional

from pydantic import BaseModel

from looplab.core.comparison import ComparisonContract
from looplab.core.models import Idea, Node, RunState
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMDeveloper, LLMResearcher


def make_poly_dataset(seed: int, n: int, true_degree: int, noise: float
                      ) -> tuple[list[float], list[float]]:
    rng = random.Random(seed)
    coeffs = [rng.uniform(-2.0, 2.0) for _ in range(true_degree + 1)]
    X, Y = [], []
    for _ in range(n):
        x = rng.uniform(-3.0, 3.0)
        y = sum(c * x ** p for p, c in enumerate(coeffs)) + rng.gauss(0.0, noise)
        X.append(round(x, 4))
        Y.append(round(y, 4))
    return X, Y

# The generated solution: self-contained ridge polynomial regression + K-fold CV.
_REG_TEMPLATE = '''\
import json

X = {X}
Y = {Y}
DEGREE = {degree}
LAM = {lam}
K = {k}


def features(x, d):
    f = [1.0]
    for p in range(1, d + 1):
        f.append(x ** p)
    return f


def fit(xs, ys, d, lam):
    Phi = [features(x, d) for x in xs]
    m = len(Phi[0])
    A = [[0.0] * m for _ in range(m)]
    b = [0.0] * m
    for row, yi in zip(Phi, ys):
        for i in range(m):
            b[i] += row[i] * yi
            for j in range(m):
                A[i][j] += row[i] * row[j]
    for i in range(m):
        A[i][i] += lam
    # Gauss-Jordan solve (A | b)
    M = [A[i][:] + [b[i]] for i in range(m)]
    for col in range(m):
        piv = max(range(col, m), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            M[col][col] = 1e-12
        pv = M[col][col]
        for j in range(col, m + 1):
            M[col][j] /= pv
        for r in range(m):
            if r != col:
                fac = M[r][col]
                for j in range(col, m + 1):
                    M[r][j] -= fac * M[col][j]
    return [M[i][m] for i in range(m)]


def predict(w, x, d):
    return sum(wi * fi for wi, fi in zip(w, features(x, d)))


def cv_mse(X, Y, d, lam, k):
    n = len(X)
    idx = list(range(n))
    folds = [idx[i::k] for i in range(k)]
    errs = []
    for fold in folds:
        test = set(fold)
        xtr = [X[i] for i in idx if i not in test]
        ytr = [Y[i] for i in idx if i not in test]
        if not xtr or not fold:
            continue
        w = fit(xtr, ytr, d, lam)
        se = sum((predict(w, X[i], d) - Y[i]) ** 2 for i in fold)
        errs.append(se / len(fold))
    return sum(errs) / len(errs) if errs else float("inf")


print(json.dumps({{"metric": cv_mse(X, Y, DEGREE, LAM, K)}}))
'''


class RegressionResearcher:
    """Blind hyperparameter optimizer over (degree, lambda)."""

    def __init__(self, max_degree: int = 6, seed: int = 0):
        self.max_degree = max_degree
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            degree = self.rng.randint(0, self.max_degree)
            lam = self.rng.choice([0.0, 0.001, 0.01, 0.1, 1.0])
            return Idea(operator="draft", params={"degree": float(degree), "lam": lam},
                        rationale="random hyperparameters")
        pd = int(round(parent.idea.params.get("degree", 1)))
        degree = max(0, min(self.max_degree, pd + self.rng.choice([-1, 0, 1])))
        pl = parent.idea.params.get("lam", 0.0)
        lam = max(0.0, round(pl * self.rng.choice([0.5, 1.0, 2.0]), 6))
        return Idea(operator="improve", params={"degree": float(degree), "lam": lam},
                    rationale=f"perturb node {parent.id} (degree={pd})")


class RegressionDeveloper:
    def __init__(self, X: list[float], Y: list[float], k: int = 5):
        self.X = X
        self.Y = Y
        self.k = k

    def implement(self, idea: Idea) -> str:
        return _REG_TEMPLATE.format(
            X=self.X, Y=self.Y,
            degree=int(round(idea.params.get("degree", 1))),
            lam=float(idea.params.get("lam", 0.0)),
            k=self.k,
        )


class RegressionTask(BaseModel):
    kind: str = "regression"
    id: str = "poly_regression"
    goal: str = "select polynomial degree + ridge lambda minimizing K-fold CV MSE"
    direction: str = "min"
    comparison_contract: ComparisonContract | None = None
    n: int = 40
    true_degree: int = 2
    noise: float = 1.0
    seed: int = 0
    max_degree: int = 6
    cv_k: int = 5

    def _data(self) -> tuple[list[float], list[float]]:
        return make_poly_dataset(self.seed, self.n, self.true_degree, self.noise)

    def columns(self) -> dict[str, list]:
        """For the grounding/profiling pre-phase (I16)."""
        X, Y = self._data()
        return {"x": X, "y": Y}

    def build_roles(self) -> tuple[RegressionResearcher, RegressionDeveloper]:
        X, Y = self._data()
        return (
            RegressionResearcher(max_degree=self.max_degree, seed=self.seed),
            RegressionDeveloper(X, Y, k=self.cv_k),
        )

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        X, Y = self._data()
        hint = (f"Hyperparameters to choose: 'degree' (integer 0..{self.max_degree}, the "
                "polynomial order) and 'lam' (float >= 0, ridge regularization). Lower "
                "K-fold CV MSE is better. Avoid underfitting (degree too low) and "
                "overfitting (degree too high).")
        bounds = {"degree": (0.0, float(self.max_degree)), "lam": (0.0, 100.0)}
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                RegressionDeveloper(X, Y, k=self.cv_k))


class CodeRegressionTask(BaseModel):
    """Like RegressionTask, but the LLM *writes the solution code* (reading the dataset
    from a `data.json` asset) instead of filling a fixed template — a real coding loop.
    Offline (`backend=toy`) it falls back to the templated regression roles so the
    engine still runs without a model.

    TRUST-BOUNDARY CAVEAT: this is a DEMO task — the solution computes AND self-reports its
    own K-fold CV MSE (no private grader), so a reward-hacking model could print a fake metric.
    That is acceptable here because it only demonstrates the LLM-writes-code loop. For the real
    "agent never authors its own metric" guarantee use MLEBenchTask (held-out grader holds the
    answer key) or RepoTask (the operator's own eval command + protected metric reader)."""

    kind: str = "code_regression"
    id: str = "code_poly_regression"
    goal: str = "write code that fits a polynomial+ridge model minimizing K-fold CV MSE"
    direction: str = "min"
    comparison_contract: ComparisonContract | None = None
    n: int = 40
    true_degree: int = 2
    noise: float = 1.0
    seed: int = 0
    max_degree: int = 6
    cv_k: int = 5

    def _data(self) -> tuple[list[float], list[float]]:
        return make_poly_dataset(self.seed, self.n, self.true_degree, self.noise)

    def columns(self) -> dict[str, list]:
        X, Y = self._data()
        return {"x": X, "y": Y}

    def assets(self) -> dict[str, str]:
        """Materialized into each node's sandbox workdir before the solution runs."""
        X, Y = self._data()
        return {"data.json": json.dumps({"x": X, "y": Y})}

    def build_roles(self):  # offline fallback (templated, embeds its own data)
        X, Y = self._data()
        return (RegressionResearcher(max_degree=self.max_degree, seed=self.seed),
                RegressionDeveloper(X, Y, k=self.cv_k))

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        hint = (f"Choose 'degree' (integer 0..{self.max_degree}) and 'lam' (float >= 0, "
                "ridge strength) for a polynomial regression. Lower CV MSE is better.")
        bounds = {"degree": (0.0, float(self.max_degree)), "lam": (0.0, 100.0)}
        brief = (
            "The script MUST read the dataset from './data.json' (a JSON object with keys "
            "'x' and 'y', each a list of floats of equal length). Fit a polynomial ridge "
            f"regression of the requested degree and evaluate it with {self.cv_k}-fold "
            "cross-validation. Print EXACTLY one final line of JSON: "
            '{"metric": <float>} where <float> is the mean CV mean-squared-error '
            "(lower is better). Use ONLY numpy and the Python standard library — "
            "scikit-learn (sklearn), pandas and scipy are NOT installed and importing "
            "them will crash. Implement ridge regression with numpy directly. "
            "Print nothing after that JSON line."
        )
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                LLMDeveloper(client, brief=brief))
