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
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_PHASE_PROGRESS, assert_progress_phase

# INVARIANT #1's APPEND-SITE ASSERTION, which every other concurrent-diagnostic writer carries
# (`evaluate.py`, `eval_dispatch.py`, both watchdogs). `_progress` appends `EV_PHASE_PROGRESS`
# directly from the speculative producer worker and from the parallel-build worker threads, and the
# ONLY thing that makes a non-main-task append legal there is the type being fold-ignored — its own
# docstring says so. The claim had no enforcement here, so a later registry edit that folded this
# type would have made every one of those workers an unguarded writer of folded state.
#
# AT MODULE SCOPE, not inside `_emit`, and that placement is the whole point: `_emit`'s body is
# wrapped in a containment `except Exception` (a progress beacon may never fail the work it reports
# on), which would swallow the AssertionError and leave the guard reading as if it held. Import time
# is also where a registry mistake belongs — it is a coding error, exactly like the phase check
# `assert_progress_phase` makes outside that same containment.
assert EV_PHASE_PROGRESS in DIAGNOSTIC_EVENTS, (
    "phase_progress is appended from concurrent workers; invariant #1 permits that only for a "
    "fold-ignored DIAGNOSTIC type")


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


def effective_eval_time_budget(engine) -> Optional[float]:
    """The per-eval WALL-CLOCK ceiling one experiment has to fit, in seconds — or None (docs/29 F1h).

    Its sibling above answers "what did the Researcher ASK for, and may it have it?"; this answers
    "how long does an evaluation of this run actually get?", which is the question a role sizing a
    training schedule needs and which no single field holds. Resolved the SAME way the dispatcher
    resolves it (`engine/eval_dispatch.py::_run_eval`), so the announced number is the one the run
    is planned around rather than one an operator typed into a field that does not reach this task:

    * an ACTIVE eval spec (repo/command tasks) owns the budget through
      `command_eval.eval_spec_time_budget` — `Settings.timeout` is not consulted at all on that
      branch, and a cue that read it printed "~30s (~0.0h)" for a multi-hour repo training.
    * otherwise (script-solution tasks in the sandbox) the run default `Settings.timeout` stands.
      Deliberately the base number and NOT `timeout * sweep_timeout_mult`: the multiplier applies
      only to a node whose Idea already carries a `space`, which does not exist at proposal time, and
      quoting the stretched budget to an experiment that turns out not to be a sweep would overstate
      it by 8x on the shipped default. A researcher-authored `eval_timeout` can raise it later —
      `effective_researcher_eval_timeout` is that rule, governed by `agent_control.timeout` and
      clamped by `max_eval_timeout` — which is why the hint that quotes this number also states the
      headroom rather than pretending the default is a hard wall.

    Read PER PROPOSAL, never cached at construction: `self.timeout` is mutable mid-run by an
    operator's `budget_extend{timeout}` control (`_apply_control_overrides`) and by a granted
    Strategist retune (`engine/strategy.py`), so a number captured in `Engine.__init__` would keep
    quoting a budget nobody is running under — the same reason `_gpu_budget_hint` is stamped per
    proposal (docs/29 F1b).

    ``None`` means "not knowable, say nothing" — a partially built Engine, a non-numeric timeout, or
    a non-finite/zero one. A plausible wrong ceiling is worse than none: the whole point is that the
    role stops guessing.
    """
    from looplab.runtime.command_eval import eval_spec_time_budget

    es = getattr(engine, "_eval_spec", None) or {}
    if es:
        return eval_spec_time_budget(es)
    cand = getattr(engine, "timeout", None)
    if isinstance(cand, bool) or not isinstance(cand, (int, float)):
        return None
    return float(cand) if math.isfinite(cand) and cand > 0 else None


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
        whose `started` could bound it — a build that ends on `implement` would otherwise read as
        still writing forever, which is precisely the "is it hung?" question this exists to answer.

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

        `enabled=False` runs the body and emits NOTHING — a caller that must sometimes stay silent
        writes the bracketed body ONCE rather than duplicating it under an `if`. It has no production
        caller today: the one that had it was the run prologue, and that whole beacon was reverted
        because the log is the wrong channel there (see `events/types.py::PROGRESS_STAGES`). It is
        kept, and driven by a test, because the next caller in a stretch that is only SOMETIMES worth
        narrating will need exactly this and would otherwise reach for the duplicated `if`.
        """
        assert_progress_phase(stage, phase, "started")
        store = getattr(self, "store", None) if enabled else None   # bare Engine.__new__ instances
                                                                    # in tests carry no store either

        def _emit(status: str, extra: Optional[dict] = None):
            # `extra` is a MAPPING and not `**kwargs`, because the caller's `learned` dict flows into
            # it: splatting `**learned` beside fixed keywords made `learned["ok"]` (or `["seconds"]`)
            # a duplicate-keyword TypeError raised AT THE CALL SITE — outside this containment, from
            # inside the `finally` below, where it REPLACES the exception the phase was propagating.
            # A body reporting what it learned would then have destroyed the failure it was
            # reporting. A merge cannot collide; the engine's own keys are merged last so a body
            # cannot overwrite the beacon's authority fields either.
            if store is None:
                return
            try:
                store.append(EV_PHASE_PROGRESS,
                             {**detail, **(extra or {}),
                              "stage": stage, "phase": phase, "status": status})
            except Exception:  # noqa: BLE001 - observability must never take down the work it reports on
                pass

        _emit("started")
        t0 = time.time()
        ok = True
        # Yielded so the BODY can report what it LEARNED to its own `finished` beacon: a count, a
        # size, anything that does not exist until the phase has actually run. Deliberately never
        # merged into `started`, which has already been appended by the time the body sees this.
        learned: dict = {}
        try:
            yield learned
        except BaseException:
            ok = False
            raise
        finally:
            _emit("finished", {**learned, "seconds": round(time.time() - t0, 3), "ok": ok})

    @contextlib.contextmanager
    def _paid_progress(self, stage: str, phase: str, **detail):
        """`_progress` for a phase that SPENDS: the operator's beacon AND a real tracing span.

        WHY THE PAIR IS ONE HELPER. `_progress` appends an event and opens no span, and
        `core/tracing.py::generation` yields a NULL handle whenever `_current_tracer` is unset —
        which `Tracer.span` is the only binder of. So every provider call a `_progress`-only phase
        makes is written to `events.jsonl` with `trace_id=null, span_id=null` and appears in no span
        at all: real money, attributable to nothing, and invisible to `looplab timings`, the trace
        view and every per-phase cost question anyone will ever ask of the run.

        MEASURED, `/var/tmp/looplab-bench/runs-armb` (20 AlgoTune runs, 2026-08-20): 1,579 of the
        campaign's 6,002 paid calls (26 %) carried a null trace, and the novelty gate — a
        `_progress`-only phase running a twelve-turn agentic loop plus, on a rejection, a whole
        second Researcher proposal — was $1.77 of it, 11 % of the $15.73 budget and 6.6 of the 60.8
        run-hours. Nothing in the run said where any of it went. An invisible fifth of a budget is
        its own defect: the next person to measure this is misled exactly as we were.

        The span NESTS (never `new_trace`): this phase's cost belongs to the operation that caused
        it. It is `kind="operation"`, so `_phase_ctx` stamps `phase=<name>` onto every generation
        underneath it and the cost becomes attributable by the SAME key the beacon uses. Degrades to
        the beacon alone when no tracer is wired — tests build `Engine` via `__new__` — exactly as
        `_op_span` does, and for the same reason: observability may never decide whether work runs.

        THE NULL-TRACE SHARE ABOVE IS FROM 2026-08-20 AND IS NO LONGER 26 %. Re-derived on the later
        twenty-run arm B (`/var/tmp/looplab-bench/runs-B`, doc 53 §2b), the remaining spanless money
        was $2.2132 over 905 calls — 11.1 % of $20.0081 — and it was NOT this helper's absence: 817
        of those calls were the CONCURRENT deep-research seams, whose shared step opened no span
        because only its serial caller did (`research_cadence.py::_research_attempt_step`), and 88
        were `concept_cadence.py::_tag_hypothesis_concepts` paying after the span beside it closed.
        Both now open one. So the lesson this docstring teaches generalizes past `_progress`: ANY
        paid seam outside a span is invisible, beacon or no beacon. What keeps it that way is
        `tests/test_paid_calls_are_spanned.py`, a conservation check over the two channels
        (`sum(llm_usage.cost) == sum(generation-span cost)`) rather than a list of known sites.
        """

        tracer = getattr(self, "tracer", None)
        span = (tracer.span(phase, **detail) if tracer is not None
                else contextlib.nullcontext())
        with span, self._progress(stage, phase, **detail) as learned:
            yield learned

    # The shared since-last node-count gate (report/distill/refresh/strategist/coverage cadences).
    # `engine/cadence.py` states why since-last and not `n % every == 0`; the NAME lives here because
    # several mixins call it as `self._cadence_due`.
    _cadence_due = staticmethod(cadence_due)
