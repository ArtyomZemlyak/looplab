"""One sentence about money, in one place.

MEASURED 2026-09-06 over eight capped probes, by phase, counting `generation` spans whose prompt
carries the `BUDGET:` line:

    deep_research   395/475  83 %      propose            0/538   0 %
    plan_step       279/897  31 %      repropose          0/158   0 %
    plan             44/216  20 %      foresight_rank      0/64   0 %
                                       hyp_prioritize      0/60   0 %

The two phases the standing sweep names -- `foresight_rank` and `hyp_prioritize` -- get nothing, and
so do `propose` and `repropose`, which §245-§247 measured as holding a quarter of a run's spend in
11 % of its calls. The line existed twice already, copy-pasted into `agents/deep_research.py` and
`adapters/repo_developer.py` with different second sentences; a third copy for the proposal phases
would have been the moment the wordings started drifting apart for good.
"""
from __future__ import annotations


def budget_line(spent: float, limit: float | None, tail: str = "") -> str:
    """`BUDGET: $x of $y spent, $z left (n % gone).` plus a caller-chosen sentence.

    Returns "" when there is no limit: a run with no ceiling has no share to report, and inventing
    one ("0 % gone") would be a number the run cannot act on.
    """
    try:
        cap = float(limit) if limit is not None else 0.0
        used = max(0.0, float(spent))
    except (TypeError, ValueError):
        return ""
    if cap <= 0:
        return ""
    left = max(0.0, cap - used)
    pct = 100.0 * used / cap
    head = (f"BUDGET: ${used:.4f} of ${cap:.4f} spent, ${left:.4f} left ({pct:.0f} % gone).")
    return f"{head} {tail}".rstrip() + "\n\n" if tail else head + "\n\n"
