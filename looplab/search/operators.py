"""Operators (I7/I11, ADR-6). The result-moving levers beyond draft/improve:

- debug  : a depth-bounded fix attempt on a *failed* node (re-propose from it).
- merge  : an ensemble/merge node combining ≥2 parents (mean of numeric params) —
           the multi-parent DAG step (parent_ids has length ≥2).

draft/improve live in the orchestrator's role calls; merge is purely mechanical
(no model needed) so it lives here as a function. The policy decides *when* each
operator fires; these functions decide *what* the resulting Idea is.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node


def merge_idea(parents: list[Node]) -> Idea:
    """Mean-merge the numeric params of the parents into one new Idea."""
    keys: set[str] = set()
    for p in parents:
        keys |= set(p.idea.params)
    params: dict[str, float] = {}
    for k in sorted(keys):
        # Only mean-merge numerically-coercible values. A non-numeric param (free-form repo task)
        # would otherwise raise inside sum() and, because no node_created event is written, the
        # policy would re-issue the SAME merge every iteration — an infinite loop on resume.
        vals: list[float] = []
        for p in parents:
            if k in p.idea.params:
                try:
                    vals.append(float(p.idea.params[k]))
                except (TypeError, ValueError):
                    continue
        if vals:
            params[k] = round(sum(vals) / len(vals), 4)
    pids = ",".join(str(p.id) for p in parents)
    # CODEX AGENT: a merge inherits the UNION of every parent. A bare durable Idea has unknown/absent
    # membership; an explicit zero delta is the only unambiguous way to preserve that union unchanged.
    return Idea(operator="merge", params=params, rationale=f"mean-merge of nodes {pids}",
                concept_mode="delta", concepts_added=[], concepts_removed=[])
