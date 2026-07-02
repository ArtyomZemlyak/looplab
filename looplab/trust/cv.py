"""Consistent evaluation harness + cross-validation (I8, ADR-15) — pure-Python.

The key property is *consistency*: every candidate is scored on the **same** folds,
so node-to-node comparisons in the search tree are valid. Includes a custom
purged/embargoed walk-forward splitter for temporal tasks (no library models the
look-ahead gap — that's our code, per ADR-15).
"""
from __future__ import annotations

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
