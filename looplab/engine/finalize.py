"""Durable run finalization and rebuildable UI projections.

Two identities deliberately coexist:

* ``finalize_scope`` names one logical wrap-up and gates paid/external effects with
  ``finalize_step`` markers;
* ``finish_seq`` names the accepted ``run_finished`` event and is acknowledged by
  ``finalization_finished`` for crash recovery.

Keeping both lets old scoped logs and the newer exact-finish handshake converge without replaying a
paid report, reflection, or cost delta after an ambiguous crash.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING

import orjson

from looplab.core.atomicio import atomic_write_bytes, atomic_write_text
from looplab.core.models import RunState
from looplab.engine.costs import in_memory_cost_total, reconcile_cost_accountants
from looplab.events.eventstore import EventStoreConcurrencyError
from looplab.events.htmlview import render_html
from looplab.events.readmodel import build_readmodel
from looplab.events.replay import fold
from looplab.events.traceview import build_trace_view, hydrate_inputs, load_spans
from looplab.events.types import (
    EV_BUDGET,
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
    EV_RUN_ABORT,
    EV_RUN_FINISHED,
)
from looplab.search.archive import DiversityArchive

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


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
            and (event.data or {}).get("step") == "begun"
            and _adjacent_claim(event)
        ),
        None,
    )


def _scope_terminal(events, scope: str):
    return next(
        (
            event
            for event in reversed(events)
            if event.type == EV_RUN_FINISHED
            and (event.data or {}).get("finalize_scope") == scope
            and str((event.data or {}).get("reason") or "").lower() != "error"
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
                          EV_REFLECTION_NOTE, EV_LESSONS_DISTILLED}:
            # The reflection finalize step emits reflection_note (always) and lessons_distilled
            # (comparative). They are this finalization's OWN effects, so — like llm_usage/command_ack
            # diagnostics — they must not read as a foreign event that abandons scope-based recovery
            # (REPLAY-1): otherwise a crash after reflection_note but before the completion markers
            # leaves the non-modern error-recovery finish permanently unfinished.
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
            if str(data.get("reason") or "").lower() == "error":
                continue
        return False
    return True


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
            and data.get("step") == "begun"
            and _adjacent_claim(event)
        )
        is_finished = (
            event.type == EV_RUN_FINISHED
            and str(data.get("reason") or "").lower() != "error"
            and _adjacent_claim(event)
        )
        scope = data.get("scope") if is_begun else data.get("finalize_scope")
        if (is_begun or is_finished) and isinstance(scope, str) and scope:
            candidate = (event.seq, scope)
    if candidate is None:
        return None
    _, scope = candidate
    if _scope_has_step(events, scope, "complete") or _scope_has_step(events, scope, "abandoned"):
        return None
    return scope if finalize_scope_quiescent(events, scope) else None


def _terminal_data_for_scope(events, scope: str) -> tuple[dict, str]:
    """Recover the exact terminal payload for an incomplete finalization boundary."""
    begun = _finalize_begun(events, scope)
    finish_data = (begun.data or {}).get("finish_data") if begun is not None else None
    if isinstance(finish_data, dict):
        return dict(finish_data), "finalize_begun"

    prior_finish = _scope_terminal(events, scope)
    if prior_finish is not None:
        data = dict(prior_finish.data or {})
        for key in ("after_seq", "finalization_required", "finalize_scope"):
            data.pop(key, None)
        return data, "superseding_control"

    reason = "aborted" if scope.startswith("abort:") else "recovered_terminal"
    return {"reason": reason}, "legacy_finalize_begun"


def _scope_is_effective_terminal(events, state: RunState, scope: str) -> bool:
    if not state.finished or state.last_finish_seq < 0:
        return False
    event = next((item for item in events if item.seq == state.last_finish_seq), None)
    if event is None or event.type != EV_RUN_FINISHED:
        return False
    data = event.data or {}
    return (
        data.get("finalize_scope") == scope
        and str(data.get("reason") or "").lower() != "error"
        and _adjacent_claim(event)
    )


def _finish_report_planned(events, scope: str) -> bool:
    begun = _finalize_begun(events, scope)
    return bool(begun is not None and (begun.data or {}).get("finish_report_planned") is True)


def scoped_finish_report(events, scope: str):
    return next(
        (
            event
            for event in reversed(events)
            if event.type == EV_REPORT_GENERATED
            and (event.data or {}).get("finalize_scope") == scope
            and (event.data or {}).get("trigger") == "finish"
        ),
        None,
    )


def _finalize_scope(events, state: RunState) -> tuple[str, int]:
    """Stable scope plus the accepted physical finish sequence."""
    finished = next((event for event in events if event.seq == state.last_finish_seq), None)
    if finished is not None:
        explicit = (finished.data or {}).get("finalize_scope")
        if isinstance(explicit, str) and explicit:
            return explicit, finished.seq
    if state.stop_requested:
        abort = next((event for event in reversed(events) if event.type == EV_RUN_ABORT), None)
        if abort is not None:
            return f"abort:{abort.seq}", state.last_finish_seq
    return f"finish:{state.last_finish_seq}", state.last_finish_seq


def _has_finish_step(events, event_type: str, finish_seq: int) -> bool:
    return any(
        event.type == event_type and (event.data or {}).get("finish_seq") == finish_seq
        for event in events
    )


def _finalize_step_done(
    events,
    scope: str,
    finish_seq: int,
    step: str,
    effect_type: str | None = None,
) -> bool:
    if _scope_has_step(events, scope, step):
        return True
    if effect_type is None:
        return False
    for event in events:
        if event.type != effect_type:
            continue
        data = event.data or {}
        if data.get("finalize_scope") == scope or data.get("finish_seq") == finish_seq:
            return True
        # Compatibility with the pre-marker build: an unscoped effect after this exact finish is
        # evidence that the step committed, so recovery must not duplicate it.
        if (
            event.seq is not None
            and event.seq > finish_seq
            and data.get("finalize_scope") is None
            and data.get("finish_seq") is None
        ):
            return True
    return False


def _llm_cost_rollup_stale(events, scope: str, finish_seq: int) -> bool:
    """Whether durable usage landed after this finish's presentation roll-up boundary.

    Older builds could write ``llm_cost`` before the Part IV/V stewards existed.  If such a run is
    resumed with an otherwise-incomplete finalization, the upgraded stewards may append exact
    ``llm_usage`` deltas after that old marker.  The folded total remains correct, but consumers of
    the terminal roll-up event would otherwise keep seeing the pre-upgrade total forever.
    """
    boundary = -1
    latest_usage = -1
    for event in events:
        seq = event.seq if isinstance(event.seq, int) else -1
        data = event.data or {}
        if event.type == EV_LLM_USAGE:
            latest_usage = max(latest_usage, seq)
            continue
        if (
            event.type == EV_FINALIZE_STEP
            and data.get("scope") == scope
            and data.get("step") == "llm_cost"
        ):
            boundary = max(boundary, seq)
            continue
        if event.type != EV_LLM_COST:
            continue
        if (
            data.get("finalize_scope") == scope
            or data.get("finish_seq") == finish_seq
            or (
                seq > finish_seq
                and data.get("finalize_scope") is None
                and data.get("finish_seq") is None
            )
        ):
            boundary = max(boundary, seq)
    return boundary >= 0 and latest_usage > boundary


def _mark_finalize_step(engine: "Engine", scope: str, step: str, **data) -> None:
    engine.store.append(EV_FINALIZE_STEP, {"scope": scope, "step": step, **data})


def _claim_paid_finalize_step(engine: "Engine", scope: str, step: str) -> None:
    """Persist the at-most-once boundary before dispatching a paid/external effect."""
    # both guarantees are mandatory here.  The surrounding paid_effect_guard closes
    # the live/live race, while strict fsync closes the crash window.  A sync failure may leave a
    # visible ambiguous marker, but it must propagate before the provider is called.
    engine.store.append(
        EV_FINALIZE_STEP,
        {"scope": scope, "step": step},
        require_lock=True,
        require_durable=True,
    )


def _resolve_existing_finish_report_attempt(
    engine: "Engine",
    events,
    scope: str,
    *,
    close_ambiguous: bool,
) -> bool | None:
    """Return a terminal answer for an existing plan/attempt, or None when dispatch is still needed."""
    if not _finish_report_planned(events, scope):
        return True
    if _scope_has_step(events, scope, "report"):
        return True
    if scoped_finish_report(events, scope) is not None:
        return True
    if _scope_has_step(events, scope, "report_begun"):
        if not close_ambiguous:
            return None  # it may belong to the live process currently holding paid_effect_guard
        _mark_finalize_step(
            engine,
            scope,
            "report",
            outcome="prior_attempt_incomplete_not_replayed",
        )
        return True
    return None


def ensure_finish_report(engine: "Engine", events, scope: str, *, state=None) -> bool:
    """Resolve one planned paid finish report without an ambiguous provider retry.

    The successful ``report`` marker is deliberately deferred until ``run_finished`` is durable so
    the upstream replay contract can keep ``report_generated`` immediately adjacent to its finish.
    """
    # ``events`` remains part of the compatibility signature, but every decision uses a fresh read.
    # a missing writer is a free/no-provider recovery path.  It must observe an existing
    # terminal or ambiguous attempt without requiring paid-work locking/fsync on lock-less filesystems.
    del events
    current = engine.store.read_all()
    writer_available = getattr(engine, "report_writer", None) is not None
    resolved = _resolve_existing_finish_report_attempt(
        engine,
        current,
        scope,
        close_ambiguous=False,
    )
    if resolved is not None:
        return resolved
    if not writer_available:
        if not _scope_has_step(current, scope, "report_begun"):
            return False
        # A differently configured process may currently own this attempt.  An optional guard waits
        # for it on a normal filesystem; where locks are unsupported, a required paid dispatcher
        # could not have entered, so closing the durable ambiguity remains safe and available.
        with engine.store.paid_effect_guard(required=False):
            current = engine.store.read_all()
            resolved = _resolve_existing_finish_report_attempt(
                engine,
                current,
                scope,
                close_ambiguous=True,
            )
            return resolved if resolved is not None else False

    # Repeat the decision while holding the paid-effect guard.  Otherwise a second
    # EventStore/process can pass the preflight using a stale absence observation.
    with engine.store.paid_effect_guard():
        current = engine.store.read_all()
        resolved = _resolve_existing_finish_report_attempt(
            engine,
            current,
            scope,
            close_ambiguous=True,
        )
        if resolved is not None:
            return resolved

        begun = _finalize_begun(current, scope)
        if state is None:
            anchor = begun.seq if begun is not None else -1
            state = fold([event for event in current if event.seq is None or event.seq < anchor])
        _claim_paid_finalize_step(engine, scope, "report_begun")
        engine._write_report(state, trigger="finish", finalize_scope=scope)
        if scoped_finish_report(engine.store.read_all(), scope) is None:
            _mark_finalize_step(
                engine,
                scope,
                "report",
                outcome="attempt_returned_without_durable_report",
            )
        return True


def _reflection_can_write(engine: "Engine") -> bool:
    """Whether Engine configuration can make run-end reflection perform external/shared work.

    Minimal test/compatibility engines historically expose neither flag, so absence remains enabled;
    an actual Engine exposes both and ``write_reflection_note`` uses this same truthiness gate.
    """
    # An instance-level writer is an explicit integration/test seam and may perform work regardless
    # of the stock LessonMemory flags.  Treat it as external rather than silently skipping it.
    if callable(getattr(engine, "__dict__", {}).get("_write_reflection_note")):
        return True
    if hasattr(engine, "_reflection_priors") and not bool(engine._reflection_priors):
        return False
    if hasattr(engine, "memory_dir") and not bool(engine.memory_dir):
        return False
    return True


def ensure_finalize_reflection(engine: "Engine", scope: str, finish_seq: int) -> None:
    """Run one reflection attempt, or close an already-ambiguous attempt without replay."""
    del finish_seq  # scope is the legacy-compatible durable identity for reflection markers
    events = engine.store.read_all()
    if _scope_has_step(events, scope, "reflection"):
        return
    if not _reflection_can_write(engine):
        with engine.store.paid_effect_guard(required=False):
            events = engine.store.read_all()
            if _scope_has_step(events, scope, "reflection"):
                return
            if _scope_has_step(events, scope, "reflection_begun"):
                _mark_finalize_step(
                    engine,
                    scope,
                    "reflection",
                    outcome="prior_attempt_incomplete_not_replayed",
                )
                return
            # the real reflection implementation immediately returns for these configs.
            # Keep free finalization compatible with filesystems that cannot provide strict locking;
            # ordinary legacy-shaped markers are sufficient because no paid/shared write can happen.
            _mark_finalize_step(engine, scope, "reflection_begun", outcome="disabled")
            _mark_finalize_step(engine, scope, "reflection", outcome="disabled")
            return

    with engine.store.paid_effect_guard():
        events = engine.store.read_all()
        if _scope_has_step(events, scope, "reflection"):
            return
        if _scope_has_step(events, scope, "reflection_begun"):
            _mark_finalize_step(
                engine,
                scope,
                "reflection",
                outcome="prior_attempt_incomplete_not_replayed",
            )
            return

        # Freeze the exact pre-claim state.  Diagnostics appended while the provider is running must
        # not silently change the reflection input selected by this finalization boundary.
        final = fold(events)
        _claim_paid_finalize_step(engine, scope, "reflection_begun")
        engine._write_reflection_note(final)
        _mark_finalize_step(engine, scope, "reflection")


def mark_finish_report_complete(engine: "Engine", scope: str) -> None:
    events = engine.store.read_all()
    if not _finish_report_planned(events, scope) or _scope_has_step(events, scope, "report"):
        return
    if scoped_finish_report(events, scope) is not None:
        _mark_finalize_step(engine, scope, "report", outcome="completed")


def _build_readmodel_atomic(events, path: Path) -> RunState:
    """Build the rebuildable SQLite projection off to the side, then atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        state = build_readmodel(events, tmp)
        os.replace(tmp, path)
        return state
    finally:
        for candidate in (tmp, Path(f"{tmp}-journal"), Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
            try:
                candidate.unlink()
            except OSError:
                pass


def _recover_scoped_terminal(engine: "Engine", events, state: RunState, scope: str) -> tuple[list, RunState]:
    if _scope_is_effective_terminal(events, state, scope):
        mark_finish_report_complete(engine, scope)
        refreshed = engine.store.read_all()
        return refreshed, fold(refreshed)
    if not finalize_scope_quiescent(events, scope):
        return events, state

    finish_data, recovery_kind = _terminal_data_for_scope(events, scope)
    # If the RECOVERY TARGET is itself an error terminal — the begun marker captured an error finish,
    # i.e. the guarded-abort path staged ``{"reason": "error"}`` — re-materializing it loops forever:
    # an error finish is never an "effective terminal" (that predicate excludes error), so the scope
    # never gains a complete/abandoned marker and a DUPLICATE error run_finished (+ report clone) is
    # appended on every resume (violating invariants #2 one-terminal and #3 gated-side-effects). The
    # run errored out — that IS its terminal state — so close the scope idempotently instead. (A
    # NON-error target, e.g. a transient error finish over a durable ``time_budget`` intent, still
    # re-materializes the original terminal below and converges normally.)
    if str(finish_data.get("reason") or "").lower() == "error":
        if (state.finished and state.last_finish_seq >= 0
                and not _has_finish_step(events, EV_FINALIZATION_FINISHED, state.last_finish_seq)):
            try:
                engine.store.append(
                    EV_FINALIZATION_FINISHED, {"finish_seq": state.last_finish_seq})
            except Exception:  # noqa: BLE001 - the open scope retries the same close on re-entry
                pass
        latest = engine.store.read_all()
        if not (_scope_has_step(latest, scope, "complete")
                or _scope_has_step(latest, scope, "abandoned")):
            try:
                _mark_finalize_step(engine, scope, "abandoned", outcome="error_terminal")
            except Exception:  # noqa: BLE001 - retried on re-entry while the scope stays open
                pass
        refreshed = engine.store.read_all()
        return refreshed, fold(refreshed)
    latest = engine.store.read_all()
    # Re-check quiescence against the SAME snapshot the finish CAS tail is derived from (not the caller's
    # staler `events`). A concurrent control — reopen/reset/inject from the UI/CLI writer — can land in the
    # window between `events` and this read; it is invisible to the quiescence gate at the top yet becomes
    # the tail here, so the run_finished CAS below (`expected_last_seq=tail_seq`) would SUCCEED on mere
    # adjacency and silently bury it (the fold clears `finished` on reopen, then this re-materialized
    # run_finished sets it back). Bail so the reopened/reset scope is handled fresh on re-entry — matching
    # the orchestrator's own finish path, which re-checks quiescence and derives the tail from one snapshot.
    if not finalize_scope_quiescent(latest, scope):
        return latest, fold(latest)
    report = scoped_finish_report(latest, scope)
    tail_seq = latest[-1].seq if latest else -1
    if report is not None and report.seq != tail_seq:
        # No provider call: republish the already-durable content only when diagnostics followed it,
        # restoring the report->finish adjacency required by replay. A background-appendable event can
        # splice in between this tail read and the CAS just like the finish CAS below; on a lost race
        # bail to a fresh read so scope recovery retries instead of raising out of finalization.
        try:
            cloned = engine.store.append(
                EV_REPORT_GENERATED,
                dict(report.data or {}),
                expected_last_seq=tail_seq,
            )
        except EventStoreConcurrencyError:
            return engine.store.read_all(), fold(engine.store.read_all())
        tail_seq = cloned.seq
    payload = {
        **finish_data,
        "finalize_scope": scope,
        f"recovered_from_{recovery_kind}": True,
    }
    try:
        engine.store.append(EV_RUN_FINISHED, payload, expected_last_seq=tail_seq)
    except EventStoreConcurrencyError:
        return engine.store.read_all(), fold(engine.store.read_all())
    mark_finish_report_complete(engine, scope)
    refreshed = engine.store.read_all()
    return refreshed, fold(refreshed)


def _publish_completion(engine: "Engine", scope: str, finish_seq: int) -> bool:
    """Close both recovery protocols without an unrepairable marker gap.

    The exact-finish acknowledgement is written first, after every required effect has committed.
    If the process dies before the scoped ``complete`` marker, the still-open scope drives one more
    idempotent pass. In the opposite order a legacy/scoped finish could die after ``complete`` but
    before ``finalization_finished`` and neither protocol would consider it pending.
    """
    events = engine.store.read_all()
    latest = fold(events)
    if not latest.finished or latest.last_finish_seq != finish_seq:
        return False
    if not _has_finish_step(events, EV_FINALIZATION_FINISHED, finish_seq):
        try:
            engine.store.append(EV_FINALIZATION_FINISHED, {"finish_seq": finish_seq})
        except Exception:  # noqa: BLE001 - exact-finish marker remains retryable
            return False
    events = engine.store.read_all()
    if not _scope_has_step(events, scope, "complete"):
        try:
            _mark_finalize_step(engine, scope, "complete")
        except Exception:  # noqa: BLE001 - the open scope retries the same checklist on re-entry
            return False
    return True


def finalize_run(engine: "Engine", *, entry_finished: bool, start_time: float) -> RunState:
    """Finish one accepted terminal boundary and rebuild its derived projections."""
    events = engine.store.read_all()
    completed = fold(events)
    pending_scope = incomplete_finalize_scope(events)
    if pending_scope is not None:
        if ensure_finish_report(engine, events, pending_scope):
            events = engine.store.read_all()
            completed = fold(events)
            events, completed = _recover_scoped_terminal(
                engine, events, completed, pending_scope)

    finish_event = next(
        (event for event in events if event.seq == completed.last_finish_seq),
        None,
    )
    finish_data = (finish_event.data or {}) if finish_event is not None else {}
    modern_protocol = bool(finish_data.get("finalization_required", False))
    scoped_terminal = bool(
        pending_scope is not None
        and _scope_is_effective_terminal(events, completed, pending_scope)
    )
    legacy_initial = bool(
        not entry_finished
        and completed.finished
        and str(finish_data.get("reason") or "").lower() != "error"
    )
    should_finalize = bool(completed.finalization_pending() or scoped_terminal or legacy_initial)

    requirements_complete = False
    scope = ""
    finish_seq = completed.last_finish_seq
    if should_finalize and completed.finished and finish_seq >= 0:
        scope, finish_seq = _finalize_scope(events, completed)

        events = engine.store.read_all()
        if not _finalize_step_done(events, scope, finish_seq, "budget", EV_BUDGET):
            try:
                engine.store.append(EV_BUDGET, {
                    "elapsed_s": round(time.time() - start_time, 3),
                    "eval_s": round(completed.total_eval_seconds, 3),
                    "nodes": len(completed.nodes),
                    "finalize_scope": scope,
                    "finish_seq": finish_seq,
                })
                _mark_finalize_step(engine, scope, "budget")
            except Exception:  # noqa: BLE001 - exact effect/marker detection makes retry safe
                pass

        events = engine.store.read_all()
        if not _finalize_step_done(events, scope, finish_seq, "diversity", EV_DIVERSITY_ARCHIVE):
            try:
                archive = dict(DiversityArchive(engine.archive_resolution).summary(completed))
                engine.store.append(EV_DIVERSITY_ARCHIVE, {
                    **archive,
                    "finalize_scope": scope,
                    "finish_seq": finish_seq,
                })
                _mark_finalize_step(engine, scope, "diversity")
            except Exception:  # noqa: BLE001 - retry missing deterministic step later
                pass

        events = engine.store.read_all()
        if not _finalize_step_done(events, scope, finish_seq, "case"):
            try:
                final = fold(events)
                engine._store_case(final)
                # D8 research memory is a first-class source, independent of concept surfacing.
                # Compatibility engines may omit the hook, so keep finalization best-effort across versions.
                store_research = getattr(engine, "_store_research_claims", None)
                if callable(store_research):
                    store_research(final)
                if getattr(engine, "_cross_run_concepts", False):    # §21.20 Step 2: cross-run concept capsule
                    engine._store_concept_capsule(final)             # idempotent upsert, sibling of the case
                _mark_finalize_step(engine, scope, "case")
            except Exception:  # noqa: BLE001 - case store is an idempotent upsert
                pass

        # §22.4 AGENTIC taxonomy steward — portfolio-scoped and not a terminal requirement. A caught
        # curation failure does not prevent terminal completion, but these calls run synchronously and can
        # delay finalize / add paid inference before the cost roll-up. Idempotency lives in the stores
        # (append-only aliases, per-task facets), so this step carries no scoped finalize marker.
        # Gated on `cross_run_curation` + an available LLM client; off => byte-identical finalize AND no extra
        # fold (the flag-check skips the whole block so the default path does zero steward work). The stewards
        # re-check the same gate internally (defense in depth). NOTE: the reviewed graph includes THIS run only
        # when `cross_run_concepts` is also on (that flag gates the capsule write in the case step above).
        ensure_finalize_reflection(engine, scope, finish_seq)

        # Layer 1b ref-only Card enrichment runs once more after run-end reflection.  This captures
        # comparative lessons produced during finalize (and also repairs a crash gap on retry) before
        # the terminal cost/publication steps, while Card events remain authored by the main task.
        try:
            engine._sync_card_enrichments(fold(engine.store.read_all()))
        except Exception:  # noqa: BLE001 - advisory Card links must never block terminal completion
            pass

        # reflection must precede claim curation so this run's durable lessons are visible;
        # every steward still precedes llm_cost so its provider usage enters the terminal roll-up.
        if getattr(engine, "_cross_run_curation", False):
            try:
                final = fold(engine.store.read_all())
                engine._store_concept_curation(final)
                engine._store_claim_curation(final)   # agentic claim ratify/reject/pin
                engine._store_task_facets(final)       # agentic task faceting (once/task)
            except Exception:  # noqa: BLE001 — steward failure must not prevent terminal completion
                pass

        events = engine.store.read_all()
        cost_step_done = _finalize_step_done(
            events, scope, finish_seq, "llm_cost", EV_LLM_COST)
        # a legacy roll-up marker is not proof that newly-added steward usage was
        # presented.  Refresh only when a later exact usage delta exists; the new roll-up then becomes
        # the boundary, so repeated recovery remains idempotent.
        cost_refresh_needed = not cost_step_done or _llm_cost_rollup_stale(
            events, scope, finish_seq)
        cost_refresh_ok = True
        if cost_refresh_needed:
            cost_refresh_ok = emit_llm_cost(
                engine,
                finalize_scope=scope,
                finish_seq=finish_seq,
            )
            if cost_refresh_ok and not cost_step_done:
                _mark_finalize_step(engine, scope, "llm_cost")

        latest_events = engine.store.read_all()
        requirements = (
            (
                not _finish_report_planned(latest_events, scope)
                or _finalize_step_done(latest_events, scope, finish_seq, "report")
            ),
            _finalize_step_done(latest_events, scope, finish_seq, "budget", EV_BUDGET),
            _finalize_step_done(
                latest_events, scope, finish_seq, "diversity", EV_DIVERSITY_ARCHIVE),
            _finalize_step_done(latest_events, scope, finish_seq, "case"),
            _finalize_step_done(latest_events, scope, finish_seq, "reflection"),
            # A NEEDED cost refresh that failed to reconcile must block completion, preserving the initial
            # cost step's "block until durable" guarantee. The step marker persists from a prior pass
            # (cost_step_done), so without this a stale-refresh whose emit_llm_cost returns False would
            # still read "done" and the run would publish completion with an un-folded usage delta stranded
            # in the outbox — a silent cost under-count on a run that reports itself finalized. The next
            # finalize pass re-emits; a transient outbox/append conflict clears without a wedge.
            _finalize_step_done(latest_events, scope, finish_seq, "llm_cost", EV_LLM_COST)
            and (not cost_refresh_needed or cost_refresh_ok),
        )
        requirements_complete = all(requirements)
        if modern_protocol and requirements_complete:
            _publish_completion(engine, scope, finish_seq)

    try:
        final = _build_readmodel_atomic(
            engine.store.read_all(), engine.run_dir / "readmodel.sqlite")
    except Exception as exc:  # derived cache must never undo a domain terminal
        final = fold(engine.store.read_all())
        try:
            engine.store.append(EV_READMODEL_SKIPPED, {"error": str(exc)[:300]})
        except Exception:  # noqa: BLE001 - even the diagnostic is best-effort
            pass
    # UI projection (ADR-17): join the research tree (events) to its execution detail
    # (spans) -> trace.json for the React UI + an inline span tree in the static HTML.
    # Hydrate delta-encoded generation inputs before projection so archived and live readers derive the
    # same retained diagnostic conversation. `build_trace_view` then redacts/caps that material and marks
    # omissions explicitly: trace.json is a bounded, potentially partial projection, never a raw transcript.
    try:
        trace_view = build_trace_view(
            final,
            hydrate_inputs(load_spans(engine.run_dir / "spans.jsonl")),
        )
    except Exception:
        if modern_protocol:
            return final
        raise  # compatibility for an in-flight legacy scope; its complete marker stays absent
    try:
        atomic_write_bytes(engine.run_dir / "trace.json", orjson.dumps(trace_view))
    except Exception:  # noqa: BLE001 - independent rebuildable projection
        pass
    try:
        atomic_write_text(engine.run_dir / "tree.html", render_html(final, trace_view))
    except Exception:  # noqa: BLE001 - independent rebuildable projection
        pass

    if not modern_protocol and requirements_complete and finish_seq >= 0:
        _publish_completion(engine, scope, finish_seq)
        final = fold(engine.store.read_all())
    return final


def emit_llm_cost(
    engine: "Engine",
    *,
    finalize_scope: str | None = None,
    finish_seq: int | None = None,
) -> bool:
    """Reconcile exact usage deltas, then emit one presentation roll-up for this finish."""
    try:
        tracked = isinstance(getattr(engine, "_llm_cost_bindings", None), dict)
        if tracked and not reconcile_cost_accountants(engine):
            return False
        durable = fold(engine.store.read_all()).llm_cost
        total = durable or in_memory_cost_total(engine)
        if not total:
            return True
        payload = {
            "cost": round(float(total.get("cost", 0.0)), 6),
            "calls": int(total.get("calls", 0)),
            "prompt_tokens": int(total.get("prompt_tokens", 0)),
            "completion_tokens": int(total.get("completion_tokens", 0)),
            "total_tokens": int(total.get("total_tokens", 0)),
        }
        if finalize_scope is not None:
            payload["finalize_scope"] = finalize_scope
        if finish_seq is not None:
            payload["finish_seq"] = finish_seq
        if any(payload[key] for key in (
            "cost", "calls", "prompt_tokens", "completion_tokens", "total_tokens"
        )):
            engine.store.append(EV_LLM_COST, payload)
        return True
    except Exception:  # noqa: BLE001 - telemetry never aborts domain finalization
        return False
