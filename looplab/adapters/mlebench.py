"""MLEBench-style competition TaskAdapter (I20, ADR-2).

Models the shape of a real ML benchmark (Kaggle / MLEBench) without any external
dataset or network: a synthetic-but-realistic **binary classification** competition with
a **held-out test set** scored leaderboard-style.

The faithful bit is the *held-out grading*: the solution is given `train.json` (features
+ labels) and `test.json` (features ONLY — labels withheld). It trains a model, predicts
the test labels, and calls a `grader.score(predictions)` whose private answer key lives
inside the materialized `grader.py` — exactly how MLEBench grades against a private set.
This prevents the loop from optimizing a self-reported metric: the number it sees is the
true held-out accuracy.

Integrity & trust model (`trusted_local`):
  - The agent CANNOT *overwrite* the grader: `grader.py` is a task asset, and the
    orchestrator (a) writes node files before assets so assets win any name collision and
    (b) refuses to write a node file whose name matches a task asset. So a patch-gated
    agent shipping its own `grader.py` (even in-surface `*.py`) is ignored — enforced by
    construction (ADR-7 Rule 2), not by instruction. Covered by test_patch_gate.py.
  - The agent could still *read* the key (`from grader import _Y`) since the sandbox
    isn't file-isolated on this tier. That's an accepted `trusted_local` caveat (the
    threat is overfitting/honest error, not an adversarial solver). Closing it fully
    needs out-of-process grading or the `untrusted` (isolated) sandbox tier — tracked.
  - The reward-hack detector (`trust/reward_hack.py`) treats the MANDATED
    `from grader import score` as task-sanctioned here — shipping `grader.py` as an
    asset is the declaration (it reaches the detector via the engine's protected/asset
    set) — while key ACCESS (`grader._Y`, `_Y`) stays flagged.

Reuses the engine with zero loop changes: it's just a `TaskAdapter` with `assets()` (the
data + grader), a `brief` (the I/O contract), and a hyperparameter to optimize (`k`).
Offline (`backend=toy`) a templated k-NN developer makes it runnable without a model.
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


def make_classification_dataset(
    seed: int, n_train: int, n_test: int, n_features: int, sep: float, noise: float
) -> tuple[list[list[float]], list[int], list[list[float]], list[int]]:
    """Two balanced Gaussian blobs (separated class centers + noise). Returns
    (X_train, y_train, X_test, y_test) — the test labels are the private answer key.

    Train and test are drawn from INDEPENDENT RNG streams (the class centers are shared,
    so same distribution) so that changing `n_train` never perturbs the held-out test
    key — two runs differing only in train size stay comparable on the same leaderboard."""
    base = random.Random(seed)
    c0 = [base.uniform(-1.0, 1.0) for _ in range(n_features)]
    c1 = [v + base.choice([-1.0, 1.0]) * sep for v in c0]

    def sample(rng: random.Random, n: int) -> tuple[list[list[float]], list[int]]:
        X, y = [], []
        for i in range(n):
            cls = i % 2                       # balanced classes
            center = c1 if cls else c0
            X.append([round(center[j] + rng.gauss(0.0, noise), 4)
                      for j in range(n_features)])
            y.append(cls)
        order = list(range(n))
        rng.shuffle(order)
        return [X[i] for i in order], [y[i] for i in order]

    Xtr, ytr = sample(random.Random(seed * 2 + 1), n_train)
    Xte, yte = sample(random.Random(seed * 2 + 2), n_test)
    return Xtr, ytr, Xte, yte


# Offline templated solution: a self-contained k-NN that reads the assets and produces predictions.
# Shared body (deterministic, no LLM); the two variants below differ only in how the result is
# emitted (in-workdir grader vs. host-scored predictions file), so the classifier lives in one place.
_KNN_BODY = '''\
import json
with open("train.json", encoding="utf-8") as _f:
    TRAIN = json.load(_f)
with open("test.json", encoding="utf-8") as _f:
    TEST = json.load(_f)
K = {k}
Xtr, ytr, Xte = TRAIN["X"], TRAIN["y"], TEST["X"]
k = max(1, min(int(K), len(Xtr)))


def dist2(a, b):
    return sum((p - q) ** 2 for p, q in zip(a, b))


preds = []
for x in Xte:
    nn = sorted(range(len(Xtr)), key=lambda i: dist2(x, Xtr[i]))[:k]
    votes = {{}}
    for i in nn:
        votes[ytr[i]] = votes.get(ytr[i], 0) + 1
    preds.append(max(votes, key=votes.get))
'''

# In-workdir grader variant: grades via grader.py and prints the metric line.
_KNN_TEMPLATE = _KNN_BODY + '''
from grader import score
print(json.dumps({{"metric": score(preds)}}))
'''

# Host-graded variant (out-of-process grading): the solution writes ONLY predictions; the host scores
# them against the held-out labels (which are NEVER materialized into the workdir). Closes the
# in-process-grader leak entirely — there is no answer key on the candidate's filesystem to read.
_KNN_PREDICT_TEMPLATE = _KNN_BODY + '''
# Write predictions only — the HOST scores them against labels we never see (no self-report). Use a
# `with` block so the file is flushed/closed deterministically even if the process is killed after.
with open("predictions.json", "w", encoding="utf-8") as _f:
    json.dump(preds, _f)
'''

# The private grader (materialized as an asset). Holds the held-out answer key.
_GRADER_TEMPLATE = '''\
# Private grader (MLEBench-style). The held-out answer key lives here, not with the data.
_Y = {y_test}


def score(predictions):
    """Leaderboard metric: classification accuracy. Worst score (0.0) on a malformed
    submission (wrong length or non-integer elements) so a broken solution can't look
    good."""
    try:
        p = list(predictions)            # non-iterable submission (None/int/...) -> worst score
    except TypeError:
        return 0.0
    if len(p) != len(_Y):
        return 0.0
    correct = 0
    for a, b in zip(p, _Y):
        try:
            correct += int(int(a) == int(b))
        except (TypeError, ValueError):
            return 0.0
    return round(correct / len(_Y), 6)
'''


class MLEBenchResearcher:
    """Blind hyperparameter optimizer over the model-complexity knob `k` (k-NN count)."""

    def __init__(self, max_k: int = 15, seed: int = 0):
        self.max_k = max_k
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft", params={"k": float(self.rng.randint(1, self.max_k))},
                        rationale="random k")
        pk = int(round(parent.idea.params.get("k", 3)))
        k = max(1, min(self.max_k, pk + self.rng.choice([-2, -1, 1, 2])))
        return Idea(operator="improve", params={"k": float(k)},
                    rationale=f"perturb node {parent.id} (k={pk})")


class MLEBenchDeveloper:
    """Offline templated k-NN classifier. `host_graded` -> writes predictions.json (host scores it);
    otherwise grades via the in-workdir grader.py."""

    def __init__(self, max_train: int, host_graded: bool = False):
        self.max_train = max_train
        self.host_graded = host_graded

    def implement(self, idea: Idea) -> str:
        k = max(1, min(self.max_train, int(round(idea.params.get("k", 3)))))
        tmpl = _KNN_PREDICT_TEMPLATE if self.host_graded else _KNN_TEMPLATE
        return tmpl.format(k=k)


class MLEBenchTask(BaseModel):
    """A held-out-graded classification competition. The LLM/agent Developer writes a
    classifier; the Researcher tunes `k`. Offline it falls back to a templated k-NN."""

    kind: str = "mlebench"
    id: str = "mlebench_blobs"
    goal: str = ("train a classifier on train.json and maximize held-out accuracy on "
                 "test.json, scored by the private grader")
    direction: str = "max"          # accuracy: higher is better
    comparison_contract: ComparisonContract | None = None
    seed: int = 0
    n_train: int = 80
    n_test: int = 40
    n_features: int = 4
    sep: float = 2.0
    noise: float = 1.0
    max_k: int = 15
    # Out-of-process grading (recommended for untrusted/real benchmarks): the solution writes
    # predictions.json and the HOST scores it against held-out labels never placed on the candidate
    # FS — there is no answer key to read or self-report. False keeps the legacy in-workdir grader.
    host_graded: bool = False

    def _data(self):
        return make_classification_dataset(
            self.seed, self.n_train, self.n_test, self.n_features, self.sep, self.noise)

    def columns(self) -> dict[str, list]:
        """For the grounding/profiling pre-phase (I16): the training features + label."""
        Xtr, ytr, _, _ = self._data()
        cols = {f"f{j}": [row[j] for row in Xtr] for j in range(self.n_features)}
        cols["label"] = [float(v) for v in ytr]
        return cols

    def leakage_inputs(self) -> dict:
        """For the leakage-first gate (I9): prove train/test are disjoint (no row
        contamination). A faithful benchmark must not leak the test set into train."""
        Xtr, _, Xte, _ = self._data()
        return {"train_rows": Xtr, "test_rows": Xte}

    def assets(self) -> dict[str, str]:
        """Materialized into each node's sandbox workdir before the solution runs: train (with
        labels) + test (features only). In the legacy (in-workdir) mode the private grader is also
        written; under `host_graded` it is NOT — the answer key never touches the candidate FS."""
        Xtr, ytr, Xte, yte = self._data()
        a = {
            "train.json": json.dumps({"X": Xtr, "y": ytr}),
            "test.json": json.dumps({"X": Xte}),       # labels withheld
        }
        if not self.host_graded:
            a["grader.py"] = _GRADER_TEMPLATE.format(y_test=yte)
        return a

    def host_grader(self):
        """Out-of-process grading hook (B1+): when `host_graded`, hand the engine the held-out labels
        + scorer so the HOST scores predictions.json. None (legacy in-workdir grader) otherwise."""
        if not self.host_graded:
            return None
        _, _, _, yte = self._data()
        return {"predictions": "predictions.json", "scorer": "accuracy", "labels": yte}

    def build_roles(self):  # offline fallback (templated k-NN)
        return (MLEBenchResearcher(max_k=self.max_k, seed=self.seed),
                MLEBenchDeveloper(max_train=self.n_train, host_graded=self.host_graded))

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        hint = (f"Choose 'k' (integer 1..{self.max_k}): the model-complexity knob, e.g. "
                "the number of nearest neighbours. Higher held-out accuracy is better; "
                "too small overfits, too large underfits.")
        bounds = {"k": (1.0, float(self.max_k))}
        common = (
            "This is a held-out classification competition. Read training data from "
            "'./train.json' (a JSON object with 'X' = list of equal-length float feature "
            "rows and 'y' = list of integer class labels) and the test features from "
            "'./test.json' (a JSON object with 'X' only — the test labels are withheld). "
            "Train a classifier (you may use the suggested hyperparameter 'k', e.g. "
            "k-nearest-neighbours) and predict an integer label for every row of "
            "test.json's X, in order. ")
        if self.host_graded:
            brief = common + (
                "Then WRITE your predictions (a JSON list of integer labels, in test order) to "
                "'./predictions.json' with json.dump — the HOST scores them; do NOT print a metric "
                "and do NOT attempt to read the test labels. Use ONLY numpy and the Python standard "
                "library — scikit-learn/pandas/scipy are NOT installed.")
        else:
            brief = common + (
                "Then score your predictions with the provided grader and print EXACTLY one final "
                'line of JSON: {"metric": <float>} produced by:\n'
                "    from grader import score\n"
                "    print(json.dumps({\"metric\": score(predictions)}))\n"
                "Do NOT read or import the test labels any other way. Use ONLY numpy and the "
                "Python standard library — scikit-learn, pandas and scipy are NOT installed "
                "and importing them will crash. Print nothing after that JSON line.")
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                LLMDeveloper(client, brief=brief))
