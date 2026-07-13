"""Run finalization (extracted from the tail of Engine.run(), a pure move): the budget
summary, diversity-archive dump, LLM cost roll-up, cross-run case store + reflection note,
the derived SQLite read-model, and the trace.json / tree.html UI projections. Event emission
order is replay-gated; the LLM summary intentionally follows reflection so it includes the final
possible paid calls.

Layering: the trace/html projections moved from `serve/` to their dependency-true home
`events/` (they are pure RunState -> string readers), so the engine no longer touches `serve`
at all — the imports below are ordinary downward engine -> events imports."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import orjson

from looplab.core.models import RunState
from looplab.engine.costs import in_memory_cost_total, reconcile_cost_accountants
from looplab.events.htmlview import render_html
from looplab.events.readmodel import build_readmodel
from looplab.events.replay import fold
from looplab.events.traceview import build_trace_view, load_spans
from looplab.events.types import (EV_BUDGET, EV_DIVERSITY_ARCHIVE, EV_FINALIZE_STEP, EV_LLM_COST,
                                  EV_READMODEL_SKIPPED, EV_REPORT_GENERATED, EV_RUN_ABORT,
                                  EV_RUN_FINISHED)
from looplab.search.archive import DiversityArchive

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


def incomplete_finalize_scope(events) -> str | None:
    """Return the latest new-format terminal scope until its projection marker is durable.

    Legacy terminal events have no scope and remain compatible: they are treated as completed rather
    than retroactively replaying external case/reflection side effects after an upgrade.
    """
    candidate = None
    for event in events:
        data = event.data or {}
        is_begun = event.type == EV_FINALIZE_STEP and data.get("step") == "begun"
        is_finished = (
            event.type == EV_RUN_FINISHED
            and str(data.get("reason") or "").lower() != "error"
        )
        scope = data.get("scope") if is_begun else data.get("finalize_scope")
        if (is_begun or is_finished) and isinstance(scope, str) and scope:
            candidate = (event.seq, scope)
    if candidate is None:
        return None
    _, scope = candidate
    complete = any(event.type == EV_FINALIZE_STEP
                   and (event.data or {}).get("scope") == scope
                   and (event.data or {}).get("step") == "complete" for event in events)
    return None if complete else scope


def _terminal_data_for_scope(events, scope: str) -> tuple[dict, str]:
    """Recover the exact terminal payload for an incomplete finalization boundary."""
    begun = next(
        (event for event in reversed(events) if event.type == EV_FINALIZE_STEP
         and (event.data or {}).get("scope") == scope
         and (event.data or {}).get("step") == "begun"), None)
    finish_data = (begun.data or {}).get("finish_data") if begun is not None else None
    if isinstance(finish_data, dict):
        return dict(finish_data), "finalize_begun"

    prior_finish = next(
        (event for event in reversed(events) if event.type == EV_RUN_FINISHED
         and (event.data or {}).get("finalize_scope") == scope
         and str((event.data or {}).get("reason") or "").lower() != "error"), None)
    if prior_finish is not None:
        return dict(prior_finish.data or {}), "superseding_control"

    # Compatibility for the short-lived pre-payload marker format. Abort scopes retain their exact
    # operator meaning; an untyped natural terminal remains visibly recovered rather than guessed.
    reason = "aborted" if scope.startswith("abort:") else "recovered_terminal"
    return {"reason": reason}, "legacy_finalize_begun"


def _scope_is_effective_terminal(events, state: RunState, scope: str) -> bool:
    """Whether the currently effective terminal is this scope's successful publication.

    A historical scoped success is insufficient: a later resume can reopen it, and an outer guard
    can append a later ``reason=error`` after projection failed. Both require republishing the exact
    staged payload before wrap-up continues.
    """
    if not state.finished:
        return False
    latest = next((event for event in reversed(events) if event.type == EV_RUN_FINISHED), None)
    if latest is None:
        return False
    data = latest.data or {}
    return (data.get("finalize_scope") == scope
            and str(data.get("reason") or "").lower() != "error")


def _finalize_begun(events, scope: str):
    return next(
        (event for event in reversed(events) if event.type == EV_FINALIZE_STEP
         and (event.data or {}).get("scope") == scope
         and (event.data or {}).get("step") == "begun"),
        None,
    )


def _finish_report_planned(events, scope: str) -> bool:
    begun = _finalize_begun(events, scope)
    return bool(begun is not None and (begun.data or {}).get("finish_report_planned") is True)


def _scoped_finish_report(events, scope: str):
    return next(
        (event for event in reversed(events) if event.type == EV_REPORT_GENERATED
         and (event.data or {}).get("finalize_scope") == scope
         and (event.data or {}).get("trigger") == "finish"),
        None,
    )


def _finalize_scope(events, state: RunState) -> tuple[str, int]:
    """Stable identity for one logical wrap-up, including retries after run_finished(error)."""
    finished = next((event for event in reversed(events)
                     if event.type == EV_RUN_FINISHED
                     and str((event.data or {}).get("reason") or "").lower() != "error"), None)
    if finished is not None:
        explicit = (finished.data or {}).get("finalize_scope")
        if isinstance(explicit, str) and explicit:
            return explicit, finished.seq
    if state.stop_requested:
        abort = next((event for event in reversed(events) if event.type == EV_RUN_ABORT), None)
        if abort is not None:
            return f"abort:{abort.seq}", abort.seq
    seq = finished.seq if finished is not None else -1
    return f"finish:{seq}", seq


def _scope_anchor_seq(events, scope: str) -> int:
    terminal = next(
        (event for event in reversed(events) if event.type == EV_RUN_FINISHED
         and (event.data or {}).get("finalize_scope") == scope
         and str((event.data or {}).get("reason") or "").lower() != "error"),
        None,
    )
    if terminal is not None:
        return terminal.seq
    begun = _finalize_begun(events, scope)
    return begun.seq if begun is not None else -1


def _finalize_step_done(events, scope: str, anchor_seq: int, step: str,
                        effect_type: str | None = None) -> bool:
    if any(event.type == EV_FINALIZE_STEP
           and (event.data or {}).get("scope") == scope
           and (event.data or {}).get("step") == step for event in events):
        return True
    if effect_type is not None:
        # Backward-compatible recovery: a pre-marker build may already have emitted the effect after
        # this logical finalize's anchor. Treat it as complete rather than duplicating it on retry.
        return any(event.seq > anchor_seq and event.type == effect_type
                   and (event.data or {}).get("finalize_scope") in (None, scope)
                   for event in events)
    return False


def _mark_finalize_step(engine: "Engine", scope: str, step: str, **data) -> None:
    engine.store.append(EV_FINALIZE_STEP, {"scope": scope, "step": step, **data})


def ensure_finish_report(engine: "Engine", events, scope: str, *, state=None) -> bool:
    """Resolve one planned paid finish report without an ambiguous provider retry.

    ``report_begun`` is the at-most-once boundary. Before it, recovery can safely make the first
    call. After it, a matching scoped report is reused; without one, recovery records an explicit
    incomplete outcome and never risks buying the same report twice.
    """
    events = list(events)
    if not _finish_report_planned(events, scope):
        return True
    begun = _finalize_begun(events, scope)
    anchor_seq = begun.seq if begun is not None else -1
    if _finalize_step_done(events, scope, anchor_seq, "report"):
        return True
    if _scoped_finish_report(events, scope) is not None:
        _mark_finalize_step(engine, scope, "report", outcome="durable_report_reused")
        return True
    if _finalize_step_done(events, scope, anchor_seq, "report_begun"):
        _mark_finalize_step(
            engine,
            scope,
            "report",
            outcome="prior_attempt_incomplete_not_replayed",
        )
        return True
    if getattr(engine, "report_writer", None) is None:
        # No attempt has started, so leave the scope pending. A later re-entry with the configured
        # writer can still make exactly one safe first call.
        return False

    _mark_finalize_step(engine, scope, "report_begun")
    if state is None:
        # Reconstruct the exact pre-terminal snapshot captured by begun; later error/control events
        # must not alter the report input on recovery.
        state = fold([event for event in events if event.seq <= anchor_seq])
    engine._write_report(state, trigger="finish", finalize_scope=scope)
    latest = engine.store.read_all()
    if _scoped_finish_report(latest, scope) is not None:
        _mark_finalize_step(engine, scope, "report", outcome="completed")
    else:
        _mark_finalize_step(
            engine,
            scope,
            "report",
            outcome="attempt_returned_without_durable_report",
        )
    return True


def finalize_run(engine: "Engine", *, entry_finished: bool, start_time: float) -> RunState:
    """Finalize a run() invocation and return the final RunState (the fold, or the read-model's
    equivalent fold). `entry_finished` is whether the run was ALREADY finished when run() was
    entered; `start_time` is run()'s wall-clock entry time (for the budget summary)."""
    # Finalize only on real completion (not when paused for approval / idempotent
    # resume of a done run).
    events = engine.store.read_all()
    completed = fold(events)
    pending_terminal_scope = incomplete_finalize_scope(events)
    report_ready = True
    if pending_terminal_scope is not None:
        report_ready = ensure_finish_report(engine, events, pending_terminal_scope)
        events = engine.store.read_all()
        completed = fold(events)
    if (pending_terminal_scope is not None and report_ready
            and not _scope_is_effective_terminal(events, completed, pending_terminal_scope)):
        # Recover either side of the durable boundary: an old resume/reopen may have superseded the
        # scoped terminal event, a hard kill may have landed after `begun` but before it, or a caller
        # may have appended run_finished(reason=error) after the scoped append failed. Fold marks that
        # last case finished too, but it is not this boundary's successful terminal event. In every
        # case publish the original payload in the original scope and go straight to wrap-up.
        finish_data, recovery_kind = _terminal_data_for_scope(events, pending_terminal_scope)
        engine.store.append(EV_RUN_FINISHED, {
            **finish_data,
            "finalize_scope": pending_terminal_scope,
            f"recovered_from_{recovery_kind}": True,
        })
        events = engine.store.read_all()
        completed = fold(events)
    pending_terminal_ready = (
        pending_terminal_scope is None
        or (report_ready
            and _scope_is_effective_terminal(events, completed, pending_terminal_scope))
    )
    if not entry_finished and completed.finished and pending_terminal_ready:
        cur = completed
        scope, anchor_seq = _finalize_scope(events, cur)
        if not _finalize_step_done(events, scope, anchor_seq, "budget", EV_BUDGET):
            engine.store.append(EV_BUDGET, {                   # budget summary (I13 + #2)
                "elapsed_s": round(time.time() - start_time, 3),
                "eval_s": round(cur.total_eval_seconds, 3),
                "nodes": len(cur.nodes),
                "finalize_scope": scope,
            })
            _mark_finalize_step(engine, scope, "budget")
        if not _finalize_step_done(
                engine.store.read_all(), scope, anchor_seq, "diversity", EV_DIVERSITY_ARCHIVE):
            archive = dict(DiversityArchive(engine.archive_resolution).summary(cur))
            archive["finalize_scope"] = scope
            engine.store.append(EV_DIVERSITY_ARCHIVE, archive)  # diversity archive (I22)
            _mark_finalize_step(engine, scope, "diversity")
        if not _finalize_step_done(engine.store.read_all(), scope, anchor_seq, "case"):
            engine._store_case(fold(engine.store.read_all()))   # cross-run memory (I19)
            _mark_finalize_step(engine, scope, "case")
        if not _finalize_step_done(engine.store.read_all(), scope, anchor_seq, "reflection"):
            if _finalize_step_done(
                    engine.store.read_all(), scope, anchor_seq, "reflection_begun"):
                # Reflection writes several shared files and may spend LLM calls. If it raised after
                # any partial external write, replaying it cannot be exactly-once. The durable begun
                # marker chooses at-most-once (auditable partial best-effort memory) over duplicated
                # lessons/meta-notes/cost on every finalize retry.
                engine.store.append(EV_FINALIZE_STEP, {
                    "scope": scope, "step": "reflection",
                    "outcome": "prior_attempt_incomplete_not_replayed",
                })
            else:
                _mark_finalize_step(engine, scope, "reflection_begun")
                engine._write_reflection_note(                  # E4 cross-run meta-review prior
                    fold(engine.store.read_all()))
                _mark_finalize_step(engine, scope, "reflection")
        # Reflection itself may call an LLM. Its paid deltas are durable immediately, and the
        # presentation summary belongs after the final possible LLM consumer.
        if not _finalize_step_done(
                engine.store.read_all(), scope, anchor_seq, "llm_cost", EV_LLM_COST):
            if emit_llm_cost(engine, finalize_scope=scope):
                _mark_finalize_step(engine, scope, "llm_cost")

    # The SQLite read-model is a DERIVED, rebuildable cache that nothing in-process reads (the UI
    # folds events.jsonl / reads trace.json). On a FUSE/S3 run dir (JupyterHub geesefs) sqlite's
    # byte-range locks are unsupported and the write can raise `database is locked` / `disk I/O
    # error` — which must NOT abort an otherwise-finished run. Build best-effort; the run state we
    # actually need comes from the event fold regardless.
    try:
        final = build_readmodel(engine.store.read_all(), engine.run_dir / "readmodel.sqlite")
    except Exception as e:  # noqa: BLE001 - derived cache; a FUSE sqlite failure must not kill finalize
        final = fold(engine.store.read_all())
        try:
            engine.store.append(EV_READMODEL_SKIPPED, {"error": str(e)[:300]})
        except Exception:  # noqa: BLE001 - even the audit note is best-effort
            pass
    # UI projection (ADR-17): join the research tree (events) to its execution detail
    # (spans) -> trace.json for the React UI + an inline span tree in the static HTML.
    tv = build_trace_view(final, load_spans(engine.run_dir / "spans.jsonl"))
    (engine.run_dir / "trace.json").write_bytes(orjson.dumps(tv))
    (engine.run_dir / "tree.html").write_text(render_html(final, tv), encoding="utf-8")
    # The marker is deliberately last. A hard kill anywhere above leaves the same explicit scope
    # incomplete, so command recovery re-enters wrap-up without reopening search. Legacy runs have
    # no explicit scope and are not retroactively finalized after an upgrade.
    latest_events = engine.store.read_all()
    pending_scope = incomplete_finalize_scope(latest_events)
    if pending_scope is not None:
        # Evaluate completion against the pending boundary itself. A prior run cycle can leave an
        # older successful terminal in the log; its completed steps must never complete this scope.
        scope = pending_scope
        anchor_seq = _scope_anchor_seq(latest_events, scope)
        required = (
            (not _finish_report_planned(latest_events, scope)
             or _finalize_step_done(latest_events, scope, anchor_seq, "report")),
            _finalize_step_done(latest_events, scope, anchor_seq, "budget", EV_BUDGET),
            _finalize_step_done(
                latest_events, scope, anchor_seq, "diversity", EV_DIVERSITY_ARCHIVE),
            _finalize_step_done(latest_events, scope, anchor_seq, "case"),
            _finalize_step_done(latest_events, scope, anchor_seq, "reflection"),
            _finalize_step_done(latest_events, scope, anchor_seq, "llm_cost", EV_LLM_COST),
        )
        if all(required):
            engine.store.append(EV_FINALIZE_STEP, {"scope": pending_scope, "step": "complete"})
    return final


def emit_llm_cost(engine: "Engine", *, finalize_scope: str | None = None) -> bool:
    """Emit a presentation summary; return false while a known delta is not durable."""
    try:
        tracked = isinstance(getattr(engine, "_llm_cost_bindings", None), dict)
        if tracked and not reconcile_cost_accountants(engine):
            return False
        durable = fold(engine.store.read_all()).llm_cost
        # Directly-constructed test fakes and old integrations can call this helper without the
        # Engine constructor. Keep their historical in-memory roll-up instead of returning empty.
        total = durable or in_memory_cost_total(engine)
        if not total:
            return True
        payload = {
            "cost": round(float(total.get("cost", 0.0)), 6),
            "calls": int(total.get("calls", 0)),
            "prompt_tokens": int(total.get("prompt_tokens", 0)),
            "completion_tokens": int(total.get("completion_tokens", 0)),
            "total_tokens": int(total.get("total_tokens", 0)),
            "finalize_scope": finalize_scope,
        }
        if any(payload[key] for key in ("cost", "calls", "prompt_tokens",
                                        "completion_tokens", "total_tokens")):
            engine.store.append(EV_LLM_COST, payload)
        return True
    except Exception:  # noqa: BLE001 - cost telemetry must NEVER abort run finalization
        return False
