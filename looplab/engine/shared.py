"""The Engine members that belong to EVERY cluster and therefore to none of them (doc 25 ES-14).

The seventeen-file split separates the big concerns well, but it had no home for a helper that
several clusters call. Those defaulted back into `orchestrator.py` — the module the split exists to
shrink — or landed in whichever mixin happened to need them first: the agent-governance gate
`_agent_may` sat in `EvalDispatchMixin` while its own sibling
`effective_researcher_eval_timeout` lived in a separate 33-line module, and `_op_span` stayed in the
god-module under a comment explaining that it is shared. A comment saying "this belongs to no
cluster" is the symptom, not the resolution.

This file is that home. The bar for adding something is narrow and worth stating: it must be called
from more than one cluster AND have no state of its own beyond what the Engine already carries.
Anything with a cluster is still that cluster's, and a helper used once still belongs where it is
used — a shared module that collects single-caller helpers is just a second god-module.
"""
from __future__ import annotations

import contextlib
import math
from typing import Optional

from looplab.engine.cadence import cadence_due


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


class SharedEngineMixin:
    """Cross-cluster members, mixed into `Engine` like every other mixin. In here `self` IS the
    Engine, exactly as in the concern mixins."""

    def _agent_may(self, role: str, setting: str) -> bool:
        """Governance gate (Settings.agent_control): may `role` (strategist|boss|researcher) change
        `setting` at runtime? A setting absent from the map is LOCKED for everyone. Pure + cheap —
        called at each agent seam so the matrix is the single source of truth."""
        from looplab.core.config import parallelism_aliases

        aliases = parallelism_aliases(setting)
        if len(aliases) == 1:
            return role in (self._agent_control.get(setting) or ())
        canonical, legacy = aliases
        # a canonical entry is the migrated authority record even when its allow-list is
        # empty (an explicit revocation). Fall back to a legacy snapshot grant only when the canonical
        # key is absent; unioning both would let a stale alias silently bypass a new canonical lock.
        authority_key = canonical if canonical in self._agent_control else legacy
        return role in (self._agent_control.get(authority_key) or ())

    def _op_span(self, name: str, **attrs):
        """A named NEW-trace span for a sub-operation (strategist consult, hypothesis merge …) so the
        event appended inside it is auto-stamped with THIS op's trace_id (eventstore reads current_ids),
        letting the UI scope the event's trace to just that operation. Null-context when no tracer is
        wired (tests build Engine via __new__ and skip __init__) — the op still runs, just untraced."""
        tr = getattr(self, "tracer", None)
        return tr.span(name, new_trace=True, **attrs) if tr is not None else contextlib.nullcontext()

    # The shared since-last node-count gate (report/distill/refresh/strategist/coverage cadences).
    # `engine/cadence.py` states why since-last and not `n % every == 0`; the NAME lives here because
    # several mixins call it as `self._cadence_due`.
    _cadence_due = staticmethod(cadence_due)
