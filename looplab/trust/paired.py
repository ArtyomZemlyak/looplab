"""Paired comparison of two models on ONE shared evaluation set, clustered by user (doc 79).

The question a campaign asks is "is model T better than model C on the same frozen test, and by how
much?". When both were scored on the SAME queries, the per-query difference removes the between-query
variance an unpaired standard error keeps (`core/fitness.py` is that unpaired scale), and when one
user contributes several queries the queries are not independent — the standard error must be taken
over USERS (clusters), not queries. This module is that arithmetic and nothing else: pure Python, no
I/O, no numpy requirement (numpy is used only, when importable, to make the optional cluster
bootstrap fast), deterministic for a given seed.

Two metric shapes, the two a recommender/retrieval campaign reports (MiniOneRec 2026-10-13/14 used
both on its shared 40k test):

* ``mean`` — a per-query value (recall@K of that query, a weighted hit share, a loss): the metric is
  the plain mean over queries, the contrast the mean of per-query differences;
* ``ratio`` — a per-query numerator and denominator (hits and targets): the metric is
  ``sum(num) / sum(den)`` (a MICRO average), the contrast a difference of two ratios, its standard
  error by the delta method (linearization), so queries with many targets weigh in proportion.

Standard errors are cluster-robust with the usual small-sample factor ``G / (G - 1)``; intervals are
normal-approximation (``z = 1.959964``) with a two-sided normal p-value. Holm's step-down adjustment
is applied over a DECLARED family only — the caller names the family; this module never decides
which contrasts were pre-registered together.
"""
from __future__ import annotations

import math
import random
from typing import Iterable, Optional, Sequence

Z95 = 1.959963984540054
METHOD_VERSION = "looplab-paired-v1"
# A bootstrap over many clusters is G draws per replicate; in pure Python (no numpy) it is bounded so
# a request can never pin a server worker for minutes. With numpy the bound is far higher.
PURE_BOOTSTRAP_MAX_DRAWS = 4_000_000
NUMPY_BOOTSTRAP_MAX_DRAWS = 400_000_000


class PairedInputError(ValueError):
    """The vectors cannot be compared (lengths differ, no cluster, a zero denominator total, …)."""


def _cluster_index(clusters: Sequence) -> tuple[list[int], int]:
    """Map each query's cluster label to a dense index; returns (index per query, cluster count)."""
    seen: dict = {}
    index = []
    for label in clusters:
        index.append(seen.setdefault(label, len(seen)))
    return index, len(seen)


def _finite(values: Iterable[float], what: str) -> list[float]:
    out = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise PairedInputError(f"{what} holds a non-finite value")
        out.append(number)
    return out


def _cluster_se(influence: Sequence[float], index: Sequence[int], groups: int, scale: float) -> float:
    """Cluster-robust SE of a linear statistic: sqrt(G/(G-1) * sum_g (sum_{i in g} z_i)^2) / scale."""
    if groups < 2:
        return float("nan")
    sums = [0.0] * groups
    for z, g in zip(influence, index):
        sums[g] += z
    return math.sqrt(groups / (groups - 1) * math.fsum(s * s for s in sums)) / scale


def normal_p_two_sided(estimate: float, se: float) -> float:
    if not (math.isfinite(se) and se > 0):
        return 1.0 if estimate == 0 else (0.0 if math.isfinite(se) else float("nan"))
    return math.erfc(abs(estimate / se) / math.sqrt(2.0))


def _interval(estimate: float, se: float) -> dict:
    p = normal_p_two_sided(estimate, se)
    if math.isfinite(se):
        low, high = estimate - Z95 * se, estimate + Z95 * se
    else:
        low = high = float("nan")
    return {"estimate": estimate, "se": se, "ci95_low": low, "ci95_high": high, "p_two_sided": p}


def mean_with_ci(values: Sequence[float], clusters: Sequence) -> dict:
    """One model's mean metric with a user-clustered normal CI (the leaderboard cell)."""
    x = _finite(values, "values")
    if len(x) != len(clusters) or not x:
        raise PairedInputError("values and clusters must be non-empty and the same length")
    index, groups = _cluster_index(clusters)
    m = math.fsum(x) / len(x)
    se = _cluster_se([v - m for v in x], index, groups, len(x))
    out = _interval(m, se)
    out.update({"n": len(x), "clusters": groups})
    return out


def ratio_with_ci(num: Sequence[float], den: Sequence[float], clusters: Sequence) -> dict:
    """One model's micro ratio sum(num)/sum(den) with a user-clustered delta-method CI."""
    a, d = _finite(num, "numerator"), _finite(den, "denominator")
    if not (len(a) == len(d) == len(clusters)) or not a:
        raise PairedInputError("numerator, denominator and clusters must be the same length")
    total = math.fsum(d)
    if total <= 0:
        raise PairedInputError("denominator total must be positive")
    index, groups = _cluster_index(clusters)
    r = math.fsum(a) / total
    se = _cluster_se([ai - r * di for ai, di in zip(a, d)], index, groups, total)
    out = _interval(r, se)
    out.update({"n": len(a), "clusters": groups, "denominator_total": total})
    return out


def paired_mean_delta(treatment: Sequence[float], control: Sequence[float],
                      clusters: Sequence) -> dict:
    """mean(treatment - control) over aligned queries, SE clustered by `clusters`."""
    t, c = _finite(treatment, "treatment"), _finite(control, "control")
    if not (len(t) == len(c) == len(clusters)) or not t:
        raise PairedInputError("treatment, control and clusters must be non-empty and aligned")
    index, groups = _cluster_index(clusters)
    diff = [a - b for a, b in zip(t, c)]
    delta = math.fsum(diff) / len(diff)
    se = _cluster_se([v - delta for v in diff], index, groups, len(diff))
    out = _interval(delta, se)
    out.update({"n": len(t), "clusters": groups,
                "treatment_value": math.fsum(t) / len(t), "control_value": math.fsum(c) / len(c)})
    return out


def paired_ratio_delta(num_t: Sequence[float], den_t: Sequence[float],
                       num_c: Sequence[float], den_c: Sequence[float],
                       clusters: Sequence) -> dict:
    """sum(num_t)/sum(den_t) - sum(num_c)/sum(den_c), delta-method SE clustered by `clusters`.

    With a shared denominator (the usual case: the same targets per query) this reduces exactly to
    the per-query hit difference over the shared target total."""
    nt, dt = _finite(num_t, "treatment numerator"), _finite(den_t, "treatment denominator")
    nc, dc = _finite(num_c, "control numerator"), _finite(den_c, "control denominator")
    n = len(nt)
    if not (n and len(dt) == len(nc) == len(dc) == len(clusters) == n):
        raise PairedInputError("numerators, denominators and clusters must be non-empty and aligned")
    total_t, total_c = math.fsum(dt), math.fsum(dc)
    if total_t <= 0 or total_c <= 0:
        raise PairedInputError("denominator totals must be positive")
    rt, rc = math.fsum(nt) / total_t, math.fsum(nc) / total_c
    index, groups = _cluster_index(clusters)
    influence = [(a - rt * b) / total_t - (x - rc * y) / total_c
                 for a, b, x, y in zip(nt, dt, nc, dc)]
    se = _cluster_se(influence, index, groups, 1.0)
    out = _interval(rt - rc, se)
    out.update({"n": n, "clusters": groups, "treatment_value": rt, "control_value": rc,
                "denominator_total": total_t if total_t == total_c else None})
    return out


def holm(pvalues: Sequence[Optional[float]]) -> list[Optional[float]]:
    """Holm step-down adjusted p-values, in the input order; a None/NaN entry stays None and is
    not counted in the family size."""
    ranked = sorted(((p, i) for i, p in enumerate(pvalues)
                     if p is not None and math.isfinite(p)), key=lambda pair: pair[0])
    m = len(ranked)
    out: list[Optional[float]] = [None] * len(pvalues)
    running = 0.0
    for rank, (p, i) in enumerate(ranked):
        running = max(running, min(1.0, (m - rank) * p))
        out[i] = running
    return out


def _cluster_sums(columns: Sequence[Sequence[float]], index: Sequence[int], groups: int):
    sums = [[0.0] * groups for _ in columns]
    for col, acc in zip(columns, sums):
        for value, g in zip(col, index):
            acc[g] += value
    return sums


def cluster_bootstrap(kind: str, vectors: dict, clusters: Sequence, *, replicates: int = 2000,
                      seed: int = 0) -> dict:
    """Percentile 95% interval of the paired contrast by resampling CLUSTERS with replacement.

    `vectors` is ``{"t": [...], "c": [...]}`` for ``kind="mean"`` and ``{"nt", "dt", "nc", "dc"}``
    for ``kind="ratio"``. Returns ``{"ci95_low", "ci95_high", "replicates", "seed", "engine"}`` or
    ``{"unavailable": reason}`` when the request exceeds the draw bound for the available engine."""
    if replicates < 100 or replicates > 100_000:
        raise PairedInputError("replicates must be between 100 and 100000")
    index, groups = _cluster_index(clusters)
    if groups < 2:
        return {"unavailable": "fewer than two clusters"}
    if kind == "mean":
        cols = [vectors["t"], vectors["c"], [1.0] * len(clusters)]
    elif kind == "ratio":
        cols = [vectors["nt"], vectors["dt"], vectors["nc"], vectors["dc"]]
    else:
        raise PairedInputError(f"unknown metric kind {kind!r}")
    sums = _cluster_sums([_finite(col, "bootstrap vector") for col in cols], index, groups)
    draws = replicates * groups
    try:
        import numpy as np  # optional: only the speed of this one instrument depends on it
    except ImportError:
        np = None
    if np is not None and draws <= NUMPY_BOOTSTRAP_MAX_DRAWS:
        rng = np.random.default_rng(seed)
        arr = np.asarray(sums, dtype=float)
        stats = np.empty(replicates)
        chunk = max(1, min(replicates, 20_000_000 // groups))
        for start in range(0, replicates, chunk):
            stop = min(replicates, start + chunk)
            pick = rng.integers(0, groups, size=(stop - start, groups))
            # A replicate's total is the sum of its drawn clusters' sums: count each cluster's draws.
            counts = np.zeros((stop - start, groups))
            rows = np.repeat(np.arange(stop - start), groups)
            np.add.at(counts, (rows, pick.ravel()), 1.0)
            totals = counts @ arr.T
            if kind == "mean":
                stats[start:stop] = (totals[:, 0] - totals[:, 1]) / totals[:, 2]
            else:
                with np.errstate(divide="ignore", invalid="ignore"):
                    stats[start:stop] = totals[:, 0] / totals[:, 1] - totals[:, 2] / totals[:, 3]
        stats = stats[np.isfinite(stats)]
        low, high = (float(v) for v in np.quantile(stats, [0.025, 0.975]))
        return {"ci95_low": low, "ci95_high": high, "replicates": int(stats.size), "seed": seed,
                "engine": "numpy"}
    if draws > PURE_BOOTSTRAP_MAX_DRAWS:
        return {"unavailable": (f"{replicates} replicates x {groups} clusters exceeds the "
                                f"pure-Python bound ({PURE_BOOTSTRAP_MAX_DRAWS} draws); install "
                                "numpy on the server or ask for fewer replicates")}
    rng = random.Random(seed)
    values = []
    width = len(sums)
    for _ in range(replicates):
        totals = [0.0] * width
        for _ in range(groups):
            g = rng.randrange(groups)
            for k in range(width):
                totals[k] += sums[k][g]
        if kind == "mean":
            stat = (totals[0] - totals[1]) / totals[2]
        else:
            if totals[1] <= 0 or totals[3] <= 0:
                continue
            stat = totals[0] / totals[1] - totals[2] / totals[3]
        values.append(stat)
    values.sort()
    if not values:
        return {"unavailable": "no finite bootstrap replicate"}

    def q(p):
        pos = p * (len(values) - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (pos - lo)
    return {"ci95_low": q(0.025), "ci95_high": q(0.975), "replicates": len(values), "seed": seed,
            "engine": "python"}


__all__ = ["METHOD_VERSION", "PairedInputError", "Z95", "cluster_bootstrap", "holm",
           "mean_with_ci", "normal_p_two_sided", "paired_mean_delta", "paired_ratio_delta",
           "ratio_with_ci"]
