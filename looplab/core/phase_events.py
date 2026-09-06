"""Inner agent phases as durable DIAGNOSTIC events (doc 52 row 16; doc 27's marker).

A tool loop's trajectory — when a phase started, what plan the agent recorded for itself, how the
phase ended — lived only in `spans.jsonl`, which replay never reads and a cleared trace loses.
`drive_tool_loop` now reports the three moments through this module, and the ENGINE decides
whether anything is written: it installs a sink for the run's lifetime (`phase_sink_scope`), and
outside a run (the assistant, a unit test, a script) `emit_phase_event` is a no-op.

WHY A SINK AND NOT A STORE. The loop runs on the main task, on a build fan-out worker thread and
on the concurrent research task, and engine invariant #1 says who may write what from where. The
three types are `DIAGNOSTIC_EVENTS`: fold-ignored, excluded wholesale from every seq-equality
fence (`_proposal_authority_seq`), and serialized by `EventStore.append`'s own lock — so the one
thing a durable row can disturb, a main-task decision keyed on a row's position, cannot see them.
The engine's sink asserts that membership at the append site and redacts every string through the
run's own tail redactor, because a recorded plan is model-authored text on its way to disk.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

PHASE_STARTED = "agent_phase_started"
PHASE_CHECKPOINTED = "agent_checkpointed"
PHASE_COMPLETED = "agent_phase_completed"
PHASE_EVENT_TYPES = (PHASE_STARTED, PHASE_CHECKPOINTED, PHASE_COMPLETED)

_SINK: contextvars.ContextVar[Optional[Callable[[str, dict], None]]] = contextvars.ContextVar(
    "looplab_phase_event_sink", default=None)


@contextmanager
def phase_sink_scope(sink: Optional[Callable[[str, dict], None]]) -> Iterator[None]:
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def current_phase_sink() -> Optional[Callable[[str, dict], None]]:
    return _SINK.get()


def emit_phase_event(etype: str, data: dict) -> None:
    """Hand one phase moment to the installed sink; never raises into the loop that reports it."""
    if etype not in PHASE_EVENT_TYPES:
        raise ValueError(f"not a phase event type: {etype!r}")
    sink = _SINK.get()
    if sink is None:
        return
    try:
        sink(etype, dict(data))
    except Exception as exc:  # noqa: BLE001 — a broken observer must not become a broken agent
        from looplab.core.containment import contain
        contain("phase event sink", exc)
