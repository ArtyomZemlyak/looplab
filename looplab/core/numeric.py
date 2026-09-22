"""Shared numeric primitives: the median, the numeric subset of a param dict, the IDW k-NN core,
and the human SIZE grammar (`parse_mem_bytes` / `size_bytes_or_error`, see the block above them).

Neither has anything to do with the event log, yet both lived in `events/digest.py` (doc 25 XP-12),
and `runtime/proxy.py` imported `events` for the sole purpose of reaching a math function — the one
runtime -> events edge in the whole import graph, existing only for that. `events` is documented as
event-log projections; a generic estimator sitting there also meant every future consumer had to
depend on the projection layer to do arithmetic.

`events/digest.py` re-exports both names, so the historical import path keeps resolving and the
digest's own `param_distance` (a run-SIMILARITY primitive, which is a projection concern) stays
where it is.
"""
from __future__ import annotations

import math


def median(values) -> float:
    """The median of `values`, sorted. Raises IndexError on an EMPTY input — deliberately, because
    both callers reduce a set they have already proved non-empty and a 0.0 there would be a reading
    nothing measured.

    Shared by `tools/log_tools.py::bucket_series` (the judge-facing per-bucket median) and
    `engine/train_monitor.py` (the loss-trajectory veto's per-window median and its noise floor).
    Those two were byte-identical copies, and they reduce the SAME data one trust tier apart: a
    window median that disagreed with the bucket median of the same log would put the deterministic
    veto and the number the judge reads off `metric_series` in silent contradiction.

    NOTE two further copies exist and are deliberately NOT this function, because they answer the
    empty case differently and their callers depend on that: `search/concept_analytics.py::_median`
    returns None (an un-scored concept has no baseline) and `engine/speculation.py::_median` returns
    0.0 (an unmeasured build width falls back to the AUTO default).
    """
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def numeric_params(params: dict, keys=None) -> dict:
    """The NUMERIC (int/float — bools included, matching the historical isinstance check) subset of a
    param dict, coerced to float. `keys` optionally restricts to a key set (e.g. the search bounds).
    Shared by the novelty gate, the surrogate and the panel so "numeric params" means the same thing
    everywhere. NOTE: search/proxy.py deliberately keeps its own try/float() variant — it also
    accepts numeric STRINGS, which this helper must not start doing."""
    return {k: float(v) for k, v in params.items()
            if (keys is None or k in keys) and isinstance(v, (int, float))}


def knn_idw(pairs, k: int):
    """Inverse-distance-weighted k-NN over pre-computed `(distance, value)` pairs — the shared CORE
    of the three empirical predictors (search/surrogate, search/panel, search/proxy). The callers
    keep their own (deliberately different) neighbour-eligibility and distance computations; only
    the rank / zero-distance short-circuit / weighting steps are unified here, so those can't
    silently drift apart again.

    Returns `(prediction, nearest_distance)`, or None when `pairs` is empty (the caller's abstain
    path). A zero-distance sample short-circuits to that sample's value with nearest=0.0 (ties keep
    input order — `sorted` is stable, exactly like every pre-extraction copy)."""
    if not pairs:
        return None
    nn = sorted(pairs, key=lambda t: t[0])[: max(1, k)]
    # Exact-match short-circuit scans the WHOLE top-k, not just nn[0]: a NaN distance (reachable —
    # the proxy coerces string params, and a float('nan') param value is isinstance-numeric
    # everywhere) sorts unpredictably and can sit AHEAD of a genuine 0.0; checking only nn[0]
    # would then fall through to the 1/d weighting and divide by that hidden zero. With no zero
    # present, a NaN distance degrades to a NaN prediction exactly like every pre-extraction copy.
    for d, v in nn:
        if d == 0.0:
            return v, 0.0
    nearest = nn[0][0]
    wsum = sum(1.0 / d for d, _ in nn)
    return sum((1.0 / d) * v for d, v in nn) / wsum, nearest


def euclidean(a: dict, b: dict, keys) -> float:
    """Euclidean distance between two param dicts over *keys*, which both must contain.

    The three empirical predictors (`search/surrogate`, `search/panel`, `search/proxy`) each wrote
    this loop out (doc 25 SE-15). Their neighbour-ELIGIBILITY rules differ deliberately — full-bounds
    dimensionality, target-subspace containment, any shared key — and those stay at the call sites,
    documented, because they are what each predictor means by "comparable". Only the arithmetic is
    shared, so the three cannot drift on the distance itself while claiming to differ on eligibility.

    Unnormalized on purpose: every caller has already projected through `numeric_params` (or, for the
    proxy, its own string-tolerant variant) into the same param space, and normalizing here would
    silently change what `knn_idw` weights.
    """
    return math.sqrt(sum((a[key] - b[key]) ** 2 for key in keys))


# THE HUMAN SIZE GRAMMAR ("8g", "512m", "1073741824", 4096), in core since review 2026-09-22 (CORE-05)
# because two layers must read it the SAME way: `core/config.py::Settings` REFUSES a size it cannot
# read, and `runtime/sandbox.py` (which re-exports `parse_mem_bytes`) ENFORCES what it reads. Before,
# only the runtime read it — after `Settings` had accepted anything — and an unreadable value such as
# `sandbox_memory_local="8GB"` (docker's own spelling) turned the RLIMIT_AS host-OOM guard OFF with no
# word anywhere. Suffixes k/m/g/t are powers of 1024, matching `docker run --memory`; case and
# surrounding whitespace are ignored. Deliberately NOT widened to "gb"/"gib": a spelling this grammar
# does not read is refused where the operator typed it, not silently reinterpreted.
_SIZE_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}


def size_bytes_or_error(spec) -> int:
    """STRICT: the byte count `spec` names — 0 for an explicit OFF (`None`, `""`, `0`) — or
    ValueError for anything that is not a size under the grammar above: an unknown suffix ("8GB",
    "2 GiB"), a negative, a non-finite or overflowing value, words."""
    if spec is None:
        return 0
    if isinstance(spec, (int, float)):
        # `int(True)` was always 1 here; kept so the tolerant reader below stays byte-identical.
        if isinstance(spec, float) and not math.isfinite(spec):
            raise ValueError(f"{spec!r} is not a finite size")
        n = int(spec)
        if n < 0:
            raise ValueError(f"{spec!r} is a negative size")
        return n
    s = str(spec).strip().lower()
    if not s:
        return 0
    mult = 1
    if s[-1] in _SIZE_UNITS:
        mult = _SIZE_UNITS[s[-1]]
        s = s[:-1].strip()
    try:
        value = float(s) * mult
    except ValueError:
        raise ValueError(
            f"{spec!r} is not a size: use a byte count or a k/m/g/t suffix, e.g. '8g'") from None
    if not math.isfinite(value):
        raise ValueError(f"{spec!r} is not a finite size")
    if value < 0:
        raise ValueError(f"{spec!r} is a negative size")
    return int(value)


def parse_mem_bytes(spec) -> int | None:
    """Parse a human memory size ("8g", "512m", "1073741824", 4096) to a positive int byte count, or
    None for "" / 0 / an unparseable value (cap disabled). Suffixes k/m/g/t are powers of 1024, matching
    `docker run --memory`.

    TOTAL on purpose — it never raises, because a live eval must not crash on a value that got past
    its boundary. The boundary is where the REFUSAL lives: `Settings` refuses an unreadable
    `sandbox_memory_local`/`sandbox_fsize_local` at construction (via `size_bytes_or_error`, the
    same grammar), and `runtime/sandbox.py::readonly_rootfs_argv` refuses its own. So None here now
    means "the operator asked for no cap", not "the operator's cap was unreadable"."""
    try:
        n = size_bytes_or_error(spec)
    except (ValueError, OverflowError):
        # OverflowError: `int(float("1e308") * 1024**3)`-scale products are caught as non-finite
        # above; this is the belt for anything `int()` still refuses.
        return None
    return n if n > 0 else None
