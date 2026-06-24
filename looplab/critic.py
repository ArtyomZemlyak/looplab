"""C4 · Independent critic (ADR-7). A cheap, execution-free critic pass over a generated solution
BEFORE it's trusted: does the code plausibly do what the Idea claims, and is it not an obvious no-op?
Catches the failure modes a validator's syntax check misses — a model that returns a stub, hard-codes
the metric, or ignores the requested hyperparameters. Audit-only (surfaced in the Trust panel via the
same reward_hack_suspected event); never changes selection. Ties to B5.
"""
from __future__ import annotations

import re

from .models import Idea


def critique(idea: Idea, code: str) -> list[dict]:
    """Return a list of {issue, detail} the critic flags (empty == looks fine)."""
    code = code or ""
    issues: list[dict] = []
    stripped = code.strip()
    if len(stripped) < 20:
        issues.append({"issue": "stub", "detail": "solution is suspiciously short / near-empty"})
        return issues
    if "metric" not in code:
        issues.append({"issue": "no_metric_output",
                       "detail": "code never references 'metric' — it may not emit the required score"})
    if re.search(r'["\']metric["\']\s*:\s*[0-9.+\-eE]+\s*[}\)]', code):
        issues.append({"issue": "hardcoded_metric",
                       "detail": "the metric appears to be a hard-coded constant, not computed"})
    # Requested hyperparameters should appear in the code; none appearing suggests a no-op that
    # ignores the proposal (the idea isn't actually implemented).
    pnames = [str(k) for k in (idea.params or {})]
    if pnames and not any(re.search(rf"\b{re.escape(p)}\b", code) for p in pnames):
        issues.append({"issue": "params_ignored",
                       "detail": f"none of the proposed params {pnames} are referenced in the code"})
    return issues
