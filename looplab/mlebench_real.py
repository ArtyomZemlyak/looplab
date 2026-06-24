"""Real MLE-bench competition TaskAdapter (D1, kind="mlebench_real").

Wraps an *actual* Kaggle competition prepared by mle-bench (see :mod:`looplab.mlebench_prep`):
the agent is given the official ``public/`` split (train + unlabeled test + sample submission +
description) and must write ``submission.csv``; the HOST scores it with mle-bench's *real*
grader against the held-out ``private/test.csv`` answers (out-of-process, never on the candidate
FS — :mod:`looplab.mlebench_grade`). The number the search optimises is therefore the genuine
MLE-bench metric, and the result carries the official medal/above-median report.

Trust model: this is the credible, comparable benchmark path. The answer key lives only in the
mle-bench data dir; ``assets()`` copies just the public files into the candidate workspace, and
grading runs on the host. Run untrusted-tier (Docker) for true isolation of the candidate code.

CPU-only throughout. The OFFLINE baselines use **numpy + the Python standard library only** (CSV via
:mod:`csv`), so they run with zero extra deps. The LLM brief additionally allows **pandas + scikit-
learn**, which are installed on the trusted_local host (the host grader needs them) and are therefore
importable by the candidate subprocess — they make the LLM's job far more reliable (e.g. column-by-
name handling avoids train/test feature misalignment). For the untrusted Docker tier those libs must
be added to the candidate image, or the brief constrained to numpy+stdlib.
"""
from __future__ import annotations

import csv as _csv
import io
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, model_validator

from .models import Idea, Node, RunState
from .parse import LLMClient
from .roles import LLMDeveloper, LLMResearcher


# --------------------------------------------------------------------------------------------------
# Competition metadata (direction, scorer, file names) — read from the mle-bench registry. The
# leaderboard (committed in the mle-bench repo) decides whether lower is better; the prepared
# public dir is where we read train/test/sample-submission from.
# --------------------------------------------------------------------------------------------------
def _competition(competition_id: str, data_dir: Optional[str] = None):
    """Resolve a mle-bench Competition under the given (or the default) data dir. The single place
    the registry/data-dir resolution lives. Imports mlebench lazily."""
    from mlebench.registry import registry
    reg = registry if not data_dir else registry.set_data_dir(Path(data_dir).resolve())
    return reg.get_competition(competition_id)


@lru_cache(maxsize=32)
def competition_meta(competition_id: str, data_dir: Optional[str] = None) -> dict:
    """Immutable competition metadata: {'direction','scorer','sample_name','public_dir','answers'}.
    Cached (these don't change). The mutable 'is it prepared yet' check is :func:`is_prepared`,
    deliberately NOT cached. Imports mlebench lazily so the rest of LoopLab doesn't depend on it."""
    from mlebench.data import get_leaderboard
    comp = _competition(competition_id, data_dir)
    direction = "min" if comp.grader.is_lower_better(get_leaderboard(comp)) else "max"
    return {
        "direction": direction,
        "scorer": comp.grader.name,
        "sample_name": comp.sample_submission.name,
        "public_dir": comp.public_dir,
        "answers": comp.answers,
    }


def is_prepared(competition_id: str, data_dir: Optional[str] = None) -> bool:
    """Live (uncached) check that the competition's public+private split exists locally."""
    from mlebench.data import is_dataset_prepared
    return is_dataset_prepared(_competition(competition_id, data_dir))


class MLEBenchRealTask(BaseModel):
    """A real Kaggle/MLE-bench competition. Requires the data to be prepared first:
    ``python -m looplab.mlebench_prep -c <competition>``."""

    kind: str = "mlebench_real"
    competition: str
    id: str = ""                       # defaults to the competition id
    data_dir: Optional[str] = None     # override the mle-bench data dir (else its default)
    goal: str = ""                     # defaults from the competition description
    direction: str = "auto"            # resolved from the grader/leaderboard
    submission: str = "submission.csv"
    grade_timeout: float = 300.0
    # Offline baseline hyperparameter bounds (NB smoothing / ridge lambda), tuned by the Researcher.
    max_param: float = 5.0

    @model_validator(mode="after")
    def _resolve(self):
        meta = competition_meta(self.competition, self.data_dir)
        if not self.id:
            self.id = self.competition
        if self.direction == "auto":
            self.direction = meta["direction"]
        if not self.goal:
            self.goal = (f"Solve the Kaggle competition '{self.competition}' "
                         f"({meta['scorer']}, {meta['direction']}). Train on train.csv, predict for "
                         "test.csv, and write submission.csv in the sample_submission format.")
        return self

    # --- data wiring -----------------------------------------------------------------------------
    def _meta(self) -> dict:
        return competition_meta(self.competition, self.data_dir)

    def _public_dir(self) -> Path:
        meta = self._meta()
        if not is_prepared(self.competition, self.data_dir):
            raise RuntimeError(
                f"Competition '{self.competition}' is not prepared. Run:\n"
                f"    python -m looplab.mlebench_prep -c {self.competition}"
                + (f" --data-dir {self.data_dir}" if self.data_dir else "")
                + "\n(You must accept the competition rules on kaggle.com first.)")
        return Path(meta["public_dir"])

    def assets(self) -> dict[str, str]:
        """Materialize the official public files into the candidate workspace: all top-level CSVs
        (train.csv, test.csv, sample_submission*.csv) + description.md. Subdirectories (e.g. nomad's
        per-sample geometry) are intentionally omitted — the CPU baselines use tabular features only.
        The held-out answers are NOT here (host-graded)."""
        pub = self._public_dir()
        out: dict[str, str] = {}
        for p in sorted(pub.iterdir()):
            if p.is_file() and (p.suffix.lower() == ".csv" or p.name == "description.md"):
                out[p.name] = p.read_text(encoding="utf-8", errors="replace")
        if not any(n.startswith("train") for n in out):
            raise RuntimeError(f"No train.csv found in prepared public dir {pub}")
        return out

    def host_grader(self) -> dict:
        """Out-of-process official grading: the candidate writes submission.csv; the host runs
        mle-bench's real grader against private/test.csv (in the data dir, never copied here)."""
        meta = self._meta()
        return {"kind": "mlebench", "competition": self.competition, "data_dir": self.data_dir,
                "submission": self.submission, "scorer": meta["scorer"], "predictions": self.submission,
                "timeout": self.grade_timeout}

    # --- roles -----------------------------------------------------------------------------------
    def build_roles(self):
        """Offline templated baseline (numpy+stdlib): a per-competition solver writing
        submission.csv, with one tunable hyperparameter the Researcher perturbs."""
        return (MLEBenchRealResearcher(self.competition, max_param=self.max_param),
                MLEBenchRealDeveloper(self.competition, self.submission))

    def _schema_preview(self, sample: str, n_rows: int = 2, max_field: int = 100) -> str:
        """A compact, truncated preview of train.csv + the sample submission so the LLM sees the REAL
        schema (column names + a couple rows) instead of guessing it. This is what stops the model
        confusing, e.g., spooky's single categorical `author` column with the submission's per-class
        probability columns. Long text fields are truncated to keep the prompt small."""
        import csv as _csv
        pub = self._public_dir()

        def head(path: Path, rows: int) -> str:
            if not path.is_file():
                return "(missing)"
            out = []
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                for i, row in enumerate(_csv.reader(f)):
                    if i > rows:
                        break
                    out.append(",".join((c[:max_field] + "…") if len(c) > max_field else c
                                        for c in row))
            return "\n".join(out)

        return (f"\n\nSCHEMA PREVIEW (real columns + first rows; text truncated):\n"
                f"--- train.csv ---\n{head(pub / 'train.csv', n_rows)}\n"
                f"--- test.csv ---\n{head(pub / 'test.csv', 1)}\n"
                f"--- {sample} (EXACT required output format) ---\n{head(pub / sample, 1)}\n")

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        meta = self._meta()
        sample = meta["sample_name"]
        hint = (f"Choose ONE numeric hyperparameter 'p' in (0, {self.max_param}] for your model "
                "(e.g. Naive-Bayes Laplace smoothing, or ridge regularization). Higher is more "
                "regularized. The score is the competition metric; tune 'p' to improve it.")
        brief = (
            f"This is the real Kaggle competition '{self.competition}'. Metric: {meta['scorer']} "
            f"({meta['direction']} is better). Read the training data from './train.csv' and the "
            "test features from './test.csv' (CSV files). You may use numpy, pandas and scikit-learn "
            "(all installed) plus the Python standard library; CPU only, no GPU/network. "
            f"A './description.md' explains the task and './{sample}' shows the EXACT required "
            "output format. IMPORTANT about columns: the column(s) you must predict are exactly the "
            f"non-id column(s) of './{sample}'. './test.csv' has the SAME columns as './train.csv' "
            "EXCEPT those target column(s); build your feature matrix from the SAME set of columns for "
            "train and test — exclude the id column and the target column(s) from the features (a "
            "train/test feature-count mismatch means you left a target or id column in). If "
            f"'./{sample}' has MULTIPLE non-id columns but './train.csv' has only ONE label/category "
            "column (a class name per row), this is a MULTI-CLASS task: do NOT expect those per-class "
            "columns to exist in train.csv — train a classifier on the single label column and OUTPUT "
            "one probability column per class (named exactly as the sample header), each row summing "
            "to 1. Train a model, "
            f"predict for every test row, and WRITE your predictions to './{self.submission}' with the "
            f"SAME header and row order as './{sample}'. Open every file with encoding='utf-8' (the "
            "data is UTF-8). Do NOT print a metric and do NOT read any test labels — the host scores "
            "your submission. You may use the suggested hyperparameter 'p'.")
        brief += self._schema_preview(sample)
        return (LLMResearcher(client, space_hint=hint,
                              bounds={"p": (1e-3, float(self.max_param))}, parser=parser),
                LLMDeveloper(client, brief=brief))


# --------------------------------------------------------------------------------------------------
# Offline roles: a baseline per competition family, pure numpy + stdlib, writing submission.csv.
# --------------------------------------------------------------------------------------------------
class MLEBenchRealResearcher:
    """Perturbs a single hyperparameter `p` (NB smoothing / ridge lambda)."""

    def __init__(self, competition: str, max_param: float = 5.0, seed: int = 0):
        import random
        self.competition = competition
        self.max_param = max_param
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft", params={"p": 1.0}, rationale="baseline p=1")
        pp = float(parent.idea.params.get("p", 1.0))
        nxt = min(self.max_param, max(1e-3, pp * self.rng.choice([0.5, 0.7, 1.4, 2.0])))
        return Idea(operator="improve", params={"p": round(nxt, 4)},
                    rationale=f"perturb p ({pp}->{round(nxt,4)})")


class MLEBenchRealDeveloper:
    """Renders the competition's baseline template with the proposed `p`."""

    def __init__(self, competition: str, submission: str = "submission.csv"):
        self.competition = competition
        self.submission = submission
        if competition not in _BASELINES:
            raise ValueError(
                f"No offline baseline for '{competition}'. Use backend=llm (LLM Developer) for "
                f"this competition, or add a template. Known: {sorted(_BASELINES)}")

    def implement(self, idea: Idea) -> str:
        p = float(idea.params.get("p", 1.0))
        # Placeholder substitution (not str.format) — the templates contain literal `{}` (dict
        # comprehensions, f-strings) that would collide with format fields.
        return (_BASELINES[self.competition]
                .replace("__P__", repr(p)).replace("__SUB__", repr(self.submission)))


# Shared stdlib CSV helpers prepended to every baseline (no pandas).
_CSV_HELPERS = '''\
import csv, math
import numpy as np
csv.field_size_limit(10_000_000)   # competition text fields can exceed csv's 131072 default

def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        return header, [row for row in r]

def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

def tokenize(s):
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in s).split() if t]
'''

# Multinomial Naive Bayes over bag-of-words, returning per-class log-probabilities. `p` = Laplace
# smoothing. Shared by the two text competitions.
_NB_TRAIN = '''\
def nb_fit(texts, labels, classes, alpha):
    vocab = {}
    for t in texts:
        for w in tokenize(t):
            if w not in vocab:
                vocab[w] = len(vocab)
    V = max(1, len(vocab))
    counts = {c: np.zeros(V) for c in classes}
    totals = {c: 0.0 for c in classes}
    n = {c: 0 for c in classes}
    for t, y in zip(texts, labels):
        for w in tokenize(t):
            j = vocab.get(w)
            if j is not None:
                counts[y][j] += 1.0
                totals[y] += 1.0
        n[y] += 1
    N = sum(n.values()) or 1
    logprior = {c: math.log((n[c] + 1.0) / (N + len(classes))) for c in classes}
    loglik = {c: np.log((counts[c] + alpha) / (totals[c] + alpha * V)) for c in classes}
    return vocab, logprior, loglik

def nb_logscores(text, vocab, logprior, loglik, classes):
    idx = [vocab[w] for w in tokenize(text) if w in vocab]
    out = {}
    for c in classes:
        s = logprior[c]
        if idx:
            s += float(loglik[c][idx].sum())
        out[c] = s
    return out

def softmax(d, classes):
    m = max(d[c] for c in classes)
    ex = {c: math.exp(d[c] - m) for c in classes}
    z = sum(ex.values()) or 1.0
    return {c: ex[c] / z for c in classes}
'''

_SPOOKY = _CSV_HELPERS + _NB_TRAIN + '''
ALPHA = __P__
CLASSES = ["EAP", "HPL", "MWS"]
h, tr = read_rows("train.csv")
ci = {name: i for i, name in enumerate(h)}
texts = [r[ci["text"]] for r in tr]
labels = [r[ci["author"]] for r in tr]
vocab, lp, ll = nb_fit(texts, labels, CLASSES, ALPHA)

th, te = read_rows("test.csv")
tci = {name: i for i, name in enumerate(th)}
out_rows = []
for r in te:
    probs = softmax(nb_logscores(r[tci["text"]], vocab, lp, ll, CLASSES), CLASSES)
    out_rows.append([r[tci["id"]]] + [f"{probs[c]:.6f}" for c in CLASSES])
write_csv(__SUB__, ["id"] + CLASSES, out_rows)
'''

_INSULTS = _CSV_HELPERS + _NB_TRAIN + '''
ALPHA = __P__
CLASSES = ["0", "1"]
h, tr = read_rows("train.csv")
ci = {name: i for i, name in enumerate(h)}
texts = [r[ci["Comment"]] for r in tr]
labels = [str(int(float(r[ci["Insult"]]))) for r in tr]
vocab, lp, ll = nb_fit(texts, labels, CLASSES, ALPHA)

th, te = read_rows("test.csv")
tci = {name: i for i, name in enumerate(th)}
out_rows = []
for r in te:
    probs = softmax(nb_logscores(r[tci["Comment"]], vocab, lp, ll, CLASSES), CLASSES)
    date = r[tci["Date"]] if "Date" in tci else ""
    out_rows.append([f"{probs['1']:.6f}", date, r[tci["Comment"]]])
write_csv(__SUB__, ["Insult", "Date", "Comment"], out_rows)
'''

# Ridge regression (numpy normal equations) predicting both nomad targets; clip >=0 (RMSLE).
_NOMAD = _CSV_HELPERS + '''
LAM = __P__
TARGETS = ["formation_energy_ev_natom", "bandgap_energy_ev"]
h, tr = read_rows("train.csv")
ci = {name: i for i, name in enumerate(h)}
feat_cols = [c for c in h if c not in (["id"] + TARGETS)]

def to_X(rows, header):
    idx = {name: i for i, name in enumerate(header)}
    X = np.asarray([[float(r[idx[c]]) for c in feat_cols] for r in rows], dtype=float)
    mu = X.mean(axis=0); sd = X.std(axis=0); sd[sd == 0] = 1.0
    return (X - mu) / sd, mu, sd

Xtr, mu, sd = to_X(tr, h)
Xtr = np.hstack([Xtr, np.ones((len(Xtr), 1))])
Ytr = np.asarray([[float(r[ci[t]]) for t in TARGETS] for r in tr], dtype=float)
d = Xtr.shape[1]
reg = LAM * np.eye(d); reg[-1, -1] = 0.0
W = np.linalg.solve(Xtr.T @ Xtr + reg, Xtr.T @ Ytr)

th, te = read_rows("test.csv")
tci = {name: i for i, name in enumerate(th)}
Xte_raw = np.asarray([[float(r[tci[c]]) for c in feat_cols] for r in te], dtype=float)
Xte = np.hstack([(Xte_raw - mu) / sd, np.ones((len(te), 1))])
P = np.clip(Xte @ W, 0.0, None)
out_rows = [[r[tci["id"]], f"{P[i,0]:.6f}", f"{P[i,1]:.6f}"] for i, r in enumerate(te)]
write_csv(__SUB__, ["id"] + TARGETS, out_rows)
'''

_BASELINES = {
    "spooky-author-identification": _SPOOKY,
    "detecting-insults-in-social-commentary": _INSULTS,
    "nomad2018-predict-transparent-conductors": _NOMAD,
}
