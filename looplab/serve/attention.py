"""Read-only, redacted attention projection for the owner UI.

The append-only run logs remain the source of truth.  This module deliberately emits a small
allow-listed envelope instead of forwarding event ``data``: goals, errors, paths, code, prompts and
provider material never enter the attention feed.  Stable opaque ids are generation + seq + kind
derived, so polling/replay does not duplicate a signal and reset/replacement cannot alias the old one.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Iterable

from looplab.core.models import Event, NodeStatus
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.replay import fold
from looplab.events.types import (
    EV_APPROVAL_REQUESTED,
    EV_FINALIZATION_FINISHED,
    EV_NODE_FAILED,
    EV_PAUSE,
    EV_RUN_FINISHED,
    EV_SPEC_APPROVAL_REQUESTED,
)
from looplab.serve.run_commands import run_generation_token

_IGNORED_FAILURE_REASONS = {"aborted", "cancelled", "proxy_skipped", "superseded"}
_BUDGET_REASONS = {
    "time_budget": "The run reached its wall-clock budget.",
    "eval_budget": "The run reached its evaluation-compute budget.",
}
_FAILED_FINISH_REASONS = {"error", "leakage", "no_eligible_candidate"}
_STOPPED_FINISH_REASONS = {"aborted", "finalized"}


def _integer(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _timestamp(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0


def _run_id(value) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if text and len(text) <= 255 and not any(ord(char) < 32 or ord(char) == 127 for char in text) else ""


def _opaque_id(run_id: str, generation: str, seq: int, kind: str) -> str:
    raw = json.dumps(
        {"v": 1, "run_id": run_id, "generation": generation, "seq": seq, "kind": kind},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _item(run_id: str, generation: str, event: Event, kind: str, *, severity: str,
          title: str, detail: str, browser: bool, active: bool = False,
          node_id: int | None = None, node_generation: int | None = None,
          derived: bool = False) -> dict | None:
    seq = _integer(event.seq)
    rid = _run_id(run_id)
    if seq is None or not rid or len(generation) != 64:
        return None
    out = {
        "id": _opaque_id(rid, generation, seq, kind),
        "kind": kind,
        "severity": severity,
        "title": title,
        "detail": detail,
        "run_id": rid,
        "generation": generation,
        "seq": seq,
        "created": _timestamp(event.ts),
        "browser": bool(browser),
        "active": bool(active),
        "derived": bool(derived),
    }
    if node_id is not None:
        out["node_id"] = node_id
    if node_generation is not None:
        out["node_generation"] = node_generation
    return out


def project_event_attention(run_id: str, events: Iterable[Event]) -> dict:
    """Return cached event-derived items plus the minimum runtime flags needed for a live probe."""
    rows = list(events)
    generation = run_generation_token(rows)
    if not generation:
        return {"generation": "", "items": [], "tail_seq": -1, "tail_ts": 0.0,
                "runtime": {}}
    state = fold(rows)
    items: list[dict] = []
    events_by_seq: dict[int, list[Event]] = {}
    for event in rows:
        seq = _integer(event.seq)
        if seq is not None:
            events_by_seq.setdefault(seq, []).append(event)

    def accepted_event(seq, event_type: str) -> Event | None:
        anchor = _integer(seq)
        if anchor is None:
            return None
        return next((event for event in events_by_seq.get(anchor, [])
                     if event.type == event_type), None)

    # Exact pending result approval.  A request that has already been granted/reset/tombstoned is not
    # actionable and is intentionally omitted even if its historical event remains in the log.
    subject = _integer(state.approval_subject)
    subject_generation = _integer(state.approval_generation)
    node = state.nodes.get(subject) if subject is not None else None
    if (state.awaiting_approval and subject is not None and subject_generation is not None
            and node is not None and not node.tombstoned
            and node.attempt == subject_generation):
        request = accepted_event(state.approval_request_seq, EV_APPROVAL_REQUESTED)
        if request is not None:
            item = _item(
                run_id, generation, request, "approval", severity="action",
                title="Experiment approval needed",
                detail=f"Review the pending decision for experiment #{subject}.",
                browser=True, active=True, node_id=subject, node_generation=subject_generation,
            )
            if item:
                items.append(item)
    elif state.awaiting_approval:
        request = accepted_event(state.approval_request_seq, EV_APPROVAL_REQUESTED)
        if request is not None:
            item = _item(
                run_id, generation, request, "approval_incomplete", severity="danger",
                title="Approval state needs inspection",
                detail="The pending approval has no verifiable experiment lifecycle. Inspect Events.",
                browser=False, active=True,
            )
            if item:
                items.append(item)

    if state.spec_approval_requested and not state.spec_confirmed:
        request = accepted_event(state.spec_approval_request_seq, EV_SPEC_APPROVAL_REQUESTED)
        if request is not None:
            item = _item(
                run_id, generation, request, "spec_approval", severity="action",
                title="Evaluation spec approval needed",
                detail="Review and ratify the pending evaluation specification.",
                browser=True, active=True,
            )
            if item:
                items.append(item)

    # Count only terminal failure events that still describe the current lifecycle of a failed node.
    # Expected skips/superseded attempts do not constitute a failure spike.  One signal is emitted per
    # deterministic group of three; the fourth/fifth failure update the count without creating spam.
    current_failures: dict[tuple[int, int], Event] = {}
    for nid, current in state.nodes.items():
        if (current.tombstoned or nid in state.aborted_nodes
                or current.status is not NodeStatus.failed):
            continue
        # Replay records the first accepted terminal for this lifecycle. A later duplicate failure
        # (or a conflicting terminal that first-terminal-wins ignored) cannot rotate the notification
        # identity or turn an aborted/ignored lifecycle into a fresh spike.
        event = accepted_event(current.terminal_event_seq, EV_NODE_FAILED)
        if event is None:
            continue
        reason = str((event.data or {}).get("reason") or "").strip().lower()
        if reason in _IGNORED_FAILURE_REASONS:
            continue
        current_failures[(nid, current.attempt)] = event
    failures = sorted(current_failures.items(), key=lambda pair: pair[1].seq)
    anchor = accepted_event(state.failure_spike_seq, EV_NODE_FAILED)
    if state.current_failure_count >= 3 and anchor is not None and failures:
        # Route to a lifecycle that is still failed even if the original threshold-crossing node was
        # later reset/aborted. The opaque id stays tied to the real upward crossing, so shrinking and
        # regrouping the current set never fabricates a fresh browser alert.
        (nid, attempt), _current_anchor = failures[-1]
        item = _item(
            run_id, generation, anchor, "failure_spike", severity="warning",
            title="Experiment failures need attention",
            detail=f"{state.current_failure_count} current experiment failures; inspect the failure panel.",
            browser=True, active=True, node_id=nid, node_generation=attempt,
        )
        if item:
            items.append(item)

    # Developer-crash auto-pause is a system failure; an explicit operator pause has no node owner
    # and is intentionally quiet. The folded generation check prevents an old pause from alerting
    # after that node was reset or aborted.
    pause_node_id = _integer(state.pause_node_id)
    pause_generation = _integer(state.pause_generation)
    if state.paused and pause_node_id is not None and pause_generation is not None:
        pause = accepted_event(state.pause_event_seq, EV_PAUSE)
        if pause is not None:
            item = _item(
                run_id, generation, pause, "run_failed", severity="danger",
                title="Run paused after a developer failure",
                detail=f"Experiment #{pause_node_id} needs recovery before the run can continue.",
                browser=True, active=True, node_id=pause_node_id,
                node_generation=pause_generation,
            )
            if item:
                items.append(item)

    finish = next((event for event in reversed(rows)
                   if event.type == EV_RUN_FINISHED and event.seq == state.last_finish_seq), None)
    if finish is not None and state.finished:
        reason = str((finish.data or {}).get("reason") or "").strip().lower()
        modern = bool((finish.data or {}).get("finalization_required", False))
        marker = None
        if modern and state.finalized_finish_seq == state.last_finish_seq:
            marker = accepted_event(state.finalization_marker_seq, EV_FINALIZATION_FINISHED)
        completed_event = marker if modern else finish

        if reason in _FAILED_FINISH_REASONS or reason.startswith("stuck:"):
            item = _item(
                run_id, generation, finish, "run_failed", severity="danger",
                title=("Run ended with an error" if reason == "error"
                       else "Run ended without a publishable result"),
                detail="Open the run to inspect the terminal failure and recovery options.",
                browser=True, active=True,
            )
            if item:
                items.append(item)
        elif completed_event is not None and reason in _BUDGET_REASONS:
            item = _item(
                run_id, generation, completed_event, "budget_exhausted", severity="warning",
                title="Run budget reached", detail=_BUDGET_REASONS[reason], browser=True,
            )
            if item:
                items.append(item)
        elif completed_event is not None and reason in _STOPPED_FINISH_REASONS:
            item = _item(
                run_id, generation, completed_event, "stopped", severity="warning",
                title="Run finalized by operator",
                detail="The run was intentionally stopped and its durable wrap-up is ready.",
                browser=False,
            )
            if item:
                items.append(item)
        elif completed_event is not None:
            item = _item(
                run_id, generation, completed_event, "finished", severity="success",
                title="Run finished", detail="The final report and durable wrap-up are ready.",
                browser=True,
            )
            if item:
                items.append(item)

    tail = rows[-1] if rows else Event(type="empty")
    runtime = {
        "finished": bool(state.finished),
        "paused": bool(state.paused),
        "awaiting_approval": bool(state.awaiting_approval),
        "spec_approval": bool(state.spec_approval_requested and not state.spec_confirmed),
        "resume_pending": bool(state.resume_pending()),
        "finalization_pending": bool(
            incomplete_finalize_scope(rows) is not None or state.finalization_pending()),
    }
    return {
        "generation": generation,
        "task_id": _run_id(state.task_id),
        "items": items,
        "tail_seq": _integer(tail.seq) if rows else -1,
        "tail_ts": _timestamp(tail.ts) if rows else 0.0,
        "runtime": runtime,
    }


def visible_event_attention(projection: dict, *, engine_running: bool | None) -> list[dict]:
    """A terminal-success marker is visible only after the driver releases its engine lock."""
    terminal = {"finished", "budget_exhausted", "stopped"}
    return [item for item in projection.get("items", [])
            if item.get("kind") not in terminal or engine_running is False]


def project_runtime_attention(run_id: str, projection: dict, *, engine_running: bool | None,
                              now: float | None = None) -> list[dict]:
    """Derived in-app-only liveness attention.  No OS notification claims a non-domain event."""
    if engine_running is not False or not projection.get("generation"):
        return []
    flags = projection.get("runtime") or {}
    if flags.get("finished") and not flags.get("finalization_pending"):
        return []
    if flags.get("resume_pending"):
        return []
    if (not flags.get("finished") and (flags.get("paused") or flags.get("awaiting_approval")
                                      or flags.get("spec_approval"))):
        return []
    # Engine-lock release can briefly lead the finalization marker (and an ordinary run can be in a
    # launch hand-off). Apply the same grace to both derived warnings so polling never flashes a
    # false recovery demand during a healthy transition.
    current_time = _timestamp(now) if now is not None else time.time()
    if current_time - float(projection.get("tail_ts") or 0) < 15.0:
        return []
    tail = Event(seq=projection.get("tail_seq", -1), ts=projection.get("tail_ts", 0.0), type="derived")
    finalizing = bool(flags.get("finalization_pending"))
    item = _item(
        run_id, projection["generation"], tail,
        "finalization_stalled" if finalizing else "stalled",
        severity="danger",
        title="Finalization needs recovery" if finalizing else "Run engine stopped",
        detail=("The run stopped before durable wrap-up completed."
                if finalizing else "No engine process is advancing this run."),
        browser=False, active=True, derived=True,
    )
    return [item] if item else []


def project_run_attention(run_id: str, events: Iterable[Event], *,
                          engine_running: bool | None = None,
                          now: float | None = None) -> list[dict]:
    projection = project_event_attention(run_id, events)
    return visible_event_attention(projection, engine_running=engine_running) + project_runtime_attention(
        run_id, projection, engine_running=engine_running, now=now)
