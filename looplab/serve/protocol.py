"""Named wire protocols of the UI control plane — the implicit string contracts shared by the
FastAPI server (`serve/server.py`), the terminal client (`serve/tui.py::Api`) and the React UI
(`ui/src/util.js`). The server is the writer; the two clients string-match these values, so a
rename here is a BREAKING protocol change for both (the React side keeps its own literals — grep
`ui/src` before changing anything).

Protocols named here:

* Command request/server-derived fields — the canonical tables used by server validation
  and remote MCP phase discovery, without importing the optional FastAPI server stack.

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
  carries, and the rules a reader OUTSIDE the server needs to say what re-driving a record would do
  (`looplab stop --wait`, which must work without FastAPI): `deadline_passed`, the `engine_ack`
  postcondition as `command_intent_marker` + `file_command_ack` + `ack_observed`, and
  `waits_for_resume` — a queued intent on a stopped run starts nothing (doc 69 69.30).
"""
from __future__ import annotations

import math
from enum import Enum
from typing import Optional

from looplab.events.types import (
    PAUSE_REASON_EXTERNAL_OBLIGATIONS,
    EV_ANNOTATION, EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_DEEP_RESEARCH,
    EV_CARD_DROPPED, EV_CARD_EDITED, EV_CARD_FILED, EV_CARD_REOPENED, EV_CARD_REPRIORITIZED,
    EV_CARD_RESOURCE_PINNED, EV_COMMAND_ACK,
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED, EV_CONCEPT_TAG_EDITED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT, EV_HYPOTHESIS_ADDED,
    EV_HYPOTHESIS_UPDATED, EV_INJECT_NODE, EV_METRIC_RETARGET, EV_NODE_ABORT, EV_NODE_RESET,
    EV_PAUSE, EV_PROMOTE, EV_RESTART, EV_RESUME, EV_RUN_ABORT, EV_RUN_CONCEPTS, EV_RUN_FINISHED,
    EV_RUN_REOPENED, EV_SET_STRATEGY, EV_SPEC_APPROVED, EV_RESEARCH_COMPLETED, EV_REPORT_GENERATED,
    EV_TRACK_REQUESTED)

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
COMMAND_RECEIPT_ERROR_CAP = 256  # sanitized command-receipt diagnostics, shared server/MCP boundary


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


def file_drain_ack(drain_acknowledgements: dict, data) -> None:
    """File one `command_ack` payload into `{marker: (event_seq, …)}` IN PLACE when a DRAIN engine
    wrote it (`drain_only: true`, doc 68 68.3b) — the same keys and values `file_command_ack` files,
    kept apart so a command that asked for a drain can tell its own engine from a search."""
    if isinstance(data, dict) and data.get("drain_only") is True:
        file_command_ack(drain_acknowledgements, data)


def file_deferred_ack(deferred_acknowledgements: dict, data) -> None:
    """File one `command_ack` payload into `{marker: (event_seq, …)}` IN PLACE when a drain acked
    the intent WITHOUT serving it (`deferred: true`, doc 68 68.3b) — the command settles
    `deferred_to_next_search` rather than reading as applied."""
    if isinstance(data, dict) and data.get("deferred") is True:
        file_command_ack(deferred_acknowledgements, data)


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


# THE STOP A QUEUED INTENT LEAVES STANDING (doc 69 69.30). A fork, an inject, a forced confirm or
# ablation, a deep-research request and a strategy pin are each SERVED BY THE SEARCH — a loop turn
# takes them off their durable queue — so none needs an engine start of its own. Sent to a PAUSED
# run with no engine, the command started `looplab resume` for it anyway, and that start LIFTED the
# stop (the CLI's resume appends `resume` to a paused run): on MiniOneRec v10 the first of eight
# injects an operator queued on a stopped run started the search, and the hint sent with them landed
# after the Strategist's first decision. Such a command now records its intent and settles
# `succeeded` with `deferred_until_resume`, starting nothing: the queue waits for the operator's own
# resume (or restart), which serves all of it at once.
#
# A BUDGET EXTENSION and the two APPROVALS (a champion's, an eval spec's) wait too (critic
# 2026-09-29): each is a folded fact the next loop turn reads, and starting `looplab resume` for
# one lifted the stop exactly as the inject did — a batch holding a budget extension re-ran the
# incident.
# "How a run stopped by its budget goes on" is a FINISHED run (not a stop: `stop_holds_queued_
# intents`), and a gate waiting for approval is an exit, not a pause; the one PAUSED budget stop is
# an external run's obligations pause, which a budget extension still lifts (`waits_for_resume`).
#
# NOT HERE, each on purpose: a reset (a plain one rescores inside a resumed search — its twin that
# pauses again is the drain, doc 68 68.3b), and a resume, reopen or restart (they ARE the
# operator's resume). Read by the command service (`serve/run_commands.py::RunCommandService.
# _left_for_the_operators_resume`) and by `looplab stop --wait` (`cli/run_cmds.py::
# server_commands_restarting`), which must not count such a command as an engine start.
QUEUED_WHILE_STOPPED: frozenset[str] = frozenset({
    EV_FORK, EV_INJECT_NODE, EV_FORCE_CONFIRM, EV_FORCE_ABLATE, EV_DEEP_RESEARCH, EV_SET_STRATEGY,
    EV_BUDGET_EXTEND, EV_APPROVAL_GRANTED, EV_SPEC_APPROVED})


def stop_holds_queued_intents(state) -> bool:
    """Does the folded run `state` sit on a stop only the operator's resume lifts? Paused — by the
    operator, a drain or the engine itself — and none of: finished (a finished run is not paused
    away; an inject there reopens it as it always did), stopping (a pending finalize wraps the run
    up, and its command refuses engine-driving work meanwhile) or already asked to resume (a
    restart's replacement owner, or a pending resume request, lifts the pause and serves the queue —
    such a command waits for that engine's acknowledgement)."""
    return bool(state.paused and not state.finished and not state.stop_requested
                and not state.resume_pending())


# The rows that move a run between PAUSED and not: the latest of them says which side of a pause the
# log is on (`command_observation.py` indexes the same set; `next_standing_pause` folds it).
PAUSE_BOUNDARY_EVENTS = frozenset({EV_PAUSE, EV_RESUME, EV_RUN_REOPENED, EV_RESTART, EV_RUN_FINISHED})


def next_standing_pause(standing: Optional[str], event) -> Optional[str]:
    """Fold one row into `standing`: None while no pause stands, else the reason that HOLDS the
    stop — `""` for a pause that names none. A `resume`, `run_reopened`, `restart` or `run_finished`
    ends the pauses that stood. An external run's obligations pause
    (`PAUSE_REASON_EXTERNAL_OBLIGATIONS`) holds it only while every pause standing is one: the first
    OTHER pause decides, whichever order the two landed in. The engine writes its obligations pause
    by compare-and-swap on the log it just read, and that read can already hold the operator's stop,
    so "the latest pause" let the engine's row stand in front of the stop (critic 2026-09-29)."""
    kind = getattr(event, "type", None)
    if kind not in PAUSE_BOUNDARY_EVENTS:
        return standing
    if kind != EV_PAUSE:
        return None
    reason = (getattr(event, "data", None) or {}).get("reason")
    reason = reason if isinstance(reason, str) else ""
    if standing is None or standing == PAUSE_REASON_EXTERNAL_OBLIGATIONS:
        return reason
    return standing


def standing_pause_reason(events) -> Optional[str]:
    """`next_standing_pause` over `events` — the pause reason the exemption in `waits_for_resume`
    asks about. Not `RunState.pause_reason`, which `replay.py::_on_pause` sets only when the paused
    node/lifecycle changes, so an operator's plain stop on top of an external run's obligations
    pause kept the obligations reason, and a budget extension then lifted the operator's stop
    (critic 2026-09-29, driven)."""
    standing = None
    for event in events or ():
        standing = next_standing_pause(standing, event)
    return standing


def waits_for_resume(record, stop_holds: bool, *, own_launch_over: bool = False,
                     pause_reason: Optional[str] = None) -> bool:
    """Does re-driving `record` start NOTHING because its intent waits in its queue for the
    operator's resume (`QUEUED_WHILE_STOPPED`)? `stop_holds` is `stop_holds_queued_intents` of the
    run as it stands and `pause_reason` the reason that holds it (`standing_pause_reason`); only an
    `engine_ack` command waits — its acknowledgement is what the resumed search writes — and a
    budget extension does not wait on an external run's obligations pause
    (`PAUSE_REASON_EXTERNAL_OBLIGATIONS`), the one paused budget stop, which it is how the run goes
    on — while that pause stands alone: a stop the operator laid beside it stands.

    Not a record whose OWN `looplab resume` child may still be starting (`spawned_by_command`
    without `spawn_claim_released`): that child is on its way and serves the intent, and settling
    the record would drop the lease that keeps a second child from launching beside it — unless the
    caller KNOWS that launch is over (`own_launch_over`: the server, once the lease has expired or
    its child is definitely gone; `looplab stop --wait` cannot tell, so it counts the child)."""
    record = record or {}
    launched = bool(record.get("spawned_by_command") and not record.get("spawn_claim_released"))
    if (record.get("event_type") == EV_BUDGET_EXTEND
            and pause_reason == PAUSE_REASON_EXTERNAL_OBLIGATIONS):
        return False
    return bool(stop_holds and (own_launch_over or not launched)
                and record.get("postcondition") == "engine_ack"
                and record.get("event_type") in QUEUED_WHILE_STOPPED)


CONTROL_EVENTS = frozenset({
    EV_RUN_ABORT, EV_PAUSE, EV_RESTART, EV_RESUME, EV_NODE_ABORT, EV_NODE_RESET, EV_BUDGET_EXTEND, EV_HINT,
    EV_FORCE_CONFIRM, EV_FORCE_ABLATE, EV_FORK, EV_ANNOTATION, EV_PROMOTE,
    EV_APPROVAL_GRANTED, EV_SPEC_APPROVED, EV_INJECT_NODE, EV_RUN_REOPENED,
    EV_SET_STRATEGY,   # A7: operator pins/overrides the Strategist's choice (HITL parity)
    EV_METRIC_RETARGET,  # doc 68 68.2: operator makes a DECLARED extra metric the objective
    EV_TRACK_REQUESTED,  # doc 73 §1.4: run a declared eval.tracks evaluation on a LIVE run
    EV_DEEP_RESEARCH,  # P2: operator asks the engine to run the Deep-Research stage now
    # The same projections as the built-in research/report writers. The command intake binds
    # provenance and sanitizes content; an external agent never appends an event directly.
    EV_RESEARCH_COMPLETED, EV_REPORT_GENERATED,
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
    # The operator files one experiment under a research question (or un-files it): the correction
    # path for `Card.parent_card_id`, which every operator-injected card arrived without.
    EV_CARD_FILED,
})

# Request-field metadata is also read by remote MCP clients without the UI extra.
# The validator imports these SAME tables; normalization and authority stay there.
# HTTP control payloads are strict contracts, not arbitrary event bags. Unknown keys are dangerous:
# replay ignores many of them, so a caller could persist `{secret: ...}` and receive false success.
CONTROL_DATA_FIELDS: dict[str, frozenset[str]] = {
    EV_RUN_ABORT: frozenset({"reason"}),
    EV_PAUSE: frozenset(),
    EV_RESTART: frozenset(),
    EV_RESUME: frozenset(),
    EV_RUN_REOPENED: frozenset(),
    EV_NODE_ABORT: frozenset({"node_id", "generation", "reason"}),
    EV_NODE_RESET: frozenset({"node_id", "generation", "from_stage"}),
    EV_BUDGET_EXTEND: frozenset(
        {"add_nodes", "max_seconds", "max_eval_seconds", "timeout", "eval_timeout",
         "eval_parallel", "llm_parallel", "max_parallel", "parallel_build"}),
    EV_HINT: frozenset({"text", "replace"}),
    EV_SET_STRATEGY: frozenset({"strategy"}),
    EV_METRIC_RETARGET: frozenset({"key", "direction", "goal"}),
    EV_TRACK_REQUESTED: frozenset({"track", "node_ids"}),
    EV_FORCE_CONFIRM: frozenset({"node_id", "generation"}),
    EV_FORCE_ABLATE: frozenset({"node_id", "generation"}),
    EV_FORK: frozenset({"from_node_id", "generation"}),
    EV_INJECT_NODE: frozenset({
        "idea", "parent_id", "parent_ids", "parent_generations", "code", "files", "deleted", "origin",
        # The operator's fork-from-a-snapshot receipt (`_normalize_fork_receipt`): which node this
        # idea was branched FROM, at which lifecycle generation, from which observed seq — plus the
        # two SERVER-STAMPED fields that make "what the operator changed" checkable.
        "forked_from",
        # An ARTIFACT node (doc 73 §1.4): `node_kind: "artifact"` succeeds on a clean pipeline with no
        # metric and is never ranked; `uses` names produced artifact nodes whose workdirs this node's
        # eval reads (`LOOPLAB_USES_WORKDIRS`).
        "node_kind", "uses",
        "source_run", "source_node"}),
    EV_DEEP_RESEARCH: frozenset(),
    EV_RESEARCH_COMPLETED: frozenset({"memo"}),
    EV_REPORT_GENERATED: frozenset({"content"}),
    EV_APPROVAL_GRANTED: frozenset({"node_id", "generation"}),
    EV_SPEC_APPROVED: frozenset(),
    EV_ANNOTATION: frozenset({"node_id", "text"}),
    EV_COMMENT_CREATED: frozenset({"node_id", "node_generation", "text"}),
    EV_COMMENT_EDITED: frozenset(
        {"comment_id", "node_id", "node_generation", "expected_version", "text"}),
    EV_COMMENT_RESOLUTION_CHANGED: frozenset(
        {"comment_id", "node_id", "node_generation", "expected_version", "resolved"}),
    EV_CONCEPT_TAG_EDITED: frozenset({"node_id", "node_generation", "concepts"}),
    EV_RUN_CONCEPTS: frozenset({"concepts"}),
    EV_PROMOTE: frozenset({"node_id", "generation", "alias"}),
    EV_HYPOTHESIS_ADDED: frozenset({"id", "statement", "source"}),
    EV_HYPOTHESIS_UPDATED: frozenset({"id", "status"}),
    # Provenance is deliberately absent: normalize_control stamps operator authority after validating
    # the exact current Card and rejects attempts to forge source/dropped_by/pinned.
    EV_CARD_REPRIORITIZED: frozenset({"id", "priority"}),
    EV_CARD_EDITED: frozenset({"id", "statement"}),
    EV_CARD_RESOURCE_PINNED: frozenset({"id", "gpus", "gpu_mem_mib"}),
    EV_CARD_DROPPED: frozenset({"id", "reason"}),
    EV_CARD_REOPENED: frozenset({"id", "reason"}),
    EV_CARD_FILED: frozenset({"id", "parent_card_id"}),
}
assert set(CONTROL_DATA_FIELDS) == set(CONTROL_EVENTS), "every control event needs a data allowlist"
# These keys are required on the EVENT but deliberately absent from the REQUEST. The command
# normalizers derive them from the folded run; accepting them from an agent would let it forge
# a paid internal research attempt or a report's publication trigger.
CONTROL_SERVER_DERIVED_FIELDS: dict[str, frozenset[str]] = {
    EV_RESEARCH_COMPLETED: frozenset({"at_node", "served_manual", "trigger"}),
    EV_REPORT_GENERATED: frozenset({"at_node", "trigger"}),
}

# Versioned collaboration takes the strict-CAS append (the retired compatibility /control route
# refused these outright): the durable command protocol requires an idempotency key plus the exact
# run generation the operator observed.
COLLABORATION_EVENTS = frozenset({
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED,
    EV_CONCEPT_TAG_EDITED,
    EV_CARD_REPRIORITIZED, EV_CARD_EDITED, EV_CARD_RESOURCE_PINNED, EV_CARD_DROPPED,
    EV_CARD_REOPENED, EV_CARD_FILED,
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
