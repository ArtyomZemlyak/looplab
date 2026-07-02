"""Data profiler (I16, ADR-15) — pure-Python JSON profile that doubles as the
leakage front-end. Per column: count, missing, dtype, cardinality, numeric stats,
and quality flags (constant, high-missing). No pandas/numpy dependency.
"""
from __future__ import annotations

from typing import Any


def _is_number(v: Any) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    # NaN/inf poison every stat (mean/std -> NaN) and hide missingness; treat them as non-numeric so
    # the column is counted as missing rather than silently corrupting the profile.
    return v == v and v not in (float("inf"), float("-inf"))


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
