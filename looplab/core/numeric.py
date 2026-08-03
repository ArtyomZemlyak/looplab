"""Parameter-space numeric primitives: the numeric subset of a param dict, and the IDW k-NN core.

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
