"""Data profiler (I16, ADR-15) — pure-Python JSON profile that doubles as the
leakage front-end. Per column: count, missing, dtype, cardinality, numeric stats,
and quality flags (constant, high-missing). No pandas/numpy dependency.
"""
from __future__ import annotations

from typing import Any

from looplab.core.fitness import is_usable_metric

# `fitness.is_usable_metric` under the profiler's local name (doc 25 CO-09): a real, finite scalar,
# bools excluded, arbitrary-precision ints rejected via `float()`. It was written out here a second
# time, character-different but rule-identical. The REASONS stay written down, because they are
# profiler-specific and the shared predicate cannot carry them:
#
# NaN/inf poison every stat (mean/std -> NaN) and hide missingness; treat them as non-numeric so
# the column is counted as missing rather than silently corrupting the profile.
#
# `float(v)` is the honest form of that test, because Python ints are arbitrary-precision and JSON
# integers are unbounded: a cell like 10**400 is a FINITE int that sails past any NaN/inf check,
# and then `profile_column`'s `sum(nonnull)/len(nonnull)` mean (and the `** 0.5` std term) raises
# OverflowError — "integer division result too large for a float" — taking the whole profiler and
# the leakage front-end down over one oversized cell. That is the same stat-poisoning this guard
# exists to reject, so it is rejected here and the column degrades to categorical.
_is_number = is_usable_metric


def _n_unique(nonnull: list) -> int:
    try:
        return len(set(nonnull))
    except TypeError:
        # Nested lists/dicts (a JSON dataset column) are unhashable; fall back to a repr-based count
        # instead of aborting the whole run during optional profiling.
        return len({repr(v) for v in nonnull})


def profile_column(values: list) -> dict:
    n = len(values)
    # A non-finite float (NaN/inf) is missing data, not a valid value.
    nonnull = [v for v in values if v is not None
               and not (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))))]
    missing = n - len(nonnull)
    numeric = bool(nonnull) and all(_is_number(v) for v in nonnull)
    col: dict[str, Any] = {
        "count": n,
        "n_missing": missing,
        "missing_frac": round(missing / n, 6) if n else 0.0,
        "n_unique": _n_unique(nonnull),
        "dtype": "numeric" if numeric else "categorical",
    }
    if numeric:
        m = sum(nonnull) / len(nonnull)
        col["min"] = min(nonnull)
        col["max"] = max(nonnull)
        col["mean"] = m
        col["std"] = (sum((x - m) ** 2 for x in nonnull) / len(nonnull)) ** 0.5
    col["constant"] = col["n_unique"] <= 1
    col["high_missing"] = col["missing_frac"] >= 0.5
    return col


def profile_dataset(columns: dict[str, list]) -> dict:
    """Profile every column. Returns {col_name: profile}."""
    return {name: profile_column(vals) for name, vals in columns.items()}
