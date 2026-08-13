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
import time
from typing import Optional

from looplab.engine.cadence import cadence_due
from looplab.events.types import EV_PHASE_PROGRESS, assert_progress_phase


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

    @contextlib.contextmanager
    def _progress(self, stage: str, phase: str, *, enabled: bool = True, **detail):
        """Bracket one step of a long operation with a `phase_progress` beacon, so the operator's
        screen names the step that is running instead of showing nothing for minutes.

        Emits `started` on entry and `finished` on exit, the latter carrying `seconds` and whether
        the step raised. Both, not just `started`, because the LAST step of a stage has no successor
        whose `started` could bound it — a build that ends on `commit` would otherwise read as still
        committing forever, which is precisely the "is it hung?" question this exists to answer.

        NOT routed through `_append_proposal_event`. That sink buffers a worker's appends until the
        main task publishes them (`novelty.py::_capture_proposal_events`), which is correct for the
        FOLDED audit rows it was built for and exactly wrong here: the Layer-5 speculative producer is
        a background worker doing the very multi-minute work that is invisible, so buffering would
        deliver every beacon at once AFTER the wait it was meant to narrate. A direct `store.append`
        is what invariant #1 permits a concurrent task for a DIAGNOSTIC type.

        Never blocks the work it wraps. A progress beacon that can fail the operation it reports on
        is a downgrade, so the appends are contained — but `assert_progress_phase` runs OUTSIDE that
        containment, because an unregistered phase is a coding error to be fixed, not a runtime
        condition to be survived (see its docstring).

        `enabled=False` runs the body and emits NOTHING. It exists because one caller — the run
        prologue — must not append before it has read the log: its own `started` row would land in
        the `read_all()` it is about to make, which breaks two things that both fail silently. The
        speculation-calibration bootstrap refuses a run whose log is not exactly empty at start, and
        `search/speculation_quality.py::_validate_calibration_setup` pins the log's first FIVE events
        as an exact setup prefix. Phrased as a suppression rather than an `if` around the `with` so
        the bracketed body is written once and cannot drift between the two paths.
        """
        assert_progress_phase(stage, phase, "started")
        store = getattr(self, "store", None) if enabled else None   # bare Engine.__new__ instances
                                                                    # in tests carry no store either

        def _emit(status: str, **extra):
            if store is None:
                return
            try:
                store.append(EV_PHASE_PROGRESS,
                             {"stage": stage, "phase": phase, "status": status, **detail, **extra})
            except Exception:  # noqa: BLE001 - observability must never take down a build or a resume
                pass

        _emit("started")
        t0 = time.time()
        ok = True
        # Yielded so the BODY can report what it learned to its own `finished` beacon — the read_log
        # phase is the reason this exists: how many events it folded, and therefore whether this was
        # a resume at all, are facts that only exist once the phase has run. Never merged into
        # `started`, which has already been appended by then.
        learned: dict = {}
        try:
            yield learned
        except BaseException:
            ok = False
            raise
        finally:
            _emit("finished", seconds=round(time.time() - t0, 3), ok=ok, **learned)

    # The shared since-last node-count gate (report/distill/refresh/strategist/coverage cadences).
    # `engine/cadence.py` states why since-last and not `n % every == 0`; the NAME lives here because
    # several mixins call it as `self._cadence_due`.
    _cadence_due = staticmethod(cadence_due)
