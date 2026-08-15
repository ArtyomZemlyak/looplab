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

from pathlib import Path
import time
from typing import TYPE_CHECKING

from looplab.core.atomicio import atomic_write_bytes, atomic_write_text
from looplab.core.models import RunState
from looplab.core.tracing import TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS
from looplab.engine.costs import in_memory_cost_total, reconcile_cost_accountants
from looplab.events.eventstore import EventStoreConcurrencyError
# The finalize-SCOPE read side moved to `events/` so a policy module can import it downward
# (doc 25 XP-07). Re-exported under the original names: this module is still where the
# finalization WRITER lives, and every existing caller/patch path keeps resolving here.
from looplab.events.finalize_scope import (  # noqa: F401
    _adjacent_claim, _finalize_begun, _scope_has_step, finalize_scope_quiescent,
    incomplete_finalize_scope)
# The step vocabulary and the `budget` receipt shape are the PROTOCOL, shared with every reader —
# `search/speculation_quality.py` refuses calibration evidence whose finalization does not match it
# exactly, and `search` may not import the engine (doc 25 SE-01).
from looplab.events.finalize_protocol import (
    FINALIZE_STEP_ABANDONED,
    FINALIZE_STEP_BUDGET,
    FINALIZE_STEP_CASE,
    FINALIZE_STEP_COMPLETE,
    FINALIZE_STEP_DIVERSITY,
    FINALIZE_STEP_LLM_COST,
    FINALIZE_STEP_REFLECTION,
    FINALIZE_STEP_REFLECTION_BEGUN,
    FINALIZE_STEP_REPORT,
    FINALIZE_STEP_REPORT_BEGUN,
    budget_receipt,
)
from looplab.events.htmlview import render_html
# NOT `build_readmodel`: the atomic publish moved DOWN to `events/readmodel.py` so the on-demand
# `looplab readmodel` rebuild shares it. Importing the inner builder here too would leave a decoy
# patch seam — `monkeypatch.setattr(finalize, "build_readmodel", …)` would resolve and reach
# nothing, because `publish_readmodel` calls its own module global.
from looplab.events.readmodel import publish_readmodel
from looplab.events.replay import fold, run_wall_clock_seconds
from looplab.events.traceview import (
    TRACE_VIEW_SPAN_CAP, build_trace_view, hydrate_inputs, load_span_tail,
    trace_projection_json_bytes)
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
    EV_RUN_ABORT,
    EV_RUN_FINISHED,
)
from looplab.search.archive import DiversityArchive

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


_STEWARD_RECEIPT_OUTCOMES = frozenset({
    "disabled",
    "already-resolved",
    "already-governed",
    "empty",
    "unavailable",
    "proposed",
    "error",
})






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


def _scope_finish_event(events, scope: str):
    """This scope's OWN `run_finished`, whatever its reason.

    `_scope_terminal` above deliberately skips error finishes — it hunts a re-materializable
    terminal — so the error-recovery path cannot use it to tell its own boundary from a later,
    foreign one. Prefer the explicit `finalize_scope` stamp; fall back to the seq a `finish:<n>`
    scope names. Returns None when the identity is not discoverable (e.g. a legacy `abort:<n>` scope
    whose finish carries no stamp), which callers must treat as "unknown", never as "foreign".
    """
    stamped = next(
        (
            event
            for event in reversed(events)
            if event.type == EV_RUN_FINISHED
            and (event.data or {}).get("finalize_scope") == scope
        ),
        None,
    )
    if stamped is not None:
        return stamped
    if scope.startswith("finish:"):
        try:
            seq = int(scope.split(":", 1)[1])
        except ValueError:
            return None
        return next(
            (event for event in events if event.seq == seq and event.type == EV_RUN_FINISHED),
            None,
        )
    return None








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
            and data.get("step") == FINALIZE_STEP_LLM_COST
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


# How many finalize passes may block on a failing usage reconcile before the run is allowed to
# terminate with a DISCLOSED cost shortfall. Some outbox failures never self-heal (a foreign or
# undecodable record is never drained), and a run that can never publish completion is worse
# than one whose roll-up says it is short.
_COST_REFRESH_MAX_ATTEMPTS = 3


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
    if _scope_has_step(events, scope, FINALIZE_STEP_REPORT):
        return True
    if scoped_finish_report(events, scope) is not None:
        return True
    if _scope_has_step(events, scope, FINALIZE_STEP_REPORT_BEGUN):
        if not close_ambiguous:
            return None  # it may belong to the live process currently holding paid_effect_guard
        _mark_finalize_step(
            engine,
            scope,
            FINALIZE_STEP_REPORT,
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
        if not _scope_has_step(current, scope, FINALIZE_STEP_REPORT_BEGUN):
            # PLANNED but never dispatched, and THIS process has no writer. Deliberately unresolved
            # (False) rather than closed: a writer-less process must not retire a paid step it
            # cannot perform, because the reachable population is "a writer-equipped process planned
            # the report and crashed before `report_begun`" — and a later writer-equipped resume can
            # still produce it. Closing it here would make that loss permanent to save an orphan
            # that is already bounded: on resume the engine re-finishes under a NEW scope (report
            # unplanned) and `incomplete_finalize_scope` tracks that newer candidate, so the run
            # completes either way. Pinned by tests/test_finalize_paid_claims.py::
            # test_finish_report_no_writer_preflight_never_requires_paid_lock.
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
        _claim_paid_finalize_step(engine, scope, FINALIZE_STEP_REPORT_BEGUN)
        engine._write_report(state, trigger="finish", finalize_scope=scope)
        if scoped_finish_report(engine.store.read_all(), scope) is None:
            _mark_finalize_step(
                engine,
                scope,
                FINALIZE_STEP_REPORT,
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
    if _scope_has_step(events, scope, FINALIZE_STEP_REFLECTION):
        return
    if not _reflection_can_write(engine):
        with engine.store.paid_effect_guard(required=False):
            events = engine.store.read_all()
            if _scope_has_step(events, scope, FINALIZE_STEP_REFLECTION):
                return
            if _scope_has_step(events, scope, FINALIZE_STEP_REFLECTION_BEGUN):
                _mark_finalize_step(
                    engine,
                    scope,
                    FINALIZE_STEP_REFLECTION,
                    outcome="prior_attempt_incomplete_not_replayed",
                )
                return
            # the real reflection implementation immediately returns for these configs.
            # Keep free finalization compatible with filesystems that cannot provide strict locking;
            # ordinary legacy-shaped markers are sufficient because no paid/shared write can happen.
            _mark_finalize_step(engine, scope, FINALIZE_STEP_REFLECTION_BEGUN, outcome="disabled")
            _mark_finalize_step(engine, scope, FINALIZE_STEP_REFLECTION, outcome="disabled")
            return

    with engine.store.paid_effect_guard():
        events = engine.store.read_all()
        if _scope_has_step(events, scope, FINALIZE_STEP_REFLECTION):
            return
        if _scope_has_step(events, scope, FINALIZE_STEP_REFLECTION_BEGUN):
            _mark_finalize_step(
                engine,
                scope,
                FINALIZE_STEP_REFLECTION,
                outcome="prior_attempt_incomplete_not_replayed",
            )
            return

        # Freeze the exact pre-claim state.  Diagnostics appended while the provider is running must
        # not silently change the reflection input selected by this finalization boundary.
        final = fold(events)
        _claim_paid_finalize_step(engine, scope, FINALIZE_STEP_REFLECTION_BEGUN)
        engine._write_reflection_note(final)
        _mark_finalize_step(engine, scope, FINALIZE_STEP_REFLECTION)


def mark_finish_report_complete(engine: "Engine", scope: str) -> None:
    events = engine.store.read_all()
    if (not _finish_report_planned(events, scope)
            or _scope_has_step(events, scope, FINALIZE_STEP_REPORT)):
        return
    if scoped_finish_report(events, scope) is not None:
        _mark_finalize_step(engine, scope, FINALIZE_STEP_REPORT, outcome="completed")


def _build_readmodel_atomic(events, path: Path) -> RunState:
    """Build the rebuildable SQLite projection off to the side, then atomically publish it.

    The body lives in `events/readmodel.py::publish_readmodel`, which the on-demand
    `looplab readmodel` rebuild calls too — one spelling of the temp-build-then-`os.replace`, so a
    live/crashed run's operator-built projection and the engine's own cannot come to differ about
    what a failed build leaves on disk. This wrapper stays because the finalization's own callers
    and its `readmodel_skipped` containment name it.
    """
    return publish_readmodel(events, path)


def _flush_trace_exporter(engine: "Engine") -> bool:
    """Settle accepted async spans before any derived trace reader snapshots ``spans.jsonl``.

    The barrier waits for every accepted row's single delegate ATTEMPT, in-flight ones included —
    not for its success. A row whose attempt raised is absent from the artifact and counted in the
    `looplab.exporter.loss` receipt this same barrier emits, and `True` here does not deny that
    (`core/tracing.py::AsyncJsonlSpanExporter.force_flush`). Its one call site discards this
    return value on purpose — see the comment there: the projection is built either way, because a
    partial trace beats no trace.
    """
    flush = getattr(getattr(engine, "tracer", None), "force_flush", None)
    if not callable(flush):
        return True  # compatibility engines and the historical synchronous exporter
    try:
        return bool(flush(timeout_millis=TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS))
    except Exception:  # noqa: BLE001 - a derived diagnostic cannot undo a domain terminal
        return False


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
    # `state.last_finish_seq` is the GLOBAL latest finish, which need not be THIS scope's. A later
    # bare fallback error finish — one `finalize_scope_quiescent` deliberately ignores — can land
    # while an older scope is being recovered, and stamping `finalization_finished` on it would close
    # a foreign boundary that never ran its own checklist. Acknowledge only when this scope's own
    # finish IS the one that seq names; otherwise abandon just this scoped attempt below and let the
    # fallback own its recovery.
    #
    # An UNDISCOVERABLE identity (`_scope_finish_event` -> None, e.g. a legacy `abort:<n>` scope whose
    # finish carries no `finalize_scope` stamp) keeps today's behaviour on purpose. The conservative
    # direction here is NOT "refuse to acknowledge": that would leave `finalization_pending()` true
    # forever and strand the run as non-terminal in every projection — strictly worse than the narrow
    # mis-acknowledgement this guard exists to prevent. Only a finish proven to be someone else's is
    # skipped.
    if str(finish_data.get("reason") or "").lower() == "error":
        _own_finish = _scope_finish_event(events, scope)
        _finish_is_foreign = (_own_finish is not None
                              and _own_finish.seq != state.last_finish_seq)
        if (state.finished and state.last_finish_seq >= 0 and not _finish_is_foreign
                and not _has_finish_step(events, EV_FINALIZATION_FINISHED, state.last_finish_seq)):
            try:
                engine.store.append(
                    EV_FINALIZATION_FINISHED, {"finish_seq": state.last_finish_seq})
            except Exception:  # noqa: BLE001 - the open scope retries the same close on re-entry
                pass
        latest = engine.store.read_all()
        if not (_scope_has_step(latest, scope, FINALIZE_STEP_COMPLETE)
                or _scope_has_step(latest, scope, FINALIZE_STEP_ABANDONED)):
            try:
                _mark_finalize_step(
                    engine, scope, FINALIZE_STEP_ABANDONED, outcome="error_terminal")
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
            refreshed = engine.store.read_all()
            return refreshed, fold(refreshed)
        tail_seq = cloned.seq
    payload = {
        **finish_data,
        "finalize_scope": scope,
        f"recovered_from_{recovery_kind}": True,
    }
    try:
        engine.store.append(EV_RUN_FINISHED, payload, expected_last_seq=tail_seq)
    except EventStoreConcurrencyError:
        refreshed = engine.store.read_all()
        return refreshed, fold(refreshed)
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
    if not _scope_has_step(events, scope, FINALIZE_STEP_COMPLETE):
        try:
            _mark_finalize_step(engine, scope, FINALIZE_STEP_COMPLETE)
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
        if not _finalize_step_done(events, scope, finish_seq, FINALIZE_STEP_BUDGET, EV_BUDGET):
            # `speculation` answers "did the Card prefetch cost this run real experiment
            # budget?" — `charged_discards` is 0 while the L5 refund holds and positive the
            # moment a prefetch spends a slot the run got nothing for. It replaces the pre-run
            # calibration receipt that positive `speculation_depth` used to require on every
            # workload: an observation of the harm, made every run for free, instead of a
            # six-GPU-run ceremony before any run. See `speculation_budget_observation`.
            #
            # COMPUTED OUTSIDE the append's try. The `except` below exists for the STORE — a
            # concurrent tail, a full disk — which is genuinely retryable on the next finalize pass.
            # A pure projection is not: it would raise identically forever, and because a swallowed
            # raise skips the `finalize_step` marker as well as the append, `requirements_complete`
            # would stay false and the run would never acknowledge its own finalization. That was
            # reachable — the projection reached into `state.nodes` unguarded — so the projection is
            # now total AND the deterministic part of the payload is built before the try, where a
            # defect surfaces as an error instead of an unfinishable run.
            from looplab.search.speculation_quality import speculation_budget_observation
            # `elapsed_s` is the RUN's duration and is therefore read from the LOG, not from this
            # process's clock. A run that is stopped and wrapped up later by `looplab finalize` is
            # finalized in a DIFFERENT process, so `time.time() - start_time` there measures the
            # wrap-up: measured on a real ~4.6-minute run finalized separately, it published
            # `elapsed_s: 0.027`. That is the NORMAL shape for a long GPU run — the whole wrap-up
            # policy exists for it — so the durable receipt has to be correct for it. `ts` is on
            # every row of every log version, so this is exact on OLD logs too.
            # `process_s` keeps what the field used to hold, now under a name that says what it
            # measures: the engine loop when the run finished in one process, the wrap-up alone when
            # it did not. The two side by side are what makes a re-divergence visible instead of
            # silent. Both are total (no raise) — see the paragraph above about the append's `try`.
            process_s = max(0.0, time.time() - start_time)
            wall_clock_s = run_wall_clock_seconds(events)
            # Minted through the shared constructor, not a local dict literal: the quality reader
            # recomputes this receipt field for field and rejects any key set that is not exactly
            # `FINALIZE_BUDGET_FIELDS`, so the payload shape and its rounding are one spelling in
            # `events/finalize_protocol.py` rather than two hand-synced ones (doc 25 SE-01).
            budget_payload = budget_receipt(
                # None only for a log whose rows carry no usable `ts` at all (synthetic/hand-built) —
                # fall back to the process measurement rather than publishing a confident 0.0.
                elapsed_s=process_s if wall_clock_s is None else wall_clock_s,
                process_s=process_s,
                eval_s=completed.total_eval_seconds,
                nodes=len(completed.nodes),
                speculation=speculation_budget_observation(completed),
                finalize_scope=scope,
                finish_seq=finish_seq,
            )
            try:
                engine.store.append(EV_BUDGET, budget_payload)
                _mark_finalize_step(engine, scope, FINALIZE_STEP_BUDGET)
            except Exception:  # noqa: BLE001 - exact effect/marker detection makes retry safe
                pass

        events = engine.store.read_all()
        if not _finalize_step_done(
                events, scope, finish_seq, FINALIZE_STEP_DIVERSITY, EV_DIVERSITY_ARCHIVE):
            try:
                archive = dict(DiversityArchive(engine.archive_resolution).summary(completed))
                engine.store.append(EV_DIVERSITY_ARCHIVE, {
                    **archive,
                    "finalize_scope": scope,
                    "finish_seq": finish_seq,
                })
                _mark_finalize_step(engine, scope, FINALIZE_STEP_DIVERSITY)
            except Exception:  # noqa: BLE001 - retry missing deterministic step later
                pass

        events = engine.store.read_all()
        if not _finalize_step_done(events, scope, finish_seq, FINALIZE_STEP_CASE):
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
                _mark_finalize_step(engine, scope, FINALIZE_STEP_CASE)
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
            # These FOLDED card_enriched appends land inside the open finalize scope, so
            # `finalize_scope_quiescent` allow-lists EV_CARD_ENRICHED as one of this finalization's
            # OWN effects — see the rationale there. Without that entry a crash between this line and
            # the completion markers permanently abandoned a non-`finalization_required` scoped finish.
            engine._sync_card_enrichments(fold(engine.store.read_all()))
        except Exception:  # noqa: BLE001 - advisory Card links must never block terminal completion
            pass

        # reflection must precede claim curation so this run's durable lessons are visible;
        # every steward still precedes llm_cost so its provider usage enters the terminal roll-up.
        if getattr(engine, "_cross_run_curation", False):
            # The INDEPENDENT stewards each get their own failure boundary. Concept and claim curation
            # are the baseline pair. Task faceting is a third, explicitly scheduled call because its
            # proposal currently has no live behavior consumer; old/custom Engine shims lack the new
            # attribute, so getattr(..., True) preserves their historical all-three treatment.
            # These calls used to share one boundary: a concept-ledger exception skipped every later
            # governance output while the run still published complete. Each scheduled steward now
            # leaves a receipt, so "this task has no facets" can be told apart from "faceting never
            # ran because curation raised first" when the facet steward is enabled.
            #
            # Deliberately NOT gated on `_finalize_step_done`: idempotency lives in the stores
            # (append-only aliases, per-task facets) and re-running a steward on resume IS the
            # intended recovery, so these receipts are observability, not skip markers.
            # `EV_FINALIZE_STEP` is a DIAGNOSTIC event (types.py), hence fold-ignored — the extra
            # appends are splice-neutral BY CONSTRUCTION and cannot perturb replay state.
            def _steward_receipt(step: str, **data) -> None:
                # A receipt is diagnostics: never let its own append undo the isolation above by
                # propagating out of a steward's handler and aborting terminal completion.
                try:
                    _mark_finalize_step(engine, scope, step, **data)
                except Exception:  # noqa: BLE001 - see above
                    pass

            final = None
            try:
                final = fold(engine.store.read_all())
            except Exception as exc:  # noqa: BLE001 - a fold failure disables every steward at once,
                _steward_receipt(      # so it is recorded once rather than once per scheduled steward
                    "stewards", outcome="unavailable", error=str(exc)[:300])
            if final is not None:
                stewards = [
                    ("concept_curation", engine._store_concept_curation),
                    ("claim_curation", engine._store_claim_curation),  # ratify/reject/pin
                ]
                if getattr(engine, "_task_facets_finalize", True):
                    stewards.append(("task_facets", engine._store_task_facets))  # once per task
                for step, steward in stewards:
                    try:
                        outcome = steward(final)
                    except Exception as exc:  # noqa: BLE001 — steward failure must not prevent
                        _steward_receipt(     # terminal completion
                            step, outcome="error", error=str(exc)[:300])
                    else:
                        # Old/custom Engine shims returned None. Preserve their successful receipt while
                        # allowing the production stewards to disclose the exact bounded outcome they
                        # already wrote to their governance log.
                        receipt_outcome = (
                            outcome if isinstance(outcome, str)
                            and outcome in _STEWARD_RECEIPT_OUTCOMES
                            else "completed"
                        )
                        _steward_receipt(step, outcome=receipt_outcome)

        # PART IV §22.4 RATIFICATION — the consumer the concept steward never had. It runs OUTSIDE
        # the `_cross_run_curation` block on purpose: the proposals it applies are DURABLE, so a
        # portfolio whose steward is now switched off (or whose LLM was unreachable this time) still
        # has yesterday's recorded judgements to ratify, and the two flags stay independently
        # meaningful — "keep proposing" and "keep applying" are different operator decisions.
        #
        # Deliberately NOT a `_mark_finalize_step` receipt and NOT a background task. It appends no
        # run events at all (see `concept_tidy`'s module docstring: a cross-run policy decision is
        # not a run fact, and `fold` could not honestly re-derive one), so it cannot perturb replay,
        # cannot perturb `QUIET_FINALIZATION_SUFFIX`, and needs no `BACKGROUND_APPENDABLE` entry.
        # Its audit lives in cross-run memory, where it survives this run's deletion. Main task,
        # after the steward, so a proposal bought seconds ago is ratifiable in the same finalize.
        if getattr(engine, "_concept_tidy", False) and getattr(engine, "memory_dir", ""):
            try:
                import datetime as _dt

                from looplab.engine.concept_tidy import ratify_concept_merges
                ratify_concept_merges(
                    engine.memory_dir,
                    at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
            except Exception:  # noqa: BLE001 — taxonomy hygiene must never block terminal completion
                pass

        events = engine.store.read_all()
        cost_step_done = _finalize_step_done(
            events, scope, finish_seq, FINALIZE_STEP_LLM_COST, EV_LLM_COST)
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
                _mark_finalize_step(engine, scope, FINALIZE_STEP_LLM_COST)
            elif not cost_refresh_ok:
                # BOUNDED, DISCLOSED degradation. Blocking completion on the refresh is right for a
                # transient outbox/append conflict, but some failures never self-heal: `_drain_outbox`
                # leaves `complete=False` forever for an undecodable/foreign `.json` record (it is never
                # removed) and for a same-id event whose payload differs. `emit_llm_cost` then returns
                # False on every pass, the staleness boundary never advances, and the run re-runs the
                # whole finalize body — including the PAID stewards — on every resume while never
                # publishing `finalization_finished`. That inverts emit_llm_cost's own contract
                # ("telemetry never aborts domain finalization"). Retry a bounded number of times, then
                # record WHY the roll-up is short and let the run terminate: an under-counted cost that
                # says so beats a run that can never finish.
                _attempts = 1 + sum(
                    1 for event in events
                    if event.type == EV_FINALIZE_STEP
                    and event.data.get("scope") == scope
                    and event.data.get("step") == "llm_cost_refresh_failed")
                _mark_finalize_step(engine, scope, "llm_cost_refresh_failed", attempt=_attempts)
                if _attempts >= _COST_REFRESH_MAX_ATTEMPTS:
                    _mark_finalize_step(
                        engine, scope, FINALIZE_STEP_LLM_COST,
                        degraded="usage reconcile did not converge; the roll-up may under-count "
                                 "spend still stranded in .llm-usage-outbox",
                        attempts=_attempts)
                    cost_refresh_ok = True

        latest_events = engine.store.read_all()
        requirements = (
            (
                not _finish_report_planned(latest_events, scope)
                or _finalize_step_done(latest_events, scope, finish_seq, FINALIZE_STEP_REPORT)
            ),
            _finalize_step_done(latest_events, scope, finish_seq, FINALIZE_STEP_BUDGET, EV_BUDGET),
            _finalize_step_done(
                latest_events, scope, finish_seq, FINALIZE_STEP_DIVERSITY, EV_DIVERSITY_ARCHIVE),
            _finalize_step_done(latest_events, scope, finish_seq, FINALIZE_STEP_CASE),
            _finalize_step_done(latest_events, scope, finish_seq, FINALIZE_STEP_REFLECTION),
            # A NEEDED cost refresh that failed to reconcile must block completion, preserving the initial
            # cost step's "block until durable" guarantee. The step marker persists from a prior pass
            # (cost_step_done), so without this a stale-refresh whose emit_llm_cost returns False would
            # still read "done" and the run would publish completion with an un-folded usage delta stranded
            # in the outbox — a silent cost under-count on a run that reports itself finalized. The next
            # finalize pass re-emits; a transient outbox/append conflict clears without a wedge.
            _finalize_step_done(
                latest_events, scope, finish_seq, FINALIZE_STEP_LLM_COST, EV_LLM_COST)
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
        # The barrier stays FIRST — nothing may snapshot spans.jsonl before the rows the exporter has
        # already accepted have had their chance to land. It does NOT gate the projection, though.
        # `force_flush` returning False bounds only the CALLER's wait budget (`core/tracing.py`: "The
        # timeout bounds only the caller's wait; Python cannot safely interrupt a worker already in
        # filesystem I/O"), so it never means the rows on disk are invalid, and anything genuinely
        # dropped gets its own `looplab.exporter.loss` receipt. Raising here landed in the `except`
        # below, which returns immediately under the modern protocol — so a slow flush that lost
        # nothing cost the run BOTH `trace.json` and `tree.html`, two independently rebuildable
        # projections that each already degrade best-effort on their own a few lines down.
        _flush_trace_exporter(engine)
        trace_spans, trace_total = load_span_tail(
            engine.run_dir / "spans.jsonl", TRACE_VIEW_SPAN_CAP)
        trace_view = build_trace_view(
            final,
            # load_span_tail already normalized every retained span. Hydrate ONLY that bounded window:
            # an input_from ancestor outside it is deliberately marked input_partial rather than making
            # finalization materialize the whole diagnostic log. build_trace_view's normalization pass
            # remains required because reconstructed input is new material that has not crossed the
            # projection budget yet (see hydrate_inputs' `_normalized` contract).
            hydrate_inputs(trace_spans, _normalized=True),
            total_spans=trace_total,
            span_cap=TRACE_VIEW_SPAN_CAP,
        )
    except Exception:
        if modern_protocol:
            return final
        raise  # compatibility for an in-flight legacy scope; its complete marker stays absent
    try:
        atomic_write_bytes(engine.run_dir / "trace.json", trace_projection_json_bytes(trace_view))
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
            # `priced_calls` MUST travel with the roll-up. The folded total carries it, but this
            # event did not, and `ui/src/format.js::fmtCost` reads a missing key as ZERO priced —
            # so the timeline printed "unpriced" for a run that really spent $1.96 across 156
            # priced calls, while the Inspector (which reads the folded state) printed the money on
            # the same run at the same moment. A confident false "spend is unknown" is worse than
            # the "$0.00" it replaced: that at least did not claim to know.
            "priced_calls": int(total.get("priced_calls", 0)),
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
