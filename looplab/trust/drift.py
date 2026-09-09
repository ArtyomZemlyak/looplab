"""Distribution-shift detector (docs/BACKLOG.md §15) — deterministic, pure-Python, ADVISORY.

Every other rung in this package answers "is this number honest?"; none of them answered "is the
data the same data?". A run that scores worse on its held-out split cannot today tell a SHIFTED
input from a worse model, because nothing ever compared the deployment distribution against the
training one. This module is that comparison, and it is deliberately three cheap classical
statistics rather than a model:

  * PSI (population stability index) over `BINS` quantile bins cut from the REFERENCE column —
    the standard MLOps bar, 0.10 "moderate" / 0.25 "significant" (Siddiqi, *Credit Risk
    Scorecards*), which is where `PSI_ADVISORY` comes from;
  * the two-sample Kolmogorov–Smirnov statistic D, reported BESIDE PSI and not instead of it. PSI
    is unbounded and its magnitude depends on the binning and on the `_EPS` floor an empty bin
    hits, so it says THAT a column moved and not HOW FAR; D is a bounded 0..1 share an operator can
    read across columns. Which one is the more sensitive was measured rather than assumed — over
    Gaussian location shifts at n=400 neither fires at 0.5σ and both fire at 1.0σ, and past ~0.6σ
    PSI is the larger of the two, so in the ordinary case PSI is what trips the OR. D earns its
    place by staying interpretable there, and by still answering when the reference's quantiles
    collapse into two or three bins;
  * for a categorical column, total variation distance over the category frequencies plus the
    share of the current sample whose category the reference never had — the shape PSI degenerates
    into when the values are not ordered.

WHAT DECIDES A COLUMN'S TYPE IS `core/profile.py`, not a second rule written here. The profiler is
already the deterministic front-end for the leakage gate; asking it (`profile_column(...)["dtype"]`)
is what keeps one answer to "is this column numeric" across the profile an operator reads, the
leakage verdicts, and this. The READERS are `adapters/perception.py`'s — a caller hands this module
`{column: values}` dicts, which is exactly what `tabular_columns` returns and what `leakage_inputs`
rows convert into via `rows_to_columns`. No file is opened here.

IT RECORDS AND NOTHING ELSE. `engine/audit.py::_record_distribution_shift` appends the verdict as a
`data_shift` event (diagnostic: no fold handler, nothing keys on its position) and no selection, no
gate and no champion caveat reads it. Shift is not misconduct — a deployment sample is SUPPOSED to
differ from a training one on most real tasks — so a detector that could abort a run would be
refusing the normal case. Whether a deterministic flag may ever change selection is
`Settings.trust_gate`'s business, and this rung is not in it; `trust/leakage.py::categorical_leak`
is the sibling that made the same choice for the same reason.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

from looplab.core.profile import profile_column

BINS = 10                     # quantile bins cut from the reference column (the MLOps convention)
MIN_ROWS = 20                 # below this a "shift" is sampling noise; the column ABSTAINS instead
PSI_ADVISORY = 0.25           # "significant" on the standard PSI scale (0.10 = moderate)
KS_ADVISORY = 0.25            # D that far apart is visible in a QQ plot at these sample sizes
TVD_ADVISORY = 0.25           # total variation distance between two category frequency vectors
MAX_CATEGORIES = 50           # a higher-cardinality categorical is an id-like column: abstain
_EPS = 1e-6                   # keeps an empty bin's log finite (the standard PSI smoothing)


def _numbers(values: Sequence) -> list[float]:
    """The finite numeric cells of a column, in order. A NaN/inf is missing data, not a value —
    `core/profile.py` treats it the same way, and a NaN in either sample poisons every statistic
    below (`abs(NaN) >= threshold` is False, so a shifted column would read as unshifted)."""
    out: list[float] = []
    for v in values:
        if isinstance(v, bool):     # bools are numbers to Python and categories to a data profile
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _quantile_edges(ref: list[float], bins: int) -> list[float]:
    """Interior bin edges at the reference's quantiles, de-duplicated.

    Cut from the REFERENCE alone, deliberately: PSI asks "where did the current sample land in the
    bins the training data defined". Edges derived from the pooled sample would move with the very
    thing being measured, and a shift would partly cancel itself out."""
    s = sorted(ref)
    edges: list[float] = []
    for i in range(1, max(2, int(bins))):
        q = i / bins
        pos = q * (len(s) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(s) - 1)
        edge = s[lo] + (s[hi] - s[lo]) * (pos - lo)
        if not edges or edge > edges[-1]:
            edges.append(edge)      # a tied quantile (a spiky column) collapses to fewer bins
    return edges


def _bin_shares(values: list[float], edges: list[float]) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for v in values:
        idx = 0
        while idx < len(edges) and v > edges[idx]:
            idx += 1
        counts[idx] += 1
    n = len(values) or 1
    return [c / n for c in counts]


def psi(reference: Sequence, current: Sequence, bins: int = BINS) -> float:
    """Population stability index of `current` against `reference`, over `bins` reference quantiles.

    Sum over bins of `(cur - ref) * ln(cur / ref)`, each share floored at `_EPS` so a bin the current
    sample never lands in contributes a large finite number instead of an infinity."""
    ref, cur = _numbers(reference), _numbers(current)
    if len(ref) < 2 or not cur:
        return 0.0
    edges = _quantile_edges(ref, bins)
    r = _bin_shares(ref, edges)
    c = _bin_shares(cur, edges)
    total = 0.0
    for rs, cs in zip(r, c):
        rs, cs = max(rs, _EPS), max(cs, _EPS)
        total += (cs - rs) * math.log(cs / rs)
    return total


def ks_statistic(reference: Sequence, current: Sequence) -> float:
    """Two-sample Kolmogorov–Smirnov D: the largest gap between the two empirical CDFs.

    The statistic only, never a p-value: a p-value here would be read as a decision, and at 200
    sampled rows against a real dataset it would flag almost everything. D is a SIZE."""
    ref, cur = sorted(_numbers(reference)), sorted(_numbers(current))
    if not ref or not cur:
        return 0.0
    i = j = 0
    d = 0.0
    while i < len(ref) and j < len(cur):
        x = min(ref[i], cur[j])
        while i < len(ref) and ref[i] <= x:
            i += 1
        while j < len(cur) and cur[j] <= x:
            j += 1
        d = max(d, abs(i / len(ref) - j / len(cur)))
    return d


def _frequencies(values: Sequence) -> dict:
    out: dict = {}
    for v in values:
        if v is None:
            continue
        key = v if v.__hash__ is not None else repr(v)   # nested JSON cells are unhashable
        out[key] = out.get(key, 0) + 1
    return out


def category_shift(reference: Sequence, current: Sequence) -> tuple[float, float]:
    """`(total variation distance, unseen share)` between two categorical samples.

    TVD is half the L1 distance between the frequency vectors — 0 identical, 1 disjoint. The second
    number is the share of the CURRENT sample whose category the reference never contained, reported
    separately because it is the half an operator can act on (a new category is a schema change, not
    a re-weighting), and because TVD alone cannot tell those two apart."""
    r, c = _frequencies(reference), _frequencies(current)
    rn, cn = sum(r.values()), sum(c.values())
    if not rn or not cn:
        return 0.0, 0.0
    tvd = 0.5 * sum(abs(c.get(k, 0) / cn - r.get(k, 0) / rn) for k in set(r) | set(c))
    unseen = sum(n for k, n in c.items() if k not in r) / cn
    return tvd, unseen


def column_shift(name: str, reference: Sequence, current: Sequence) -> dict:
    """One column's verdict. ABSTAINS (`checked: False` + a reason) rather than guessing when the
    comparison is not meaningful: too few rows either side, a column that is numeric in one sample
    and categorical in the other (that is a SCHEMA difference, and saying so is the honest answer),
    or an id-like categorical whose every value is unique."""
    ref, cur = list(reference), list(current)
    out: dict = {"column": name, "n_reference": len(ref), "n_current": len(cur)}
    if len(ref) < MIN_ROWS or len(cur) < MIN_ROWS:
        return {**out, "checked": False, "shifted": False,
                "reason": f"fewer than {MIN_ROWS} rows on one side"}
    # ONE dtype rule for the whole codebase: the profiler's (see the module docstring).
    r_type = profile_column(ref)["dtype"]
    c_type = profile_column(cur)["dtype"]
    if r_type != c_type:
        return {**out, "checked": False, "shifted": False,
                "reason": f"dtype differs: reference {r_type}, current {c_type}"}
    if r_type == "numeric":
        p = psi(ref, cur)
        d = ks_statistic(ref, cur)
        return {**out, "checked": True, "kind": "numeric",
                "psi": round(p, 6), "ks": round(d, 6),
                "shifted": bool(p >= PSI_ADVISORY or d >= KS_ADVISORY)}
    n_ref_categories = len(_frequencies(ref))
    if n_ref_categories > MAX_CATEGORIES:
        return {**out, "checked": False, "shifted": False,
                "reason": f"{n_ref_categories} categories: id-like, not a distribution"}
    tvd, unseen = category_shift(ref, cur)
    return {**out, "checked": True, "kind": "categorical",
            "tvd": round(tvd, 6), "unseen_share": round(unseen, 6),
            "shifted": bool(tvd >= TVD_ADVISORY or unseen >= TVD_ADVISORY)}


def rows_to_columns(rows: Sequence, names: Optional[Sequence[str]] = None) -> dict[str, list]:
    """`[[cell, …], …]` -> `{column: values}` — the shape `leakage_inputs()` already publishes.

    Positional, because that is all a row list carries: column `i` of the train rows is compared
    against column `i` of the test rows. Ragged rows are tolerated (a short row contributes to the
    columns it has), which is why this cannot raise on a dataset the leakage detectors accepted."""
    cols: dict[str, list] = {}
    for row in rows:
        if isinstance(row, dict):          # a row-dict list (JSONL-shaped) keys itself
            for k, v in row.items():
                cols.setdefault(str(k), []).append(v)
            continue
        try:
            cells = list(row)
        except TypeError:
            continue
        for i, v in enumerate(cells):
            key = str(names[i]) if names is not None and i < len(names) else f"c{i}"
            cols.setdefault(key, []).append(v)
    return cols


def distribution_shift(reference: dict[str, list], current: dict[str, list],
                       *, source: str = "") -> dict:
    """The detector: compare two `{column: values}` samples and report, column by column.

    Only columns present on BOTH sides are compared; the names that appear on one side alone are
    reported as `only_reference`/`only_current` rather than silently dropped, because a column that
    exists in training and not at inference is the most consequential shift there is. `shift` is the
    OR over the compared columns and is ADVISORY — see the module docstring for why nothing gates on
    it. `checked` is False when no column could be compared at all, which is not evidence of
    stability."""
    ref_names = [str(k) for k in reference]
    cur_names = {str(k) for k in current}
    shared = [n for n in ref_names if n in cur_names]
    columns = [column_shift(n, reference[n], current[n]) for n in shared]
    checked = [c for c in columns if c.get("checked")]
    return {
        "detector": "distribution_shift",
        "source": source,
        "checked": bool(checked),
        "shift": any(c.get("shifted") for c in checked),
        "n_columns": len(columns),
        "n_shifted": sum(1 for c in checked if c.get("shifted")),
        "only_reference": [n for n in ref_names if n not in cur_names],
        "only_current": sorted(n for n in cur_names if n not in set(ref_names)),
        "columns": columns,
    }
