"""Named wire protocols of the UI control plane — the implicit string contracts shared by the
FastAPI server (`serve/server.py`), the terminal client (`serve/tui.py::Api`) and the React UI
(`ui/src/util.js`). The server is the writer; the two clients string-match these values, so a
rename here is a BREAKING protocol change for both (the React side keeps its own literals — grep
`ui/src` before changing anything).

Protocols named here:

* Background jobs — a slow endpoint (genesis boss, action-router, report regen) returns
  ``{"status": JOB_RUNNING, "job_id": ...}`` when the work outlasts its inline wait; clients poll
  ``GET /api/jobs/{id}`` (or ``/api/genesis/{id}``) which answers ``{"status": JOB_RUNNING, ...}``
  until done, then the full result dict with ``status=JOB_DONE``, or ``{"status": JOB_UNKNOWN}``
  once the job expired/was evicted (the client should re-issue the action). The inline-wait
  convention: a fast result is returned directly (NO ``status`` key), so clients must treat
  "status == running + job_id" as the only poll trigger (tui `_await_job`, util.js `jobAwait`).

* SSE event names — the run stream (`/api/runs/{id}/events`) emits SSE_STATE ticks and a final
  SSE_DONE (plus `: keepalive` comment lines clients ignore); the assistant stream
  (`.../message_stream`) emits SSE_TOKEN / SSE_STEP / SSE_TODOS / SSE_TEXT / SSE_ERROR and a
  final SSE_DONE. ASSISTANT_STREAM_END_SENTINEL is server-INTERNAL: the worker thread's
  end-of-queue marker, never sent on the wire.

* Permission decisions — the human's verdict on a mutating assistant tool
  (``POST /api/assistant/permissions/{id}``): PERM_ALLOW_ONCE / PERM_ALLOW_ALWAYS / PERM_DENY.
  `tools/write_tools.py` receives these via the injected approver and string-matches them
  (tools must not import serve, so it keeps its own literals — see its `_authorize`).

* Phase names — the coarse run lifecycle `server._phase` derives from folded state, rendered by
  the UI/TUI status badges (tui `_PHASE_META`). "running" is NOT a phase: clients infer it from
  ``engine_running`` on a non-finished run.
"""
from __future__ import annotations

from looplab.events.types import (
    EV_ANNOTATION, EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_DEEP_RESEARCH,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT, EV_HYPOTHESIS_ADDED,
    EV_HYPOTHESIS_UPDATED, EV_INJECT_NODE, EV_NODE_ABORT, EV_PAUSE, EV_PROMOTE,
    EV_RESUME, EV_RUN_ABORT, EV_RUN_REOPENED, EV_SET_STRATEGY, EV_SPEC_APPROVED)

# Control events the UI is allowed to append (intent). The engine writes the domain effect.
CONTROL_EVENTS = {
    EV_RUN_ABORT, EV_PAUSE, EV_RESUME, EV_NODE_ABORT, EV_BUDGET_EXTEND, EV_HINT,
    EV_FORCE_CONFIRM, EV_FORCE_ABLATE, EV_FORK, EV_ANNOTATION, EV_PROMOTE,
    EV_APPROVAL_GRANTED, EV_SPEC_APPROVED, EV_INJECT_NODE, EV_RUN_REOPENED,
    EV_SET_STRATEGY,   # A7: operator pins/overrides the Strategist's choice (HITL parity)
    EV_DEEP_RESEARCH,  # P2: operator asks the engine to run the Deep-Research stage now
    EV_HYPOTHESIS_ADDED,    # P1: a human registers a hypothesis on the board (open question to test)
    EV_HYPOTHESIS_UPDATED,  # P1: a human abandons a hypothesis line (status=abandoned)
}

POLL_SECONDS = 0.4   # SSE tail cadence — fast enough to feel live, light on the disk

# ---- background-job statuses (generic `_jobs` registry + the genesis job twin) -----------------
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_UNKNOWN = "unknown"

# ---- SSE event names ----------------------------------------------------------------------------
# Run stream (/api/runs/{id}/events): a state tick per change, then a terminal done.
SSE_STATE = "state"
SSE_DONE = "done"      # also ends the assistant stream (carrying the full result dict)
# Assistant stream (.../message_stream): live turn progress.
SSE_TOKEN = "token"    # final-answer token pieces
SSE_STEP = "step"      # one-line tool-step label ("reading README.md…")
SSE_TODOS = "todos"    # the turn's live todo list
SSE_TEXT = "text"      # interstitial assistant prose (between tool rounds)
SSE_ERROR = "error"    # turn failed; data is the error string
# Internal end-of-queue marker between the assistant worker thread and its SSE generator —
# never emitted on the wire (the generator breaks instead of yielding it).
ASSISTANT_STREAM_END_SENTINEL = "__end__"

# ---- permission decisions (assistant HITL confirm) -----------------------------------------------
PERM_ALLOW_ONCE = "allow_once"
PERM_ALLOW_ALWAYS = "allow_always"   # remembers the tool kind for the session so it stops asking
PERM_DENY = "deny"

# ---- run phase names (server._phase) --------------------------------------------------------------
PHASE_FINISHED = "finished"
PHASE_PAUSED = "paused"
PHASE_APPROVAL = "approval"
PHASE_SPEC_APPROVAL = "spec_approval"
PHASE_ONBOARDING = "onboarding"
PHASE_GROUNDING = "grounding"
PHASE_SEARCH = "search"

# Genesis chat turns seeded into a new run's chat.jsonl get seq = GENESIS_CHAT_SEQ_BASE + i: a
# huge, far-beyond-any-event seq in the "chat range", which is the UI Dock's rendering contract —
# it renders chat-range seqs as CONVERSATION turns (not engine events) while their creation-time
# timestamps (< the engine's run_started ts) still sort the planning chat at the TOP of the feed.
GENESIS_CHAT_SEQ_BASE = int(1e15)
