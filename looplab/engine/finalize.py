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
from looplab.events.traceview import build_trace_view, load_spans
from looplab.events.types import (
    EV_BUDGET,
    EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FINALIZE_STEP,
    EV_LESSONS_DISTILLED,
    EV_LLM_COST,
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
        if event.type in {"llm_usage", "command_ack", EV_READMODEL_SKIPPED,
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


def _mark_finalize_step(engine: "Engine", scope: str, step: str, **data) -> None:
    engine.store.append(EV_FINALIZE_STEP, {"scope": scope, "step": step, **data})


def ensure_finish_report(engine: "Engine", events, scope: str, *, state=None) -> bool:
    """Resolve one planned paid finish report without an ambiguous provider retry.

    The successful ``report`` marker is deliberately deferred until ``run_finished`` is durable so
    the upstream replay contract can keep ``report_generated`` immediately adjacent to its finish.
    """
    events = list(events)
    if not _finish_report_planned(events, scope):
        return True
    if _scope_has_step(events, scope, "report"):
        return True
    if scoped_finish_report(events, scope) is not None:
        return True
    if _scope_has_step(events, scope, "report_begun"):
        _mark_finalize_step(
            engine,
            scope,
            "report",
            outcome="prior_attempt_incomplete_not_replayed",
        )
        return True
    if getattr(engine, "report_writer", None) is None:
        return False

    _mark_finalize_step(engine, scope, "report_begun")
    begun = _finalize_begun(events, scope)
    if state is None:
        anchor = begun.seq if begun is not None else -1
        state = fold([event for event in events if event.seq is None or event.seq < anchor])
    engine._write_report(state, trigger="finish", finalize_scope=scope)
    if scoped_finish_report(engine.store.read_all(), scope) is None:
        _mark_finalize_step(
            engine,
            scope,
            "report",
            outcome="attempt_returned_without_durable_report",
        )
    return True


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
    latest = engine.store.read_all()
    report = scoped_finish_report(latest, scope)
    tail_seq = latest[-1].seq if latest else -1
    if report is not None and report.seq != tail_seq:
        # No provider call: republish the already-durable content only when diagnostics followed it,
        # restoring the report->finish adjacency required by replay.
        cloned = engine.store.append(
            EV_REPORT_GENERATED,
            dict(report.data or {}),
            expected_last_seq=tail_seq,
        )
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
                engine._store_case(fold(events))
                _mark_finalize_step(engine, scope, "case")
            except Exception:  # noqa: BLE001 - case store is an idempotent upsert
                pass

        events = engine.store.read_all()
        if not _finalize_step_done(events, scope, finish_seq, "reflection"):
            if _finalize_step_done(events, scope, finish_seq, "reflection_begun"):
                _mark_finalize_step(
                    engine,
                    scope,
                    "reflection",
                    outcome="prior_attempt_incomplete_not_replayed",
                )
            else:
                _mark_finalize_step(engine, scope, "reflection_begun")
                # This can write several shared files and spend LLM tokens. Let a failure propagate
                # like a process crash; the begun marker makes the next entry at-most-once.
                engine._write_reflection_note(fold(engine.store.read_all()))
                _mark_finalize_step(engine, scope, "reflection")

        events = engine.store.read_all()
        if not _finalize_step_done(events, scope, finish_seq, "llm_cost", EV_LLM_COST):
            if emit_llm_cost(
                engine,
                finalize_scope=scope,
                finish_seq=finish_seq,
            ):
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
            _finalize_step_done(latest_events, scope, finish_seq, "llm_cost", EV_LLM_COST),
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

    try:
        trace_view = build_trace_view(final, load_spans(engine.run_dir / "spans.jsonl"))
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
