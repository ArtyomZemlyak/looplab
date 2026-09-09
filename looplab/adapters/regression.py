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

from looplab.adapters.synthetic import IntWalk, PerturbResearcher, ScaledChoice, SyntheticTaskBase
from looplab.core.models import Idea
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


def regression_researcher(max_degree: int = 6, seed: int = 0) -> PerturbResearcher:
    """Blind hyperparameter optimizer over (degree, lambda).

    The `degree` walk is the STRUCTURAL lever (it decides which polynomial family the fit can
    represent at all) and `lam` is a scale, which is why the two knobs move differently: +/-1 whole
    steps versus halve/keep/double. Collapsed onto `PerturbResearcher` (doc 25 RA-06) with the draw
    order — degree, then lam, in both branches — preserved, so this consumes the same random stream
    the hand-written class did and a seeded run proposes the same nodes it always has.
    """
    return PerturbResearcher(
        (IntWalk("degree", 0, max_degree, default=1.0),
         ScaledChoice("lam", (0.0, 0.001, 0.01, 0.1, 1.0), lo=0.0, ndigits=6, default=0.0)),
        seed=seed, draft_rationale="random hyperparameters")


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


class _PolyDataTask(SyntheticTaskBase):
    """The polynomial dataset the two regression tasks share — every field and both readers.

    `RegressionTask` and `CodeRegressionTask` differ ONLY in who writes the solution (a fixed
    template vs. an LLM reading `data.json`); they were two copies of the same six data fields,
    `_data()` and `columns()` (doc 25 RA-06). The field ORDER here reproduces both models exactly,
    which is what keeps `core/setup_identity.py::setup_config_hash` — and therefore every recorded
    run's `run_started.config_hash` — unchanged.
    """

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


class RegressionTask(_PolyDataTask):
    kind: str = "regression"
    id: str = "poly_regression"
    goal: str = "select polynomial degree + ridge lambda minimizing K-fold CV MSE"

    def build_roles(self) -> tuple[PerturbResearcher, RegressionDeveloper]:
        X, Y = self._data()
        return (
            regression_researcher(max_degree=self.max_degree, seed=self.seed),
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

    def gpu_capable(self) -> bool:
        """Both role pairs end in `RegressionDeveloper`, a fixed numpy template — the roles only pick
        `degree`/`lam`, never code. Keeps this task out of the host GPU pool lease (see
        `adapters/tasks.py::TASK_OPTIONAL_HOOKS` and `engine/resources.py::_task_gpu_capable`)."""
        return False


class CodeRegressionTask(_PolyDataTask):
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

    def assets(self) -> dict[str, str]:
        """Materialized into each node's sandbox workdir before the solution runs."""
        X, Y = self._data()
        return {"data.json": json.dumps({"x": X, "y": Y})}

    def build_roles(self):  # offline fallback (templated, embeds its own data)
        X, Y = self._data()
        return (regression_researcher(max_degree=self.max_degree, seed=self.seed),
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

    def external_fallback_uses_llm(self) -> bool:
        return True  # output validation retains the script-writing LLMDeveloper

    def gpu_capable(self) -> bool:
        """The LLM writes the code here, but the brief above pins it to "ONLY numpy and the Python
        standard library" — the same offline stack `core/hardware.py::task_runtime_caps` locks this
        task to, and it never offers it the torch capability sentence. Keeps it out of the host GPU
        pool lease (`engine/resources.py::_task_gpu_capable`)."""
        return False
