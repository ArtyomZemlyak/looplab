"""Canonical governed experiment-action helpers shared by admission and execution."""
from __future__ import annotations

import math
from typing import Optional


def effective_researcher_eval_timeout(engine, idea) -> Optional[float]:
    """Return the governed, finite and hard-clamped per-node timeout override."""
    # identity must describe the EXECUTED action, not an untrusted model request.
    # RepoTask profiles own their timeout, and a locked researcher override is ignored by execution;
    # only the finite positive solution.py override that crosses governance is an action axis.
    if idea is None or getattr(engine, "_eval_spec", None):
        return None
    may = getattr(engine, "_agent_may", None)
    if not callable(may) or not may("researcher", "timeout"):
        return None
    try:
        timeout = float(getattr(idea, "eval_timeout", None))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    # Settings validates this boundary, but Engine is also a public library seam and may be built
    # directly. Missing/invalid direct-construction state therefore fails safe to the shipped one-hour
    # ceiling instead of letting an untrusted Idea disable the bound with NaN/inf/a typo.
    try:
        ceiling = float(getattr(engine, "max_eval_timeout", 3600.0))
    except (TypeError, ValueError, OverflowError):
        ceiling = 3600.0
    if not math.isfinite(ceiling) or ceiling <= 0:
        ceiling = 3600.0
    return min(timeout, ceiling)
