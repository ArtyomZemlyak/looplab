"""Canonical governed experiment-action helpers shared by admission and execution."""
from __future__ import annotations

import math
from typing import Optional


def effective_researcher_eval_timeout(engine, idea) -> Optional[float]:
    """Return the finite per-node timeout override that this engine will actually honor."""
    # CODEX AGENT: identity must describe the EXECUTED action, not an untrusted model request.
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
    return timeout if math.isfinite(timeout) and timeout > 0 else None
