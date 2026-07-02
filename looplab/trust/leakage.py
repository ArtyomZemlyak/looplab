"""Leakage detectors (I9, ADR-15) — the differentiator, pure-Python (no deps).

No library models ML-pipeline leakage, so these are custom. Each returns a small
dict so verdicts attach to nodes/events. Datasets are plain dict[col, list] +
explicit row lists / timestamp lists — adapter-agnostic.
"""
from __future__ import annotations

import re
from typing import Sequence


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    # Compare the overlapping prefix when columns are ragged (a dropped NaN row, a mismatched slice)
    # rather than silently returning 0.0 — returning 0 would HIDE a near-perfect proxy that happens to
    # be one row short, letting a leaking solution through the hard gate.
    n = min(len(a), len(b))
    if n < 3:   # a 2-point overlap is always perfectly collinear -> meaningless |r|==1.0 against the hard gate
        return 0.0
    a, b = list(a)[:n], list(b)[:n]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0.0 or vb == 0.0:
        return 0.0
    return cov / (va * vb) ** 0.5


def train_test_contamination(train_rows: list, test_rows: list) -> dict:
    """Detect identical rows shared between train and test splits."""
    train = {tuple(r) for r in train_rows}
    dups = [r for r in test_rows if tuple(r) in train]
    frac = len(dups) / len(test_rows) if test_rows else 0.0
    return {"detector": "train_test_contamination",
            "leak": len(dups) > 0, "duplicates": len(dups), "fraction": round(frac, 6)}


def target_leakage(features: dict[str, list[float]], target: list[float],
                   threshold: float = 0.98) -> dict:
    """Flag feature columns near-perfectly correlated with the target (a proxy/leak)."""
    flagged = {}
    for name, col in features.items():
        r = _pearson(col, target)
        if abs(r) >= threshold:
            flagged[name] = round(r, 6)
    return {"detector": "target_leakage", "leak": bool(flagged),
            "threshold": threshold, "flagged": flagged}


_FIT_RE = re.compile(r"\.(fit|fit_transform)\s*\(([^)]*)\)")
_SPLIT_RE = re.compile(r"train_test_split\s*\(|KFold|StratifiedKFold|\.split\s*\(")


def code_leakage_scan(code: str) -> dict:
    """I3 data-centric: static-dataflow-lite scan of solution CODE for train->test information flow
    (beyond exact-row contamination). Flags the classic anti-patterns: fitting a preprocessor on the
    FULL data before the split, and calling .fit() on test data. Heuristic + dependency-free — a
    surfaced suspicion for the operator, not a hard gate."""
    flags: list[dict] = []
    lines = code.splitlines()
    split_at = next((i for i, l in enumerate(lines) if _SPLIT_RE.search(l)), None)
    for i, line in enumerate(lines):
        m = _FIT_RE.search(line)
        if not m:
            continue
        arg = m.group(2).lower()
        if "test" in arg or "x_test" in arg or "val" in arg:
            flags.append({"signal": "fit_on_test", "line": i + 1, "code": line.strip()[:90]})
        elif split_at is not None and i < split_at and "train" not in arg:
            # a fit/fit_transform on (apparently full) data BEFORE the split leaks test statistics
            flags.append({"signal": "fit_before_split", "line": i + 1, "code": line.strip()[:90]})
    return {"detector": "code_leakage", "leak": bool(flags), "flags": flags}


def temporal_leakage(train_timestamps: list[float], test_timestamps: list[float]) -> dict:
    """For a forward (train-before-test) split, flag train rows at/after the test
    cutoff — i.e. training on future information."""
    if not train_timestamps or not test_timestamps:
        return {"detector": "temporal_leakage", "leak": False, "overlap": 0}
    cutoff = min(test_timestamps)
    overlap = sum(1 for t in train_timestamps if t >= cutoff)
    return {"detector": "temporal_leakage", "leak": overlap > 0,
            "cutoff": cutoff, "overlap": overlap}
