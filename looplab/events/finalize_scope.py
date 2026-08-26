"""The finalize-SCOPE projection: is a staged terminal boundary still open?

Pure functions over a raw event list — no engine state, no I/O, no fold. They live in `events/`
rather than `engine/finalize.py` because a POLICY module needs them: `search/speculation_quality.py`
must know whether quality evidence retains a pending finalization scope, and reaching up into the
engine to ask was the layering violation in doc 25 XP-07. `events` imports only `core`, so every
consumer — engine, serve, search — now imports this DOWNWARD.

`engine/finalize.py` re-exports all five names, so its own callers and the five `serve` importers are
unchanged; it remains where the finalization WRITER lives, and this is only the read side.
"""
from __future__ import annotations

from looplab.events.finalize_protocol import (
    FINALIZE_STEP_ABANDONED,
    FINALIZE_STEP_BEGUN,
    FINALIZE_STEP_COMPLETE,
)
from looplab.events.types import (
    EV_BUDGET,
    EV_CARD_ENRICHED,
    EV_COMMAND_ACK,
    EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FINALIZE_STEP,
    EV_LESSONS_DISTILLED,
    EV_LLM_COST,
    EV_LLM_USAGE,
    EV_READMODEL_SKIPPED,
    EV_REFLECTION_NOTE,
    EV_REPORT_GENERATED,
    EV_RUN_FINISHED,
)


def _adjacent_claim(event) -> bool:
    """Validate an optional physical tail claim carried by a lifecycle event."""
    data = event.data or {}
    if "after_seq" not in data:
        return True
    raw = data.get("after_seq")
    if isinstance(raw, bool):
        return False
    try:
        after_seq = int(raw)
    except (TypeError, ValueError, OverflowError):
        return False
    return event.seq is not None and event.seq == after_seq + 1


def _finalize_begun(events, scope: str):
    return next(
        (
            event
            for event in reversed(events)
            if event.type == EV_FINALIZE_STEP
            and (event.data or {}).get("scope") == scope
            and (event.data or {}).get("step") == FINALIZE_STEP_BEGUN
            and _adjacent_claim(event)
        ),
        None,
    )


def _scope_has_step(events, scope: str, step: str) -> bool:
    return any(
        event.type == EV_FINALIZE_STEP
        and (event.data or {}).get("scope") == scope
        and (event.data or {}).get("step") == step
        for event in events
    )


def finalize_scope_quiescent(events, scope: str) -> bool:
    """Whether a staged finish has seen only its own effects/diagnostics since its CAS claim.

    A control, reset, inject, resume, or unknown domain event invalidates the stale finish decision.
    Cost deltas and command acknowledgements are allowed: they are diagnostics produced while the
    synchronous paid report is completing, not a change to the decision snapshot.
    """
    begun = _finalize_begun(events, scope)
    if begun is None:
        return True  # compatibility: short-lived scoped terminal format without a begun marker
    for event in events:
        if event.seq is None or event.seq <= begun.seq:
            continue
        data = event.data or {}
        if event.type == EV_FINALIZE_STEP and data.get("scope") == scope:
            continue
        if event.type == EV_REPORT_GENERATED and data.get("finalize_scope") == scope:
            continue
        if event.type in {EV_LLM_USAGE, EV_COMMAND_ACK, EV_READMODEL_SKIPPED,
                          EV_REFLECTION_NOTE, EV_LESSONS_DISTILLED, EV_CARD_ENRICHED}:
            # The reflection finalize step emits reflection_note (always) and lessons_distilled
            # (comparative). They are this finalization's OWN effects, so — like llm_usage/command_ack
            # diagnostics — they must not read as a foreign event that abandons scope-based recovery
            # (REPLAY-1): otherwise a crash after reflection_note but before the completion markers
            # leaves the non-modern error-recovery finish permanently unfinished.
            # `card_enriched` is the same case, found later: `finalize_run` appends it itself via
            # `_sync_card_enrichments` between the scope's `begun` claim and `_publish_completion`.
            # Left out, a crash in that window made the finalization's OWN effect read as foreign —
            # `finalize_scope_quiescent` False, `incomplete_finalize_scope` None — and for a scoped
            # finish WITHOUT `finalization_required` (exactly the old scoped-log population this
            # predicate exists for) `should_finalize` never fired again, so the llm_cost roll-up and
            # completion markers stayed missing forever. It is main-task-authored and
            # selection-fenced by replay, so it qualifies on the same terms as the reflection pair.
            continue
        if event.type in {EV_BUDGET, EV_DIVERSITY_ARCHIVE, EV_LLM_COST} and (
            data.get("finalize_scope") == scope
        ):
            continue
        if event.type == EV_FINALIZATION_FINISHED:
            continue
        if event.type == EV_RUN_FINISHED:
            if data.get("finalize_scope") == scope and _adjacent_claim(event):
                continue
            # An outer invocation guard can record the exception raised after ``begun``. It must not
            # steal the original terminal intent; recovery republishes the exact staged payload.
            if is_guarded_abort(data.get("reason")):
                continue
        return False
    return True


# THE GUARDED-ABORT CLASS, and why it is a predicate rather than a literal in six places.
#
# `reason == "error"` never meant "this run crashed". It means "this terminal event was written by
# `cli/run_cmds.py::_run_engine_guarded`'s outer handler rather than by the engine's own clean
# finish", and the finalization protocol keys on that distinction: an outer invocation guard may
# record the exception raised after `begun`, and it must not steal the original terminal intent.
#
# Reaching the operator's spend ceiling travels that same path while being the DESIGNED end of a
# budgeted run, so `run_finished` now names it `budget_exhausted` -- measured on the 2026-08-24
# campaign, all eleven finishes in `runs-B` said `error` and every one was the ceiling, zero
# genuine failures. Introducing that reason WITHOUT this predicate would have flipped all six
# protocol checks at once and made a guarded abort look like a clean engine finish.
GUARDED_ABORT_REASONS = ("error", "budget_exhausted")


def is_guarded_abort(reason) -> bool:
    """True when a `run_finished` reason was written by the guarded-abort path."""
    return str(reason or "").lower() in GUARDED_ABORT_REASONS


def incomplete_finalize_scope(events) -> str | None:
    """Return the latest valid scoped terminal intent until its local checklist is complete.

    Legacy markerless finishes remain complete. A modern CAS claim that lost to a foreign event is
    ignored rather than later swallowing that control. ``finalization_pending()`` independently
    covers upstream ``finish_seq``-only logs.
    """
    candidate: tuple[int, str] | None = None
    for event in events:
        data = event.data or {}
        is_begun = (
            event.type == EV_FINALIZE_STEP
            and data.get("step") == FINALIZE_STEP_BEGUN
            and _adjacent_claim(event)
        )
        is_finished = (
            event.type == EV_RUN_FINISHED
            and not is_guarded_abort(data.get("reason"))
            and _adjacent_claim(event)
        )
        scope = data.get("scope") if is_begun else data.get("finalize_scope")
        if (is_begun or is_finished) and isinstance(scope, str) and scope:
            candidate = (event.seq, scope)
    if candidate is None:
        return None
    _, scope = candidate
    if (_scope_has_step(events, scope, FINALIZE_STEP_COMPLETE)
            or _scope_has_step(events, scope, FINALIZE_STEP_ABANDONED)):
        return None
    return scope if finalize_scope_quiescent(events, scope) else None
