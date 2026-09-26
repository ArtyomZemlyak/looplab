"""Named wire protocols of the UI control plane — the implicit string contracts shared by the
FastAPI server (`serve/server.py`), the terminal client (`serve/tui.py::Api`) and the React UI
(`ui/src/util.js`). The server is the writer; the two clients string-match these values, so a
rename here is a BREAKING protocol change for both (the React side keeps its own literals — grep
`ui/src` before changing anything).

Protocols named here:

* Run command generations — ``GET /api/runs/{id}/state`` exposes RUN_GENERATION_FIELD and every
  brand-new durable command echoes it as EXPECTED_RUN_GENERATION_FIELD. This binds delayed first
  submissions to the event log the operator actually reviewed; idempotent replay of an existing
  record remains observable after an in-place reset.

* Background jobs — a slow endpoint (genesis boss, action-router, report regen) returns
  ``{"status": JOB_RUNNING, "job_id": ...}`` when the work outlasts its inline wait; clients poll
  ``GET /api/jobs/{id}`` (or ``/api/genesis/{id}``) which answers ``{"status": JOB_RUNNING, ...}``
  until done, then the full result dict with ``status=JOB_DONE``, or ``{"status": JOB_UNKNOWN}``
  once the process receipt expired/was evicted. Generic terminal receipts remain replayable for the
  ten-minute polling window. A paid report has its own durable event receipt, so its first terminal
  poll atomically retires the volatile job receipt; a lost response or later poll must reconcile by
  replaying the same generation and Idempotency-Key, never a fresh identity. The inline-wait
  convention: a fast result is returned directly (NO ``status`` key), so clients must treat
  "status == running + job_id" as the only poll trigger (tui `_await_job`, util.js `jobAwait`).

* SSE event names — the run stream (`/api/runs/{id}/events`) emits SSE_STATE ticks and a final
  SSE_DONE only after the folded run is finished AND its engine has released the live lock (plus
  `: keepalive` comment lines clients ignore); the assistant stream
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

* Durable command records — the lifecycle words, the engine policies and the settle codes a record
  carries, and the three rules a reader OUTSIDE the server needs to say what re-driving a record
  would do (`looplab stop --wait`, which must work without FastAPI): `deadline_passed`, and the
  `engine_ack` postcondition as `command_intent_marker` + `file_command_ack` + `ack_observed`.
"""
from __future__ import annotations

import math
from enum import Enum

from looplab.events.types import (
    EV_ANNOTATION, EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_DEEP_RESEARCH,
    EV_CARD_DROPPED, EV_CARD_EDITED, EV_CARD_REOPENED, EV_CARD_REPRIORITIZED,
    EV_CARD_RESOURCE_PINNED, EV_COMMAND_ACK,
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED, EV_CONCEPT_TAG_EDITED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT, EV_HYPOTHESIS_ADDED,
    EV_HYPOTHESIS_UPDATED, EV_INJECT_NODE, EV_NODE_ABORT, EV_NODE_RESET, EV_PAUSE, EV_PROMOTE,
    EV_RESTART, EV_RESUME, EV_RUN_ABORT, EV_RUN_CONCEPTS, EV_RUN_REOPENED, EV_SET_STRATEGY,
    EV_SPEC_APPROVED)

# ---- run-generation command precondition ---------------------------------------------------------
# The read model exposes the generation currently occupying a reusable run id. A brand-new durable
# command echoes that exact token so a request formed before an in-place reset cannot mutate the
# replacement run when its first POST arrives late. Keep these names centralized: HTTP/TUI/Web/tool
# adapters all share them even though their transport mechanics differ.
RUN_GENERATION_FIELD = "generation"
EXPECTED_RUN_GENERATION_FIELD = "expected_generation"

# Control events the UI is allowed to append (intent). The engine writes the domain effect.
# FROZEN on purpose: this is the security boundary the command intake
# (`control_validation.py::normalize_control`) checks membership against and
# control_validation asserts a ControlSpec for. As a plain set, any imported module — or a test doing
# `CONTROL_EVENTS.add(...)` — could widen it process-wide and authorize a new appendable type with
# no failing assertion and no spec review. Adding a type must be an edit to THIS literal.
# THE DURABLE COMMAND RECORD'S LIFECYCLE, in the module whose docstring calls itself the home of the
# string contracts the server, the terminal client and the React UI share. These seven words were
# spelled across `run_commands`, the control router, both TUI halves, the run-control tool and
# `ui/src/commandModel.js`, with no test pinning any copy against another.
#
# `run_commands.TERMINAL_STATUSES` already existed and is kept under that name — its call sites read
# well — but it now DERIVES from here, and the two in-flight subsets that were spelled inline derive
# from here too. What a status MEANS stays at the surface that renders it; what the words ARE lives
# once.
COMMAND_ACTIVE_STATUSES = frozenset({"accepted", "executing"})
# `noop` is a SUCCESS: the command was understood and the state it asked for already held. Splitting
# it out from `succeeded` is what lets a surface say "already satisfied" rather than claiming it did
# something. `rejected` is likewise not `failed` — the record never became work.
COMMAND_SUCCEEDED_STATUSES = frozenset({"succeeded", "noop"})
COMMAND_FAILED_STATUSES = frozenset({"failed", "rejected", "timed_out"})
COMMAND_TERMINAL_STATUSES = COMMAND_SUCCEEDED_STATUSES | COMMAND_FAILED_STATUSES
COMMAND_STATUSES = COMMAND_ACTIVE_STATUSES | COMMAND_TERMINAL_STATUSES


class EnginePolicy(str, Enum):
    """What a control command asks of the ENGINE PROCESS — `serve/control_validation.py`'s
    `_CONTROL_POLICIES` assigns one per control event and the command worker acts on it. Its value is
    persisted on every durable command record as `engine_policy`, and read back outside the server
    by `looplab stop --wait` (`cli/run_cmds.py::server_commands_restarting`), which is why it lives
    here, beside the other record words, and not in the fastapi-importing module that assigns it."""
    NO_SPAWN = "no_spawn"
    ENSURE_RUNNING = "ensure_running"
    ENSURE_DRIVER_PRESERVE_STOP = "ensure_driver_preserve_stop"
    RESTART_AFTER_EXIT = "restart_after_exit"


# The `error.code` a durable command record settles with when its engine spawn crossed the boundary
# after which the server cannot tell whether a child started: the record is terminal, yet an engine
# may still be importing — so `looplab stop --wait` reads a RECENT one as a possible engine start.
# Every Python record site (`serve/run_commands.py`, `SPAWN_CLAIM_HATCH` included) spells it through
# this constant; the React client keeps its own literal (`ui/src/commandModel.js`).
ENGINE_START_UNCERTAIN = "engine_start_uncertain"

# The `error.code` a durable command record settles `timed_out` with when its `absolute_deadline_at`
# passed BEFORE its intent was recorded: the intent is not in the run's log, so nothing was appended
# and nothing was driven (critic 2026-09-26, driven: a worker that died before admission left the
# record `accepted`; forty minutes later a GET settled pause, budget_extend, approval_granted and
# resume `timed_out` with nothing appended, under `postcondition_timeout`'s "command intent was
# recorded but … was not observed in time" — a sentence that was false for every one of them).
# RETRYABLE: `/retry` re-arms the record under a fresh deadline and drives it from admission, and a
# record with no durable intent holds back no fresh submission of the same action
# (`run_commands.py::RunCommandService._unresolved_equivalent`). Written by
# `RunCommandService._settle_expired`; the React client stores it as itself
# (`ui/src/commandModel.js::STORED_ERROR_CODES`, pinned by `tests/test_command_status_vocabulary.py`).
DEADLINE_PASSED_BEFORE_INTENT = "deadline_passed_before_intent"


def deadline_passed(deadline, now: float) -> bool:
    """Has a durable command record's `absolute_deadline_at` passed at `now`? False for a value no
    server writes (absent, a bool, a non-finite number), which keeps such a record on the old path.
    HERE and not in `serve/run_commands.py` because `looplab stop --wait` asks the same question of
    the same field without FastAPI installed; the command service's admission and spawn gates
    (`RunCommandService._settle_if_expired`) are the other caller.

    `now` is REQUIRED, and this module keeps no clock: a deadline is compared on the clock of the
    module that STAMPED it. The re-drive gate once defaulted to this module's own `time.time()` while
    `run_commands` stamps its deadlines through `run_commands.time` — the clock
    `tests/test_command_monitor_cost.py::_MonitorClock` drives — so a test whose driven clock lagged
    the real one by the length of its own setup saw its record expire before the first monitor tick
    (critic 2026-09-26: that test flaked under load, and deterministically with one real second of
    sleep before `_execute`)."""
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    try:
        value = float(deadline)
    except OverflowError:
        return False
    return math.isfinite(value) and now >= value


# THE `engine_ack` POSTCONDITION, stated once for its two readers (critic 2026-09-26, driven). The
# command service asks it of its incremental log index (`serve/command_observation.py`); `looplab
# stop --wait` asks it of the log it just read, to leave out a command the engine already served —
# a GET settles that one `succeeded` and starts nothing. The CLI kept a copy that was not the rule:
# integer seqs only, and the marker `intent_marker or id or <file name>`. An ack row carrying
# `event_seq: 3.0` satisfied the server (`3 == 3.0`) and not the copy, so the wait reported an engine
# start the server would never make (exit 1); a hand-edited list `event_seq` raised `TypeError` from
# a set lookup, after the pause was already appended. Three pieces, each the server's own:
def command_intent_marker(record, command_id: str = "") -> str:
    """The `_command_id` a durable command record's intent is stamped with, and so the key its
    `command_ack` is filed under: the record's `intent_marker` when that is a non-empty string (a
    superseded intent re-issued under a fresh marker — `run_commands.py::RunCommandService.
    _intent_marker` has why the two cannot share one), else `command_id`, else the record's `id`.
    Never the record's FILE name, which the CLI's copy fell back to."""
    marker = (record or {}).get("intent_marker")
    if isinstance(marker, str) and marker:
        return marker
    return command_id or str((record or {}).get("id") or "")


def file_command_ack(acknowledgements: dict, data) -> None:
    """File one `command_ack` payload into `{marker: (event_seq, …)}` IN PLACE: keyed by
    `str(command_id or "")` and carrying `event_seq` exactly as written — never narrowed to an
    integer, because `ack_observed` compares with Python equality and old logs rely on it."""
    data = data if isinstance(data, dict) else {}
    marker = str(data.get("command_id") or "")
    acknowledgements[marker] = acknowledgements.get(marker, ()) + (data.get("event_seq"),)


def command_ack_index(events) -> dict:
    """`{marker: (event_seq, …)}` over every `command_ack` in `events` — the index
    `serve/command_observation.py` builds incrementally, built here in one pass for a reader that
    holds the whole log (`looplab stop --wait`)."""
    acknowledgements: dict = {}
    for event in events or ():
        if getattr(event, "type", None) == EV_COMMAND_ACK:
            file_command_ack(acknowledgements, getattr(event, "data", None))
    return acknowledgements


def ack_observed(acknowledgements, marker: str, event_seq) -> bool:
    """Is `(marker, event_seq)` among the filed acknowledgements? TUPLE MEMBERSHIP on purpose: it
    keeps Python's exact historical equality (`3 == 3.0`, and legacy oddities such as `True == 1`)
    instead of narrowing old logs to a new integer schema, and it never hashes `event_seq`, so an
    unhashable value on a hand-edited record answers False rather than raising."""
    return event_seq in acknowledgements.get(marker, ())


def engine_ack_observed(record, acknowledgements) -> bool:
    """Does `record`'s `engine_ack` postcondition hold against `acknowledgements`? The whole rule —
    `RunCommandService._postcondition` answers its `engine_ack` kind through this, over the
    observation's index (`CommandObservation.engine_ack_observed`), and `looplab stop --wait` over
    `command_ack_index` of the log it read."""
    record = record or {}
    return ack_observed(acknowledgements,
                        command_intent_marker(record, str(record.get("id") or "")),
                        record.get("event_seq"))


CONTROL_EVENTS = frozenset({
    EV_RUN_ABORT, EV_PAUSE, EV_RESTART, EV_RESUME, EV_NODE_ABORT, EV_NODE_RESET, EV_BUDGET_EXTEND, EV_HINT,
    EV_FORCE_CONFIRM, EV_FORCE_ABLATE, EV_FORK, EV_ANNOTATION, EV_PROMOTE,
    EV_APPROVAL_GRANTED, EV_SPEC_APPROVED, EV_INJECT_NODE, EV_RUN_REOPENED,
    EV_SET_STRATEGY,   # A7: operator pins/overrides the Strategist's choice (HITL parity)
    EV_DEEP_RESEARCH,  # P2: operator asks the engine to run the Deep-Research stage now
    EV_HYPOTHESIS_ADDED,    # P1: a human registers a hypothesis on the board (open question to test)
    EV_HYPOTHESIS_UPDATED,  # P1: a human abandons a hypothesis line (status=abandoned)
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED,
    EV_CONCEPT_TAG_EDITED,  # PART V Phase 2b: an operator re-tags one node's concepts (command-only)
    EV_RUN_CONCEPTS,  # PART V (D): operator/assistant sets the run's BASE concept set (last-write-wins)
    # Layer 6 Card board controls (docs/23 §12.6 stage 10) are command-only and server-stamped. They
    # never wake a dead engine; live selection/scheduling observes their fold, and an exact operator
    # drop may cancel its running eval. New engine lifecycle drops use `card_auto_dropped`; only legacy
    # logs can still carry an engine-authored `card_dropped` compatibility row.
    EV_CARD_REPRIORITIZED, EV_CARD_EDITED, EV_CARD_RESOURCE_PINNED, EV_CARD_DROPPED,
    # The drop's counterpart: an operator putting a stopped card back on the board. Command-only
    # and server-stamped like every other card control, and folded LAST-RECEIPT-WINS against the
    # drop by event index, so drop/reopen/drop is expressible and replays identically.
    EV_CARD_REOPENED,
})

# Versioned collaboration takes the strict-CAS append (the retired compatibility /control route
# refused these outright): the durable command protocol requires an idempotency key plus the exact
# run generation the operator observed.
COLLABORATION_EVENTS = frozenset({
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED,
    EV_CONCEPT_TAG_EDITED,
    EV_CARD_REPRIORITIZED, EV_CARD_EDITED, EV_CARD_RESOURCE_PINNED, EV_CARD_DROPPED,
    EV_CARD_REOPENED,
    # PART V (D): a base-concept edit is command-only too — force it through the generation-fenced command
    # endpoint so a write formed against an old generation can't land on a
    # post-reset replacement run, exactly like its per-node sibling EV_CONCEPT_TAG_EDITED.
    EV_RUN_CONCEPTS,
})

POLL_SECONDS = 0.4   # SSE tail cadence — fast enough to feel live, light on the disk

# ---- background-job statuses (generic `_jobs` registry + the genesis job twin) -----------------
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_UNKNOWN = "unknown"

# ---- SSE event names ----------------------------------------------------------------------------
# Run stream (/api/runs/{id}/events): a state tick per change, then done once terminal-ready
# (run_finished is folded and the engine has released its singleton lock).
SSE_STATE = "state"
# A DELTA against the previous frame on the SAME connection (doc 52 row 29): `{version, base_seq,
# seq, generation, event_count, ops}` — `events/state_delta.py` writes the ops, `ui/src/stateDelta.js`
# applies them, and a client whose held seq is not `base_seq` reconnects for a full `state` frame.
SSE_STATE_DELTA = "state_delta"
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
PHASE_FINALIZING = "finalizing"
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
