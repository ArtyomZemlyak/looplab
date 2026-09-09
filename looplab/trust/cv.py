"""Consistent evaluation harness + cross-validation (I8, ADR-15) — pure-Python.

The key property is *consistency*: every candidate is scored on the **same** folds,
so node-to-node comparisons in the search tree are valid. Includes a custom
purged/embargoed walk-forward splitter for temporal tasks (no library models the
look-ahead gap — that's our code, per ADR-15).

Wired vs. library: ``cv_summary`` (the confirm/variance gate) and — since 2026-09-08 — the
feature-engineering CV gate at the bottom of this file (``feature_cv_findings``, run per evaluated
node when ``Settings.feature_engineering`` is on) are on the live engine path.
``kfold_indices``, ``purged_walk_forward``, ``consistent_cv`` and the ``Evaluator``
Protocol are the ADR-15 splitter *library* — complete and tested, but not yet consumed by a
shipped adapter; a temporal TaskAdapter that runs its own consistent CV is their intended caller.
Kept (not deleted) because they are the documented seam for that adapter.
"""
from __future__ import annotations

import re
from typing import Callable, Protocol


def kfold_indices(n: int, k: int) -> list[tuple[list[int], list[int]]]:
    """Contiguous K-fold splits over range(n). Tests partition the index set exactly."""
    if k < 2 or k > n:
        raise ValueError("need 2 <= k <= n")
    idx = list(range(n))
    sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
    splits, start = [], 0
    for s in sizes:
        test = idx[start : start + s]
        test_set = set(test)
        train = [j for j in idx if j not in test_set]
        splits.append((train, test))
        start += s
    return splits


def purged_walk_forward(n: int, n_splits: int, embargo: int = 0
                        ) -> list[tuple[list[int], list[int]]]:
    """Expanding-window time-series CV: train is strictly before test, with an
    `embargo` gap of samples dropped between them to prevent leakage across the
    boundary (purging)."""
    fold = max(1, n // (n_splits + 1))
    splits = []
    for i in range(1, n_splits + 1):
        test_start = i * fold
        if test_start >= n:            # no samples left for a test window
            break
        test_end = n if i == n_splits else min(n, (i + 1) * fold)
        train_end = max(0, test_start - embargo)
        train = list(range(0, train_end))
        test = list(range(test_start, test_end))
        if train and test:
            splits.append((train, test))
    return splits


class Evaluator(Protocol):
    def score(self, train: list[int], test: list[int]) -> float: ...


def consistent_cv(eval_fn: Callable[[list[int], list[int]], float],
                  splits: list[tuple[list[int], list[int]]]) -> list[float]:
    """Apply the SAME splits to a candidate's eval_fn — the consistency guarantee."""
    return [eval_fn(train, test) for train, test in splits]


def cv_summary(scores: list[float]) -> dict:
    n = len(scores)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "n": 0}
    m = sum(scores) / n
    # Sample std (Bessel) so SE = std/sqrt(n) is unbiased; matters at small seed counts.
    std = (sum((x - m) ** 2 for x in scores) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"mean": m, "std": std, "n": n}


# ----------------------------------------------------------------- the feature-engineering CV gate
#
# THE RUNG (docs/BACKLOG.md §0.1 row 13). `Settings.feature_engineering` told the model "KEEP a
# feature only if it improves CV; drop any that don't" and nothing checked it; this is the check, on
# the deterministic tier — pure Python, no model, no execution. It fires only when the operator
# turned feature engineering ON: the run that never asked for engineered features has no ledger to
# read and nothing to say.
#
# WHAT IT DECIDES ON. The candidate's OWN declared ledger (`search/operators.py::parse_feature_cv`),
# run through the operator's own keep/drop rule (`feature_engineering_verdicts`, the >1-SE test when
# the row declares a spread). A node is flagged when the two disagree with what it actually shipped:
# its ledger says a feature did not pay for itself, and the code still builds it. The evidence is
# therefore the node's own numbers, which is what makes this rung high-precision enough to sit in
# front of a non-audit gate — an `is_hard_signal` namespace excludes a flagged node from selection
# AND from breeding under `trust_gate` gate/block, and precision is the whole cost of that.
#
# WHAT IT DELIBERATELY DOES NOT DO, both stated recall gaps in the safe direction:
#   * a candidate that prints NO ledger is not flagged. "Nothing was claimed" is not "a claim was
#     broken", and the alternative — inferring feature construction from an AST and hard-gating on
#     the inference — flags every honest solution that assigns a column.
#   * a feature the code MENTIONS on a dropping line (`df.drop(columns=["ratio_ab"])`) counts as
#     dropped. A name search alone cannot tell "still built" from "explicitly removed", and reading
#     a removal as a violation would punish exactly the behaviour the gate is asking for.
FEATURE_CV_NS = "feature_cv:"

#: Names that make a line a REMOVAL of the feature it mentions rather than a construction of it.
_DROPPING_RE = re.compile(r"(?<![\w])(drop|del|delete|pop|remove|exclude|discard)(?![\w])",
                          re.IGNORECASE)


def feature_is_kept(code: str, feature: str) -> bool:
    """Does `code` still build `feature` — the name used somewhere, on no dropping line?

    A named rule rather than an inline search because it is the one judgement in this gate that is
    not the candidate's own arithmetic, and its truth table is what `tests/test_feature_cv_gate.py`
    drives directly."""
    if not feature:
        return False
    token = re.compile(r"(?<![\w])" + re.escape(feature) + r"(?![\w])")
    hits = [line for line in str(code or "").splitlines() if token.search(line)]
    return bool(hits) and not all(_DROPPING_RE.search(line) for line in hits)


def feature_cv_verdicts(code: str, stdout: str, direction: str = "min") -> list[dict]:
    """Every ledger row this node published, with the operator's verdict and whether the code kept
    it: `{feature, keep, delta, rule, kept_in_code}`. The gate's whole input, exposed as its own
    function so an operator can ask what a node claimed without asking what it was flagged for."""
    from looplab.search.operators import feature_engineering_verdicts, parse_feature_cv
    rows = feature_engineering_verdicts(parse_feature_cv(stdout), direction)
    for row in rows:
        row["kept_in_code"] = feature_is_kept(code, row["feature"])
    return rows


def feature_cv_findings(code: str, stdout: str, direction: str = "min") -> list[dict]:
    """The gate's findings, in the shape `reward_hack_suspected` already stores.

    One `feature_cv:kept_feature_failed_cv` per feature whose own CV comparison says it did not pay
    for itself and whose construction is still in the shipped code. Already namespaced (doc 25
    CT-10): the namespace is what `events/replay.py::is_hard_signal` keys gating on, so it is minted
    by the detector that knows what it found and never by a consumer three files away."""
    from looplab.trust.findings import finding
    out: list[dict] = []
    for row in feature_cv_verdicts(code, stdout, direction):
        if row["keep"] or not row["kept_in_code"]:
            continue
        out.append(finding(
            FEATURE_CV_NS + "kept_feature_failed_cv",
            f"{row['feature']}: the node's own CV ledger reports {row['delta']:+.6g} "
            f"({row['rule']} rule, with={row['with']:g} without={row['without']:g}) and the "
            "feature is still built",
            method="feature_cv", confidence=1.0))
    return out
