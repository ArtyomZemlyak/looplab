"""Closed operator declaration for optional native script pilot selection."""
from __future__ import annotations

import math
from looplab.core.envsafe import validate_env_map


def normalize_pilot(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'env', 'timeout_seconds', 'fidelity_review'}:
        raise ValueError('best_of_n_pilot needs env, timeout_seconds and fidelity_review')
    env, error = validate_env_map('best_of_n_pilot.env', value['env'], cap=16)
    timeout, review = value['timeout_seconds'], value['fidelity_review']
    try:
        finite = type(timeout) in (int, float) and math.isfinite(float(timeout))
    except (ValueError, OverflowError):
        finite = False
    if (error or not env or not finite
            or not .1 <= timeout <= 300 or not isinstance(review, str)
            or not review.strip() or len(review) > 1000):
        raise ValueError(error or 'invalid best_of_n_pilot bounds or fidelity review')
    try:
        review.encode('utf-8')
        for val in env.values():
            val.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('pilot declaration must be valid UTF-8') from exc
    return {'env': env, 'timeout_seconds': float(timeout), 'fidelity_review': review.strip()}
