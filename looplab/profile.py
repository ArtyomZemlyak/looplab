"""Data profiler (I16, ADR-15) — pure-Python JSON profile that doubles as the
leakage front-end. Per column: count, missing, dtype, cardinality, numeric stats,
and quality flags (constant, high-missing). No pandas/numpy dependency.
"""
from __future__ import annotations

from typing import Any


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def profile_column(values: list) -> dict:
    n = len(values)
    nonnull = [v for v in values if v is not None]
    missing = n - len(nonnull)
    numeric = bool(nonnull) and all(_is_number(v) for v in nonnull)
    col: dict[str, Any] = {
        "count": n,
        "n_missing": missing,
        "missing_frac": round(missing / n, 6) if n else 0.0,
        "n_unique": len(set(nonnull)),
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
