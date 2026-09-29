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

The same engine-installed shape carries the run's STOP the other way (`run_halt_scope` /
`run_halted`): a probe a role wrapper reads before an optional paid step of its own.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

PHASE_STARTED = "agent_phase_started"
PHASE_CHECKPOINTED = "agent_checkpointed"
PHASE_COMPLETED = "agent_phase_completed"
PHASE_EVENT_TYPES = (PHASE_STARTED, PHASE_CHECKPOINTED, PHASE_COMPLETED)
# The memory-read RECORD (doc 52 row 17) rides the same sink: a tool provider runs wherever the
# loop runs and has no store, so the engine-installed sink is the one durable channel it has.
MEMORY_READ = "memory_read"
SINK_EVENT_TYPES = PHASE_EVENT_TYPES + (MEMORY_READ,)
MEMORY_READ_ROWS = 16

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
    if etype not in SINK_EVENT_TYPES:
        raise ValueError(f"not a phase event type: {etype!r}")
    sink = _SINK.get()
    if sink is None:
        return
    try:
        sink(etype, dict(data))
    except Exception as exc:  # noqa: BLE001 — a broken observer must not become a broken agent
        from looplab.core.containment import contain
        contain("phase event sink", exc)


def emit_memory_read(tool: str, args, result, *, rows=(), source: Optional[dict] = None) -> Optional[str]:
    """Report one cross-run / memory / skill tool call as a `memory_read` record and return its
    invocation id (None outside a run, where nothing is written). The record carries the exact
    rendered result's sha256 and length — never the text, which the trace already holds — plus the
    ids of the rows it showed, so a later decision can be joined to the bytes that preceded it."""
    if _SINK.get() is None:
        return None
    import hashlib
    import secrets
    text = str(result or "")
    bounded_args: dict = {}
    if isinstance(args, dict):
        for key, value in list(args.items())[:8]:
            bounded_args[str(key)[:64]] = (value if isinstance(value, (int, float, bool))
                                           and not isinstance(value, bool) or isinstance(value, bool)
                                           else str(value)[:200])
    invocation_id = "inv-" + secrets.token_hex(12)
    data = {
        "tool": str(tool or "")[:64],
        "invocation_id": invocation_id,
        "args": bounded_args,
        "result_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "result_chars": len(text),
        "rows": [{"id": str(r.get("id"))[:64], "statement": str(r.get("statement") or "")[:160]}
                 for r in list(rows or ())[:MEMORY_READ_ROWS] if isinstance(r, dict) and r.get("id")],
    }
    if isinstance(source, dict):
        data["source"] = {str(k)[:32]: v for k, v in list(source.items())[:8]
                          if isinstance(v, (str, int, float, bool)) or v is None}
    emit_phase_event(MEMORY_READ, data)
    return invocation_id


# THE RUN'S STOP, AS A ROLE WRAPPER READS IT (WP-STOP, 2026-09-29). The engine asks
# `RunState.halted` before every paid call IT starts, but a Researcher stack makes several paid calls
# inside ONE `propose` — `search/foresight.py`'s panel runs K member proposals, a ranking and a
# verifier — and nothing below the engine could see a stop. On MiniOneRec inf13 the pause landed
# while member 2 was proposing and the panel went on to rank (a new paid call) after it. The engine
# publishes its own fresh-fold probe here for the run's lifetime, beside the phase sink and for the
# same reason: this is the one channel a role has into the run it works for. A wrapper asks
# `run_halted()` before an OPTIONAL paid step of its own and, once it answers True, starts none —
# the call already in flight finishes and the engine's fences refuse its result. Outside a run (the
# assistant, a unit test, a script) nothing is published and the answer is always False.
_HALT_PROBE: contextvars.ContextVar[Optional[Callable[[], bool]]] = contextvars.ContextVar(
    "looplab_run_halt_probe", default=None)


@contextmanager
def run_halt_scope(probe: Optional[Callable[[], bool]]) -> Iterator[None]:
    """Publish `probe` — "has the run halted?" — for everything this context runs, worker threads
    included (anyio copies the context into `to_thread`). A non-callable publishes nothing."""
    if not callable(probe):
        yield
        return
    token = _HALT_PROBE.set(probe)
    try:
        yield
    finally:
        _HALT_PROBE.reset(token)


def run_halted() -> bool:
    """Has the run this code works for halted (paused, finished, or a stop requested)? False outside
    a run. A GUARDED probe, like `agents/tool_loop.py::phase_cancelled`: a probe that cannot READ the
    log — an I/O error, a row it cannot parse, a log its store calls corrupt (`OSError`, `ValueError`,
    `RuntimeError`) — answers False, because a broken observer must never be able to end the paid
    work it observes, and the engine's own next fold meets the same log. Deliberately not a blind
    handler: any other exception from a fold is a bug, and a bug surfaces."""
    probe = _HALT_PROBE.get()
    if probe is None:
        return False
    try:
        return bool(probe())
    except (OSError, ValueError, RuntimeError):
        return False
