"""Always-on assistant: a turn that outlives the HTTP request that asked for it (BACKLOG §F4).

The operator asked for three things — *"infinite assistant mode; waiting on statuses; monitoring
every N"* — and all three are the same structural ask: **the work has to survive the browser.**

A longer timeout cannot deliver any of them. A chat turn today lives inside one `POST
.../message_stream`; close the tab, refresh the page, or restart the server and the "monitoring"
the operator asked for silently stops, with nothing anywhere recording that it was ever asked for.
That is precisely the failure this feature exists to prevent, so the unit of work is a durable
**watch record** on disk plus a scheduler that re-reads it — not a bigger number.

## The shape, and where it comes from

Borrowed from how a long-running coding agent behaves, because those are the properties that make
an unattended agent tolerable rather than alarming:

* **A wake-up carries its own instruction.** The record holds the sentence the operator said, so a
  wake-up is re-enterable: the scheduler that services it after a restart needs nothing from the
  process that armed it. This is the whole reason the record is durable rather than a timer.
* **It says what it is waiting FOR, in words.** `waiting_for` is a sentence, stamped at arm time and
  shown wherever the watch is. "Waiting" with no stated object is indistinguishable from hung.
* **It reports progress instead of going dark.** Every poll updates `last_observation` with what the
  server actually saw, and every wake-up appends a real assistant turn to the session transcript —
  so the monitoring appears in the chat the operator opened, not in a log they have to find.
* **Bounded backoff, never a tight poll.** `next_poll_delay` ramps the interval toward a ceiling
  while the awaited state has not arrived. A watch on a run that finishes overnight costs a handful
  of cached `stat`s an hour, not one per second.

## The line this must not cross (doc 36)

A more autonomous assistant may decide what to DO next. It must gain no new path to what goes into
the RECORD, and a wider action space must not widen the TRUSTED set. Three properties hold that:

1. **The trigger is evaluated by the server, deterministically, over the folded run projection.**
   The agent never asserts "the run reached the state I was waiting for" — it is woken *because* the
   state was observed. An agent that could declare its own wake condition met would be reading a
   signal it controls, which corollary 1 names as a route around every gate.
2. **A wake-up turn's toolset is exactly `run_turn`'s toolset for the record's PINNED mode.** The
   watch adds no tool and no root. It is the same operator, in the same session, at a later moment;
   the only thing that changed is who typed. In particular a watch armed from a read-only chat can
   never wake into a mutating one — the mode is stamped at arm time and re-read, never re-derived.
3. **Every action a watch takes on a run still goes through the control-intent vocabulary**
   (`serve/protocol.py::CONTROL_EVENTS`, enforced by `serve/routers/control.py`) because it goes
   through the same `RunControlTools` an operator-typed turn uses. This module appends NO event of
   any kind and adds no control intent — engine invariant #1 is untouched, and deliberately so: an
   always-on assistant is the last thing that should be given a private door into the event log.

And the floor doc 36 asks for in the same breath as "effectively infinite": autonomy is not
unbounded spend. Every watch carries `max_wakeups` and `expires_at`, both capped by the constants
below, and both are recorded rather than implied.

## Restart, and the one honest refusal

`reconcile_on_start` runs when the service starts. A record left in `waking` means the process died
mid-turn, and what happens next depends on what that turn could have done:

* a **read-only** (`plan`) watch is simply re-armed. Re-running a read costs a model call and
  nothing else, and the alternative — dropping the monitoring on every server restart — is the bug.
* a **mutating** watch becomes `interrupted` and stops. Its turn may have applied half a change and
  the trace proving which half is gone. Silently re-entering it is the shape that applies an
  operator's action twice; this is the same "outcome unknown, verify this same operation" refusal
  the destructive UI paths already make, and it is a terminal state the operator resolves, not a
  state the scheduler resolves for them.

## "The run is not there" is TWO facts, and they take opposite answers

A `run_state` watch names a run by id, and absence is genuinely ambiguous: it can mean *not yet* —
armed just before a launch, which is the natural gesture and the one `watch_run`'s own description
invites ("your turn ends now and a fresh turn wakes when the state is observed") — or *never* (a
typo) / *gone* (a deleted run). Answering both with the same immediate terminal is how the natural
gesture failed SILENTLY: within two seconds the record read `failed`, *"run … no longer exists, so
this condition can never be met"*, and the chat that had just been told the watch survives a restart
held nothing at all. The same shape covers the LAUNCH WINDOW, because `run_dir` 404s until
`events.jsonl` exists — the directory alone is not yet a run.

They are told apart by EVIDENCE the record carries, in the same shape `reconcile_on_start` uses one
section up (ask what this record can PROVE, then say which branch it took), never by guessing:

* a run this watch has **already seen** and can no longer see is GONE. `run_seen` is stamped on the
  first observation and is durable, so this is a fact and not an inference — terminal at once, with
  the sentence it always had.
* a run it has **never seen** is NOT YET. Waiting is the right answer and an unbounded wait is not:
  a watch on a typo'd id would sit "waiting" until its lifetime ran out, which is its own silent
  failure. So the wait is bounded by `WATCH_RUN_APPEARANCE_GRACE_S` from the moment the operator
  armed it, and the bound is stated in the record rather than implied.
* and **while it waits it says so**. `last_error` carries a sentence naming the run that is not
  there and when this stops waiting for it, and `last_observation` records the absence as an
  observation (`present: false`) — the "reports progress instead of going dark" property applied to
  the one state where there is nothing to report. `ui/src/assistantWatchModel.js` already prints
  `last_error` as the row's note, so this reaches the strip with no client change.

**Whatever retires a watch, the operator gets a line to read.** Every terminal this scheduler
DECIDES — the lifetime, both absent-run branches, a wake-up turn that raised, an exhausted budget,
and the restart refusal above — appends a notice turn to the session transcript beside the wake-ups,
because a watch that stops with its only account of itself inside a JSON file under a dot-directory
has stopped exactly as silently as the monitoring this module exists to replace. A notice is not a
wake-up: it calls no model, holds no turn slot, spends nothing and counts against no budget. The one
terminal that gets no notice is `cancelled` by the operator, who is holding its receipt already.

## A watch is owned by the CHAT that armed it

`WatchStore` being portfolio-wide is a STORAGE decision (one directory beside the sessions, one file
per watch, no shared ledger behind a lock) and never an ownership one. Every load-bearing property
of a watch is its session's: `session` is required by `_valid`, `SessionWatches` filters on it so an
id from anywhere reaches nothing, the active cap is per session, the wake-up turn is built from that
session's history and refuses without it, and the report lands in that session's transcript. A watch
whose chat is gone cannot run, cannot be found, and cannot report — it is not free-standing, it is
orphaned.

So deletion CASCADES: `delete_for_session` is the irreversible half a session deletion calls, and it
matters most for what it removes rather than for what it stops — a watch instruction is the
operator's OWN sentence ("email the numbers to my supervisor at …"), and their own words must not
survive a deletion they asked for. It is a receipt-returning operation because it is irreversible
(the precedent is `serve/deletion_service.py` / `serve/memory_cascade.py`): it returns what it
removed, by id, status and condition — and deliberately NOT the instruction it just deleted.
`bootstrap` sweeps the same rule over records left by an out-of-band deletion or by a server that
predates the cascade, through the injected `session_exists` — whose answer is deliberately TRI-STATE
(there / provably gone / cannot tell), because an unprovable answer must leave the watch alone and a
boolean cannot say that.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from looplab.core.atomicio import atomic_write_text
from looplab.serve.protocol import (
    PHASE_APPROVAL, PHASE_FINALIZING, PHASE_FINISHED, PHASE_GROUNDING, PHASE_ONBOARDING,
    PHASE_PAUSED, PHASE_SEARCH, PHASE_SPEC_APPROVAL)
from looplab.tools.perm_modes import DEFAULT_MODE, MODES, normalize_mode

# ---- vocabularies (closed on purpose; a typo must be a refusal, not a watch that never fires) ----
WATCH_TRIGGER_KINDS = ("run_state", "target_status", "schedule", "work")

# What a `run_state` watch may wait for. The eight run PHASES the read model already publishes, plus
# ONE derived state the phase vocabulary cannot express: `engine_stopped` means no engine process
# holds the run's lock. That is the difference between "the run is finished" and "the run is not
# being worked on", and an operator who says "tell me when it stops" usually means the second — a
# crashed engine leaves the phase at `search` forever.
WATCH_RUN_STATES = (
    PHASE_FINISHED, PHASE_FINALIZING, PHASE_PAUSED, PHASE_APPROVAL, PHASE_SPEC_APPROVAL,
    PHASE_ONBOARDING, PHASE_GROUNDING, PHASE_SEARCH, "engine_stopped",
)

# Status vocabularies for the generalized wait surface. ``run_state`` remains a compatibility
# trigger because existing records must stay readable after an upgrade; new callers can address a
# whole run, one experiment (node), or one named stage through the same typed shape.  This registry
# is deliberately closed: a typo must fail while arming, not spend a day polling for a state no
# projection can ever publish.
WATCH_EXPERIMENT_STATES = ("pending", "evaluated", "failed", "tombstoned")
WATCH_STAGE_STATES = (
    "pending", "ok", "reused", "fail", "timeout", "needs_failed", "env_unsupported",
    "expect_failed", "check_failed",
)
WATCH_TARGET_STATES = {
    "run": WATCH_RUN_STATES,
    "experiment": WATCH_EXPERIMENT_STATES,
    "stage": WATCH_STAGE_STATES,
}

# `armed` -> `waking` -> (`armed` again for a schedule | a terminal). Terminals are final: nothing
# in this module transitions out of one, so a resolved watch cannot be resurrected by a stale poll.
WATCH_STATUSES = (
    "armed", "waking", "done", "blocked", "cancelled", "expired", "failed", "interrupted",
)
WATCH_TERMINAL_STATUSES = frozenset(
    {"done", "blocked", "cancelled", "expired", "failed", "interrupted"})

# ---- the floor under "effectively infinite" (doc 36: a budget plus a judgment, never neither) ----
WATCH_MIN_INTERVAL_S = 15.0          # below this a "monitor every N" is a tight poll wearing a name
WATCH_MAX_INTERVAL_S = 24 * 60 * 60.0
WATCH_POLL_BASE_S = 5.0              # first re-check of an unmet run-state condition
WATCH_POLL_CEILING_S = 60.0          # …ramping to here, and no further
WATCH_POLL_RAMP = 1.6
# How long a `run_state` watch waits for a run it has NEVER seen to appear before giving up on it
# (see the docstring's "TWO facts" section). Sized for the gesture it exists to protect — arm the
# watch, then launch the run: the operator still has to pick a task, fill in a config and clear
# preflight, and a run directory is not a run until `events.jsonl` exists. Long enough that a
# deliberate launch is never refused; short enough that a typo is answered inside the same sitting,
# rather than reading "waiting" until the 24 h lifetime runs out. Whichever of this and `expires_at`
# comes first, wins.
WATCH_RUN_APPEARANCE_GRACE_S = 15 * 60.0
WATCH_MAX_WAKEUPS_CEILING = 500
WATCH_DEFAULT_MAX_WAKEUPS = 24
WATCH_DEFAULT_LIFETIME_S = 24 * 60 * 60.0
WATCH_MAX_LIFETIME_S = 30 * 24 * 60 * 60.0
# Per SESSION, not per server: a chat that armed a dozen watches is a chat that will wake a dozen
# times, and the operator has to be able to read the list. A server-wide cap would instead let one
# runaway session starve every other chat's monitoring.
WATCH_MAX_ACTIVE_PER_SESSION = 8

WATCH_ID_RE = re.compile(r"[0-9a-f]{16}")
_WATCH_DIR = ".watches"
_MAX_INSTRUCTION_CHARS = 4000
_MAX_CHECKPOINT_SUMMARY_CHARS = 4000
_MAX_TODO_CHARS = 1000
_MAX_TODOS = 100
_MAX_TODOS_JSON_CHARS = 6000


class WatchDeferred(Exception):
    """The wake-up cannot run RIGHT NOW, and that is not a failure — try again shortly.

    Exists because "busy" and "broken" have opposite correct responses and the generic `except` in
    `_wake` would give both the same one. The only raiser today is the turn runner meeting the
    session's single-active-turn slot already held by the operator: the human typing wins, always,
    and the watch waits its turn rather than interleaving a machine-initiated turn into a
    conversation mid-sentence (which is what makes a transcript unreadable and Stop hit the wrong
    turn — the exact failure `_acquire_turn` exists to prevent).
    """


class WatchRefusal(ValueError):
    """A watch the server declines to arm, stated as one sentence for the operator or the agent.

    A plain `ValueError` on purpose rather than an `OperatorRefusal`: this never reaches the CLI's
    refusal boundary — both callers (the HTTP route and the agent tool) turn it into their own
    transport's refusal, and the tool's version is read by a model, which needs the sentence.
    """


def next_poll_delay(attempts: int, *, base: float = WATCH_POLL_BASE_S,
                    ceiling: float = WATCH_POLL_CEILING_S, ramp: float = WATCH_POLL_RAMP) -> float:
    """Seconds until the next check of an unmet condition — a BOUNDED ramp, never a tight poll.

    Stated as a pure function so its shape has a truth table: `attempts` is how many times the
    condition has already been checked and found unmet, so the FIRST re-check is at `base` (one
    check has happened, nothing is yet known about how long this will take) and the interval climbs
    to `ceiling` and stays there. A run that finishes overnight is therefore checked about sixty
    times an hour at the top of the ramp, against a state payload the read model caches by the event
    log's size+mtime — i.e. a `stat` per check on a quiescent run.
    """
    if attempts <= 1:
        return float(base)
    try:
        delay = float(base) * (float(ramp) ** (int(attempts) - 1))
    except (OverflowError, ValueError):
        return float(ceiling)
    if not math.isfinite(delay):
        return float(ceiling)
    return min(float(ceiling), delay)


def _bounded_float(value, *, low: float, high: float, default: float, what: str) -> float:
    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise WatchRefusal(f"{what} must be a number of seconds, not a boolean")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise WatchRefusal(f"{what} must be a number of seconds") from exc
    if not math.isfinite(out):
        raise WatchRefusal(f"{what} must be a finite number of seconds")
    # REFUSE, never clamp: an operator who asked to be woken every 2 seconds and is silently given
    # every 15 believes they configured something they did not. Same rule as `engine/widths.py`.
    if out < low or out > high:
        raise WatchRefusal(f"{what} must be between {low:g} and {high:g} seconds (got {out:g})")
    return out


def _normalize_todos(value, *, what: str = "todos") -> list[dict]:
    """A bounded, JSON-only TODO projection suitable for a durable work handoff."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise WatchRefusal(f"{what} must be an array")
    if len(value) > _MAX_TODOS:
        raise WatchRefusal(f"{what} may contain at most {_MAX_TODOS} items")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise WatchRefusal(f"every {what} item must be an object")
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending")
        if not content:
            raise WatchRefusal(f"every {what} item needs content")
        if len(content) > _MAX_TODO_CHARS:
            raise WatchRefusal(f"a {what} item is too long (max {_MAX_TODO_CHARS} chars)")
        if status not in ("pending", "in_progress", "completed"):
            raise WatchRefusal(
                f"unknown TODO status {status!r} — use pending, in_progress, or completed")
        out.append({"content": content, "status": status})
    encoded = json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > _MAX_TODOS_JSON_CHARS:
        raise WatchRefusal(
            f"{what} is too large for a resumable handoff (max {_MAX_TODOS_JSON_CHARS} chars)")
    return out


def normalize_target_status_trigger(trigger) -> dict:
    """Validate the typed run / experiment / stage condition shared by watches and work waits."""
    if not isinstance(trigger, dict):
        raise WatchRefusal("a target_status watch needs a trigger object")
    target = trigger.get("target")
    if not isinstance(target, dict):
        raise WatchRefusal("a target_status watch needs a typed `target` object")
    target_kind = str(target.get("kind") or "").strip()
    if target_kind not in WATCH_TARGET_STATES:
        raise WatchRefusal(
            f"unknown target kind {target_kind!r} — use one of: "
            f"{', '.join(WATCH_TARGET_STATES)}")
    run = str(target.get("run") or "").strip()
    if not run:
        raise WatchRefusal("a status target needs the run id")
    clean_target = {"kind": target_kind, "run": run}
    if target_kind in ("experiment", "stage"):
        node = target.get("node")
        if isinstance(node, bool):
            raise WatchRefusal("an experiment target needs a non-negative integer `node`")
        try:
            node = int(node)
        except (TypeError, ValueError) as exc:
            raise WatchRefusal(
                "an experiment target needs a non-negative integer `node`") from exc
        if node < 0:
            raise WatchRefusal("an experiment target needs a non-negative integer `node`")
        clean_target["node"] = node
    if target_kind == "stage":
        stage = str(target.get("stage") or "").strip()
        if not stage:
            raise WatchRefusal("a stage target needs its stage name")
        if len(stage) > 200:
            raise WatchRefusal("a stage name is too long (max 200 chars)")
        clean_target["stage"] = stage

    until = trigger.get("until")
    if isinstance(until, str):
        until = [until]
    allowed = WATCH_TARGET_STATES[target_kind]
    if not isinstance(until, (list, tuple)) or not until:
        raise WatchRefusal(
            f"a {target_kind} status watch needs `until` — one or more of: "
            f"{', '.join(allowed)}")
    wanted = []
    for state in until:
        state = str(state or "").strip()
        if state not in allowed:
            raise WatchRefusal(
                f"unknown {target_kind} state {state!r} — use one of: {', '.join(allowed)}")
        if state not in wanted:
            wanted.append(state)
    return {"kind": "target_status", "target": clean_target, "until": wanted}


def normalize_work_checkpoint(checkpoint) -> dict:
    """Validate the model's durable handoff before the scheduler acts on it.

    A malformed or missing checkpoint never defaults to ``continue``: after a mutating cycle the
    server cannot know which side effects happened, and replaying on a guess is the unsafe branch.
    """
    if not isinstance(checkpoint, dict):
        raise WatchRefusal("a continuous-work cycle must leave a checkpoint")
    status = str(checkpoint.get("status") or "").strip()
    if status not in ("continue", "waiting", "done", "blocked"):
        raise WatchRefusal(
            "checkpoint status must be continue, waiting, done, or blocked")
    summary = str(checkpoint.get("summary") or "").strip()
    if not summary:
        raise WatchRefusal("a continuous-work checkpoint needs a handoff summary")
    if len(summary) > _MAX_CHECKPOINT_SUMMARY_CHARS:
        raise WatchRefusal(
            f"the checkpoint summary is too long (max {_MAX_CHECKPOINT_SUMMARY_CHARS} chars)")
    if "todos" not in checkpoint:
        raise WatchRefusal("a continuous-work checkpoint needs the complete TODO list")
    out = {"status": status, "summary": summary,
           "todos": _normalize_todos(checkpoint.get("todos"), what="checkpoint todos")}
    if checkpoint.get("next_in_s") is not None:
        if status != "continue":
            raise WatchRefusal("next_in_s is only valid for a continue checkpoint")
        out["next_in_s"] = _bounded_float(
            checkpoint.get("next_in_s"), low=WATCH_MIN_INTERVAL_S,
            high=WATCH_MAX_INTERVAL_S, default=60.0, what="next_in_s")
    if status == "waiting":
        wait = checkpoint.get("wait")
        if isinstance(wait, dict) and wait.get("kind") is None:
            wait = {"kind": "target_status", **wait}
        out["wait"] = normalize_target_status_trigger(wait)
    elif checkpoint.get("wait") is not None:
        raise WatchRefusal("wait is only valid for a waiting checkpoint")
    return out


def observed_run_states(row: Optional[dict], *, engine_running: Optional[bool] = None) -> frozenset:
    """Which of `WATCH_RUN_STATES` a run is in RIGHT NOW, from the server's own read model.

    The trigger's whole evidentiary basis, and deliberately a projection of `run_summaries`' folded
    row rather than anything the assistant produced — see the module docstring's first property.
    An absent row is the empty set, and this function deliberately does not say what that MEANS:
    "gone" and "not yet" are the same empty set here and are told apart by `WatchService` from the
    record's own `run_seen` (see the docstring's "TWO facts" section), which is evidence this
    projection does not have.
    """
    if not isinstance(row, dict):
        return frozenset()
    states = set()
    phase = row.get("phase")
    if phase in WATCH_RUN_STATES:
        states.add(phase)
    if engine_running is None:
        engine_running = row.get("engine_running")
    # TRI-STATE, exactly as `engine_proc._engine_liveness` publishes it: True held, False
    # definitively free, None the probe could not tell (an `lstat` that raised on the mount a run
    # root lives on, a row that carries no liveness column at all). Only a definite False is
    # EVIDENCE that the engine stopped, and collapsing the other two into it (`bool(...)`, which is
    # what this said until 2026-08-15) fires an `engine_stopped` watch on a healthy training run at
    # the first transient hiccup — and that watch is a ONE-SHOT, so it spends a paid model call,
    # tells the operator the run stopped, goes `done`, and the REAL stop is then never reported at
    # all. Every other consumer of this probe already reads it this way (`_engine_alive` is `is not
    # False`, `attention.py` is `engine_running is not False`); a watch that spends money on the
    # answer is the last surface that should be the one guessing.
    if engine_running is False:
        states.add("engine_stopped")
    return frozenset(states)


class WatchStore:
    """Durable watch records under `<run_root>/assistant/.watches/<id>.json`.

    One file per watch, replaced atomically. A directory of independent files rather than one
    ledger because the scheduler rewrites exactly one record per tick and the HTTP list reads all of
    them: a shared file would put every wake-up behind a lock the list also wants, and a torn write
    would take out every watch instead of one. `.watches` is dot-prefixed so it can never collide
    with a session id (`SessionStore` requires 16 lowercase hex).
    """

    def __init__(self, run_root):
        self.dir = Path(run_root) / "assistant" / _WATCH_DIR
        self._lock = threading.Lock()
        # Ids this process has PROVEN terminal. The scheduler ticks every 2 s and `due()` used to
        # re-read, re-parse and re-validate every file in the directory each time — including every
        # terminal record, which nothing but `delete_for_session` ever unlinks, so the cost of one
        # tick grew with the server's whole watch HISTORY rather than with the watches it can still
        # service. This is deliberately a memo of ONE fact and not a mirror of the store: a terminal
        # is FINAL (`WATCH_TERMINAL_STATUSES`, and nothing in this module transitions out of one), so
        # "skip it" can never go stale, while a full in-memory index would be wrong the moment a
        # second process — or a test writing a record by hand — armed one behind our back. Unknown
        # ids are still read from disk, so anything new is still found.
        self._settled: set[str] = set()

    # ---- paths / io -----------------------------------------------------------------------
    def _path(self, watch_id: str) -> Path:
        if not isinstance(watch_id, str) or WATCH_ID_RE.fullmatch(watch_id) is None:
            raise WatchRefusal("bad watch id")
        return self.dir / f"{watch_id}.json"

    def _note_settled(self, record: Optional[dict]) -> Optional[dict]:
        """Record that this id is terminal, at the ONE point every read and write passes through."""
        if record is not None and record.get("status") in WATCH_TERMINAL_STATUSES:
            self._settled.add(record["id"])
        return record

    def _read(self, watch_id: str) -> Optional[dict]:
        try:
            record = json.loads(self._path(watch_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, WatchRefusal):
            return None
        return self._note_settled(record) if self._valid(record) else None

    def _write(self, record: dict) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {**record, "updated": time.time()}
        atomic_write_text(self._path(record["id"]), json.dumps(record))
        return self._note_settled(record)

    @staticmethod
    def _valid(record) -> bool:
        """Total over whatever is on disk. A half-written or hand-edited record is DROPPED, not
        repaired: this drives an autonomous, paid wake-up, and a record whose instruction or mode
        cannot be trusted must not run at all."""
        if not isinstance(record, dict):
            return False
        if not isinstance(record.get("id"), str) or WATCH_ID_RE.fullmatch(record["id"]) is None:
            return False
        if record.get("status") not in WATCH_STATUSES:
            return False
        if record.get("mode") not in MODES:
            return False
        if not isinstance(record.get("instruction"), str) or not record["instruction"].strip():
            return False
        trigger = record.get("trigger")
        if not isinstance(trigger, dict) or trigger.get("kind") not in WATCH_TRIGGER_KINDS:
            return False
        try:
            # A known kind is not enough for an autonomous, potentially paid wake-up: validate the
            # entire condition again at the disk trust boundary. In particular, a hand-edited work
            # record with no resumable checkpoint must be dropped rather than started from a guess.
            WatchStore._normalize_trigger(trigger)
            if trigger.get("kind") == "work":
                normalize_work_checkpoint(record.get("checkpoint"))
        except WatchRefusal:
            return False
        return isinstance(record.get("session"), str) and bool(record["session"])

    # ---- arming ---------------------------------------------------------------------------
    def arm(self, *, session: str, instruction: str, trigger: dict, mode: str = DEFAULT_MODE,
            waiting_for: str = "", max_wakeups=None, lifetime_s=None,
            now: Optional[float] = None) -> dict:
        """Create an armed watch, or refuse with one sentence saying why.

        `mode` is PINNED here and never re-derived at wake time (module docstring, property 2): a
        watch armed from a read-only chat stays read-only for its whole life even if the session is
        later switched to a mutating mode, because the operator consented to this instruction under
        the mode they were in when they said it.
        """
        instruction = str(instruction or "").strip()
        if not instruction:
            raise WatchRefusal("a watch needs an instruction — the sentence to carry out on wake-up")
        if len(instruction) > _MAX_INSTRUCTION_CHARS:
            raise WatchRefusal(f"the instruction is too long (max {_MAX_INSTRUCTION_CHARS} chars)")
        trigger = self._normalize_trigger(trigger)
        initial_todos = trigger.pop("initial_todos", []) if trigger.get("kind") == "work" else []
        ts = time.time() if now is None else float(now)
        wakeups_cap = int(_bounded_float(
            max_wakeups, low=1, high=WATCH_MAX_WAKEUPS_CEILING,
            default=WATCH_DEFAULT_MAX_WAKEUPS, what="max_wakeups"))
        lifetime = _bounded_float(lifetime_s, low=WATCH_MIN_INTERVAL_S, high=WATCH_MAX_LIFETIME_S,
                                  default=WATCH_DEFAULT_LIFETIME_S, what="lifetime_s")
        record = {
            "id": secrets.token_hex(8),
            "session": str(session),
            "mode": normalize_mode(mode),
            "instruction": instruction,
            "trigger": trigger,
            "status": "armed",
            "created": ts,
            "updated": ts,
            # The first check of a status/work watch is IMMEDIATE (the condition may already hold —
            # "tell me when run X finishes" about a run that finished an hour ago must answer now,
            # not in five seconds). A schedule's first wake-up is one interval away, because "every
            # N minutes" starting instantly would fire twice for the operator's first interval.
            "next_due": (ts + trigger["every_s"]
                         if trigger["kind"] == "schedule" else ts),
            "attempts": 0,
            "wakeups": 0,
            "max_wakeups": wakeups_cap,
            "expires_at": ts + lifetime,
            "waiting_for": (str(waiting_for or "").strip() or describe_trigger(trigger))[:300],
            "last_observation": None,
            "last_error": "",
        }
        if trigger["kind"] == "run_state":
            # Has this watch ever SEEN the run it names? Written here as an explicit `false` rather
            # than left absent, because it is the fact that decides whether a run that is not there
            # is gone or not yet, and a record an operator reads should show it. A pre-existing
            # record without the key reads the same way — absent is "never seen".
            record["run_seen"] = False
        elif trigger["kind"] == "target_status":
            record["target_seen"] = False
        elif trigger["kind"] == "work":
            # This is the compact durable handoff, distinct from the transcript. The transcript is
            # evidence and context; the checkpoint is the bounded state a fresh process needs in
            # order to resume the next safe cycle without asking the model to summarize history.
            record["checkpoint"] = {
                "status": "continue", "summary": "No work cycle has run yet.",
                "todos": initial_todos,
            }
        with self._lock:
            # COUNT AND WRITE UNDER ONE LOCK. This was a check-then-act with the count outside the
            # lock and the write inside it, so two arms racing (two tabs, or the agent tool and the
            # HTTP route) both read `n` and both wrote, leaving the session above its cap. The cap is
            # this module's stated bound on UNATTENDED SPEND — every active watch is up to
            # `max_wakeups` paid model calls nobody is watching — and nothing downstream re-checks
            # it, so the arm is the only place it can hold.
            active = [w for w in self.list(session=session)
                      if w["status"] not in WATCH_TERMINAL_STATUSES]
            if len(active) >= WATCH_MAX_ACTIVE_PER_SESSION:
                raise WatchRefusal(
                    f"this chat already has {len(active)} active watches (max "
                    f"{WATCH_MAX_ACTIVE_PER_SESSION}); stop one before arming another")
            return self._write(record)

    @staticmethod
    def _normalize_trigger(trigger) -> dict:
        if not isinstance(trigger, dict):
            raise WatchRefusal("a watch needs a trigger")
        kind = trigger.get("kind")
        if kind not in WATCH_TRIGGER_KINDS:
            raise WatchRefusal(
                f"unknown watch kind {kind!r} — use one of {', '.join(WATCH_TRIGGER_KINDS)}")
        if kind == "schedule":
            every = _bounded_float(trigger.get("every_s"), low=WATCH_MIN_INTERVAL_S,
                                   high=WATCH_MAX_INTERVAL_S, default=300.0, what="every_s")
            return {"kind": "schedule", "every_s": every}
        if kind == "work":
            every = _bounded_float(trigger.get("every_s"), low=WATCH_MIN_INTERVAL_S,
                                   high=WATCH_MAX_INTERVAL_S, default=60.0, what="every_s")
            return {"kind": "work", "every_s": every,
                    "initial_todos": _normalize_todos(trigger.get("initial_todos"),
                                                      what="initial_todos")}
        if kind == "target_status":
            return normalize_target_status_trigger(trigger)
        run = str(trigger.get("run") or "").strip()
        if not run:
            raise WatchRefusal("a run_state watch needs the run id to watch")
        until = trigger.get("until")
        if isinstance(until, str):
            until = [until]
        if not isinstance(until, (list, tuple)) or not until:
            raise WatchRefusal(
                f"a run_state watch needs `until` — one or more of: {', '.join(WATCH_RUN_STATES)}")
        wanted = []
        for state in until:
            state = str(state or "").strip()
            if state not in WATCH_RUN_STATES:
                raise WatchRefusal(
                    f"unknown run state {state!r} — use one of: {', '.join(WATCH_RUN_STATES)}")
            if state not in wanted:
                wanted.append(state)
        return {"kind": "run_state", "run": run, "until": wanted}

    # ---- reading --------------------------------------------------------------------------
    def get(self, watch_id: str) -> Optional[dict]:
        return self._read(watch_id)

    def _entries(self) -> list:
        try:
            return sorted(self.dir.iterdir())
        except OSError:
            return []

    def list(self, *, session: Optional[str] = None, active_only: bool = False) -> list[dict]:
        out = []
        for path in self._entries():
            if path.suffix != ".json" or WATCH_ID_RE.fullmatch(path.stem) is None:
                continue
            record = self._read(path.stem)
            if record is None:
                continue
            if session is not None and record.get("session") != session:
                continue
            if active_only and record.get("status") in WATCH_TERMINAL_STATUSES:
                continue
            out.append(record)
        out.sort(key=lambda r: r.get("created", 0))
        return out

    # REVIEW 2026-08-18 (efficiency): the `_settled` memo stops at TERMINAL records — an ARMED watch
    # is still read + json-parsed + fully re-validated (`_valid` re-runs `_normalize_trigger`) on
    # every 2 s tick even when its own `next_due` is minutes away, so a watch backed off at the 60 s
    # ceiling costs 30 reads/min off the runs-root mount just to learn it is not due (measured: 30
    # reads over 30 ticks). A per-id (file stat identity, next_due) hint — invalidated the way
    # `reuse_refusal` already trusts stat tuples — keeps the "another process armed one behind our
    # back" property while reducing a steady-state tick to one scandir plus stats. Separately, the
    # scheduler thread has no production `stop()` caller and `_loop` never exits on its own, so once
    # `ensure_started` fires the process ticks forever even after the last watch settles.
    def due(self, *, now: Optional[float] = None) -> list[dict]:
        """Armed watches whose next check has come round, oldest-due first (fair under a cap).

        This runs every `WatchService.interval_s` (2 s) for the life of the server, so it costs what
        a TICK costs — and it deliberately does not go through `list()`, which reads and parses every
        record on disk. A terminal record is never serviced again and is only ever removed by a
        session deletion, so the ones this server has already settled accumulate: `_settled` skips
        them without opening the file. An id this process has not proven terminal is still read from
        disk, so a watch armed by another process (or written by hand in a test) is still found.
        """
        ts = time.time() if now is None else float(now)
        ready = []
        for path in self._entries():
            if path.suffix != ".json" or WATCH_ID_RE.fullmatch(path.stem) is None:
                continue
            if path.stem in self._settled:
                continue
            record = self._read(path.stem)      # memoizes a terminal it finds, for the next tick
            if record is None or record.get("status") != "armed":
                continue
            if float(record.get("next_due", 0) or 0) <= ts:
                ready.append(record)
        ready.sort(key=lambda r: float(r.get("next_due", 0) or 0))
        return ready

    # ---- transitions ----------------------------------------------------------------------
    def update(self, watch_id: str, **fields) -> Optional[dict]:
        """Read-modify-write one record. Refuses to move a TERMINAL watch: a wake-up that finishes
        after the operator stopped its watch must not re-arm it, and that race is ordinary (a stop
        arrives while the turn it stops is mid-flight)."""
        with self._lock:
            record = self._read(watch_id)
            if record is None:
                return None
            if record["status"] in WATCH_TERMINAL_STATUSES:
                return record
            return self._write({**record, **fields})

    def claim(self, watch_id: str, *, now: Optional[float] = None) -> Optional[dict]:
        """Move `armed` -> `waking` under the store lock, or return None if someone else has it."""
        ts = time.time() if now is None else float(now)
        with self._lock:
            record = self._read(watch_id)
            if record is None or record["status"] != "armed":
                return None
            return self._write({**record, "status": "waking", "claimed_at": ts})

    def cancel(self, watch_id: str, *, reason: str = "stopped by the operator") -> Optional[dict]:
        """The operator's stop, which is `update` with a status — deliberately not a second copy of
        it. This spelled out the same lock/read/None-guard/terminal-guard/write ladder line for line,
        i.e. TWO implementations of the terminal-refusal rule this module's docstring calls
        load-bearing ("a wake-up that finishes after the operator stopped its watch must not re-arm
        it"). Two copies of a rule are one commit away from being two rules."""
        return self.update(watch_id, status="cancelled", last_error=str(reason)[:300])

    def delete_for_session(self, session: str) -> list[dict]:
        """Remove every watch this chat owns, and RETURN the receipt of what was removed.

        The cascade half of the ownership model (module docstring). IRREVERSIBLE, and the thing it
        removes is the point: a watch instruction is the operator's own sentence, and a chat they
        deleted must not go on holding it — the standing monitoring stopping is the smaller half.

        Cancel-then-unlink under one lock, in that order, because a scheduler tick may already be
        inside `_wake` for one of these: `claim`/`update` re-read the record, and a terminal is the
        one thing they will not act on, so the cancel closes the window that the unlink alone would
        leave open. A record whose file cannot be removed is reported with `removed: false` and
        stays CANCELLED — it can never wake again, which is the property that must not depend on the
        filesystem cooperating.

        The receipt names the id, the condition and the status. It deliberately does NOT echo the
        instruction: repeating the sentence back in the response to the request to delete it would
        be the same failure one layer up.
        """
        session = str(session)
        receipt = []
        for known in self.list(session=session):
            with self._lock:
                record = self._read(known["id"])
                if record is None:
                    continue
                if record["status"] not in WATCH_TERMINAL_STATUSES:
                    record = self._write({
                        **record, "status": "cancelled",
                        "last_error": "the chat that armed this watch was deleted"})
                row = {"id": record["id"], "status": record["status"],
                       "waiting_for": record.get("waiting_for"), "removed": True}
                try:
                    self._path(record["id"]).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    row["removed"] = False
            receipt.append(row)
        return receipt

    def reconcile_on_start(self) -> list[dict]:
        """Settle every record a dead process left mid-wake. See the module docstring's last section.

        Returns the records it changed, so the caller can log what a restart cost — a silent
        `interrupted` is exactly as invisible as the dropped monitoring this feature exists to fix.
        """
        changed = []
        for record in self.list():
            if record.get("status") != "waking":
                continue
            with self._lock:
                current = self._read(record["id"])
                if current is None or current.get("status") != "waking":
                    continue
                if current["mode"] == "plan":
                    changed.append(self._write({
                        **current, "status": "armed", "next_due": time.time(),
                        "last_error": "re-armed after a server restart interrupted its wake-up"}))
                else:
                    changed.append(self._write({
                        **current, "status": "interrupted",
                        "last_error": ("a server restart interrupted this wake-up in a mutating "
                                       "mode; it may have applied part of its change, so it will "
                                       "not be re-entered automatically — check and re-arm it")}))
        return changed


def describe_trigger(trigger: dict) -> str:
    """One sentence naming what a watch is waiting FOR. Never empty — a watch with nothing to say
    about its own condition reads as hung, which is the state this whole module is trying to make
    impossible to confuse with working."""
    if not isinstance(trigger, dict):
        return "an unreadable condition"
    if trigger.get("kind") == "schedule":
        every = float(trigger.get("every_s") or 0)
        if every >= 3600:
            unit = f"{every / 3600:g} h"
        elif every >= 60:
            unit = f"{every / 60:g} min"
        else:
            unit = f"{every:g} s"
        return f"every {unit}"
    if trigger.get("kind") == "work":
        return "continuous work until it reports done or blocked"
    if trigger.get("kind") == "target_status":
        target = trigger.get("target") or {}
        label = f"run {target.get('run')}"
        if target.get("kind") in ("experiment", "stage"):
            label += f" experiment {target.get('node')}"
        if target.get("kind") == "stage":
            label += f" stage {target.get('stage')}"
        states = trigger.get("until") or []
        return f"{label} to reach {' or '.join(str(s) for s in states)}"
    states = trigger.get("until") or []
    return f"run {trigger.get('run')} to reach {' or '.join(str(s) for s in states)}"


# The preamble a wake-up turn carries in front of the operator's own instruction. It states three
# things the model cannot otherwise know and would otherwise invent: that this turn was started by a
# clock rather than by a person, what was observed, and that nobody is necessarily reading. The last
# one matters — an agent that believes it is in a conversation asks clarifying questions into a void.
WAKEUP_PREAMBLE = (
    "[automatic wake-up — nobody typed this]\n"
    "You are a standing watch this chat armed earlier. It has just fired.\n"
    "Waiting for: {waiting_for}\n"
    "What the server observed: {observation}\n"
    "This is wake-up {wakeup} of at most {max_wakeups}.\n"
    "\n"
    "Carry out the standing instruction below and report what you found. The operator may not be "
    "watching, so state your conclusion plainly and do not ask questions you cannot get an answer "
    "to; if there is nothing new to report, say exactly that in one line.\n"
    "\n"
    "Standing instruction:\n{instruction}"
)

WORK_PREAMBLE = (
    "[automatic continuous-work cycle — nobody typed this]\n"
    "This chat previously armed durable work that survives page and server restarts.\n"
    "Goal: {goal}\n"
    "Previous checkpoint: {summary}\n"
    "Current TODOs: {todos}\n"
    "This is cycle {wakeup} of at most {max_wakeups}.\n"
    "Server observation that resumed this cycle: {observation}\n"
    "\n"
    "Make concrete progress toward the goal using the tools available in the pinned permission "
    "mode. Do not busy-poll a run, experiment, or stage: if progress depends on one, checkpoint as "
    "`waiting` with a typed target condition and let the server observe it without model calls. "
    "Before `final_answer`, call `checkpoint_work` exactly once with a compact handoff, the complete "
    "current TODO list, and one decision: `continue`, `waiting`, `done`, or `blocked`. Use `done` only "
    "after verifying the goal; use `blocked` when another autonomous cycle cannot safely help."
)


# REVIEW 2026-08-18 (simplification): `json.dumps(observation, sort_keys=True, default=str)[:1500]`
# is spelled out verbatim in BOTH branches below, and the 1500 is a bare literal while every sibling
# cap in this file is a named constant (`_MAX_CHECKPOINT_SUMMARY_CHARS`, `_MAX_TODOS_JSON_CHARS`,
# `_MAX_INSTRUCTION_CHARS`); `routers/assistant.py` additionally hand-quotes "1,500 chars" in a
# docstring, so one rule is stated in three places that can drift independently. Hoist a single
# `observation_json` local bounded by a `_MAX_OBSERVATION_CHARS` constant the docstring can name.
def wakeup_instruction(record: dict, observation) -> str:
    """The full model-facing instruction for one wake-up — the record's own sentence, in context."""
    if (record.get("trigger") or {}).get("kind") == "work":
        checkpoint = record.get("checkpoint") if isinstance(record.get("checkpoint"), dict) else {}
        return WORK_PREAMBLE.format(
            goal=record.get("instruction", ""),
            summary=str(checkpoint.get("summary") or "No prior checkpoint.")[:_MAX_CHECKPOINT_SUMMARY_CHARS],
            todos=json.dumps(checkpoint.get("todos") or [], ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))[:_MAX_TODOS_JSON_CHARS],
            observation=json.dumps(observation, sort_keys=True, default=str)[:1500],
            wakeup=int(record.get("wakeups", 0)) + 1,
            max_wakeups=record.get("max_wakeups"),
        )
    return WAKEUP_PREAMBLE.format(
        waiting_for=record.get("waiting_for") or describe_trigger(record.get("trigger") or {}),
        observation=json.dumps(observation, sort_keys=True, default=str)[:1500],
        wakeup=int(record.get("wakeups", 0)) + 1,
        max_wakeups=record.get("max_wakeups"),
        instruction=record.get("instruction", ""))


class SessionWatches:
    """The watch surface ONE chat may touch — its own session's records, at its own pinned mode.

    The scoping is the point, not convenience. `WatchStore` is portfolio-wide (one directory beside
    the sessions), and the tool that reaches it is driven by a model reading text from files, run
    logs and MCP servers. A bare store handed to the toolset would let a `stop_watch("<id>")` with an
    id from anywhere cancel another chat's standing monitoring; every method here filters on the
    session first, so an id the model did not get from its own `list_watches` simply is not there.
    """

    def __init__(self, store: WatchStore, session: str, mode: str = DEFAULT_MODE,
                 on_arm: Optional[Callable] = None):
        self.store = store
        self.session = str(session)
        self.mode = normalize_mode(mode)
        # The scheduler thread is started LAZILY (see `WatchService.ensure_started`), so the newly
        # armed watch has to be what starts it. Without this hook a watch armed into an idle server
        # would sit `armed` until something else happened to wake the scheduler — which is a watch
        # that silently does not watch, the exact failure the whole module exists to prevent.
        self.on_arm = on_arm

    def arm(self, *, instruction: str, trigger: dict, waiting_for: str = "",
            max_wakeups=None, lifetime_s=None) -> dict:
        record = self.store.arm(session=self.session, instruction=instruction, trigger=trigger,
                                mode=self.mode, waiting_for=waiting_for,
                                max_wakeups=max_wakeups, lifetime_s=lifetime_s)
        if self.on_arm is not None:
            try:
                self.on_arm()
            except Exception:  # noqa: BLE001 - a scheduler that will not start is reported by the
                pass           # watch never firing, not by losing the record the operator asked for
        return record

    def list(self, *, active_only: bool = False) -> list[dict]:
        return self.store.list(session=self.session, active_only=active_only)

    def cancel(self, watch_id: str) -> Optional[dict]:
        record = self.store.get(watch_id) if isinstance(watch_id, str) else None
        if record is None or record.get("session") != self.session:
            return None
        return self.store.cancel(watch_id)


class WatchService:
    """The scheduler: one daemon thread that services due watches, and a `tick` that is the whole
    decision so tests drive it directly with no thread, no sleeping and no clock of their own.

    Everything the service needs from the rest of the server is INJECTED — the run observation, the
    turn runner, the transcript append. That keeps this module free of any import of the FastAPI
    layer (so `routers/assistant.py` may import it and never the reverse) and, more usefully, makes
    the interesting behaviour — backoff, the terminal ladder, the restart refusal — reachable
    without an HTTP client or a model.
    """

    def __init__(self, store: WatchStore, *, observe_run: Callable, run_turn_fn: Callable,
                 append_turn: Callable, observe_target: Optional[Callable] = None,
                 interval_s: float = 2.0, on_error: Optional[Callable] = None,
                 session_exists: Optional[Callable] = None):
        self.store = store
        self.observe_run = observe_run          # run_id -> the read model's row (or None)
        self.observe_target = observe_target    # typed target -> bounded status projection
        self.run_turn_fn = run_turn_fn          # (record, instruction) -> the turn result dict
        self.append_turn = append_turn          # (session, turn dict) -> None
        # session id -> True (there) / False (PROVABLY gone) / None (cannot tell). The third answer
        # is not a nicety: this one is wired to an irreversible delete. See `_sweep_orphaned_watches`.
        self.session_exists = session_exists
        self.interval_s = float(interval_s)
        self.on_error = on_error
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._stop = threading.Event()

    # ---- lifecycle ------------------------------------------------------------------------
    def bootstrap(self) -> list[dict]:
        """Settle the previous process's records, and start the thread only if there IS work.

        Called once at app construction. The conditional start is not an optimization: every test
        that builds an app would otherwise get a polling daemon thread it never asked for, and a
        scheduler running in a thousand test processes is how a background feature becomes a
        flake nobody can attribute. A server with no armed watches has nothing to schedule, and the
        moment one is armed `ensure_started` starts it.

        It also sweeps ORPHANS — records whose chat is gone — because the cascade on the deletion
        route only covers deletions this build made. Records left by an earlier server, or by any
        removal that did not go through that route, would otherwise poll and hold their operator's
        instruction forever, and startup is the one moment every record is looked at anyway.
        """
        changed = self.store.reconcile_on_start()
        for record in changed:
            # A restart refusal nobody is told about is exactly as invisible as the dropped
            # monitoring this module exists to fix — so the terminal it leaves gets a line in the
            # chat, the same as every other terminal the scheduler decides.
            if record.get("status") in WATCH_TERMINAL_STATUSES:
                self._notice(record, record.get("last_error") or "this watch was settled at startup")
        removed = self._sweep_orphaned_watches()
        if self.store.list(active_only=True):
            self.ensure_started()
        return changed + removed

    def _sweep_orphaned_watches(self) -> list[dict]:
        """Delete the watches of chats that no longer exist. See `WatchStore.delete_for_session`.

        `session_exists` is INJECTED like the other three collaborators, so this module still knows
        nothing about how a chat is stored. Its answer is TRI-STATE, and that is the whole safety of
        this sweep: `False` means the chat is PROVABLY gone, `True` means it is there, and anything
        else — `None`, or a raise — means "cannot tell". Only a proof deletes.

        It was `bool(self.session_exists(sid))`, which reads every unprovable answer as "gone" and
        then irreversibly unlinks a live chat's standing watches, including the operator's own
        sentence inside them. The reachable shapes are ordinary rather than exotic: the session
        store's metadata read catches `(OSError, ValueError)` and returns None, so a transient
        EIO/EACCES on the mount a run root lives on, a `meta.json` observed mid-rewrite, or a record
        whose `mode` this build does not recognize each answered "this chat is gone" — while a RAISE,
        the only shape this treated as unprovable, is the narrowest of them.
        """
        if self.session_exists is None:
            return []
        removed, answered = [], {}
        for record in self.store.list():
            sid = record.get("session")
            if sid in answered:
                continue
            # Assume nothing is provable until it is: every early exit below leaves the watch alone.
            answered[sid] = True
            try:
                present = self.session_exists(sid)
            except Exception:  # noqa: BLE001 - an unreadable chat is not a deleted one
                continue
            if present is False:
                removed.extend(self.store.delete_for_session(sid))
        return removed

    def ensure_started(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="looplab-assistant-watch")
            self._thread.start()

    def start(self) -> None:
        """Unconditional start — for a caller that wants the scheduler running with no records yet."""
        self.ensure_started()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a bad record must never kill the scheduler
                if self.on_error is not None:
                    try:
                        self.on_error(exc)
                    except Exception:  # noqa: BLE001
                        pass

    # ---- the decision ---------------------------------------------------------------------
    def tick(self, *, now: Optional[float] = None) -> list[dict]:
        """Service every due watch once. Returns the records it touched (for tests and logging).

        ONE thread, and each wake-up runs its turn inline, so watches are serviced strictly in
        due order and never concurrently. That is deliberate rather than a limitation: the alternative
        is N unattended model calls in flight against the same shared endpoint, competing with the
        operator's own turn for provider concurrency — a stall with no visible cause, which is the
        cost `core/llm_broker.py` exists to bound for the engine's lanes. The interval between wake-ups
        is a floor, not a promise, and a watch delayed behind a sibling says so through its `next_due`.
        """
        ts = time.time() if now is None else float(now)
        touched = []
        for record in self.store.due(now=ts):
            # Expiry is checked HERE rather than by a sweeper, so the record that expires is the one
            # about to spend money, and the expiry is observed at the moment it would have.
            if ts >= float(record.get("expires_at", 0) or 0):
                touched.append(self._retire(
                    record, status="expired",
                    reason="the watch reached its lifetime without being resolved"))
                continue
            touched.append(self._service(record, now=ts))
        return [r for r in touched if r is not None]

    def _service(self, record: dict, *, now: float):
        trigger = record.get("trigger") or {}
        if trigger.get("kind") == "run_state":
            return self._service_run_state(record, trigger, now=now)
        if trigger.get("kind") == "target_status":
            return self._service_target_status(record, trigger, now=now)
        if trigger.get("kind") == "work":
            checkpoint = record.get("checkpoint") or {}
            if checkpoint.get("status") == "waiting" and isinstance(checkpoint.get("wait"), dict):
                return self._service_target_status(
                    record, checkpoint["wait"], now=now, resume_work=True)
            return self._wake(
                record, observation={"reason": "continuous_work_cycle", "at": now}, now=now)
        return self._wake(record, observation={"reason": "scheduled", "at": now}, now=now)

    def _service_target_status(self, record: dict, trigger: dict, *, now: float,
                               resume_work: bool = False):
        """Evaluate one typed status condition without asking the model.

        Identity is pinned on first sight. A run reset or a node retry is a different target even
        when it reuses the same human label; silently following it would make a watch report on an
        object the operator never armed and make a work cycle repeat against unknown state.
        """
        terminal = "blocked" if resume_work else "failed"
        if self.observe_target is None:
            return self._retire(
                record, status=terminal,
                reason="typed status observation is unavailable on this server")
        target = trigger.get("target") or {}
        try:
            observation = self.observe_target(target)
        except Exception as exc:  # noqa: BLE001 - a read failure is a retry, never a guessed state
            attempts = int(record.get("attempts", 0)) + 1
            return self.store.update(
                record["id"], attempts=attempts,
                next_due=now + next_poll_delay(attempts),
                last_error=(f"could not read {describe_trigger(trigger)}: "
                            f"{type(exc).__name__}"))

        prefix = "work_wait_" if resume_work else "target_"
        if record.get(f"{prefix}seen") and isinstance(observation, dict):
            pinned_generation = record.get(f"{prefix}run_generation")
            observed_generation = observation.get("run_generation")
            if (pinned_generation is not None and observed_generation is not None
                    and pinned_generation != observed_generation):
                return self._retire(
                    record, status=terminal, observation=observation,
                    reason=(f"{describe_trigger(trigger)} changed run generation while it was "
                            "being watched; the replacement was not followed automatically"))
            pinned_attempt = record.get(f"{prefix}attempt")
            observed_attempt = observation.get("attempt")
            if (pinned_attempt is not None and observed_attempt is not None
                    and pinned_attempt != observed_attempt):
                return self._retire(
                    record, status=terminal, observation=observation,
                    reason=(f"{describe_trigger(trigger)} changed experiment attempt while it was "
                            "being watched; the replacement was not followed automatically"))
        if isinstance(observation, dict) and observation.get("impossible"):
            return self._retire(
                record, status=terminal, observation=observation,
                reason=str(observation.get("reason") or
                           f"{describe_trigger(trigger)} can no longer be reached"))
        if not isinstance(observation, dict) or not observation.get("present"):
            return self._service_absent_target(
                record, trigger, observation, now=now, prefix=prefix, terminal=terminal)

        fields = {}
        if not record.get(f"{prefix}seen"):
            fields[f"{prefix}seen"] = True
            if observation.get("run_generation") is not None:
                fields[f"{prefix}run_generation"] = observation.get("run_generation")
            if observation.get("attempt") is not None:
                fields[f"{prefix}attempt"] = observation.get("attempt")
            record = self.store.update(record["id"], **fields) or record

        states = frozenset(str(s) for s in (observation.get("states") or ()))
        if not states.intersection(trigger.get("until") or ()):
            attempts = int(record.get("attempts", 0)) + 1
            return self.store.update(
                record["id"], attempts=attempts, last_observation=observation,
                next_due=now + next_poll_delay(attempts), last_error="")
        return self._wake(record, observation=observation, now=now)

    def _service_absent_target(self, record: dict, trigger: dict, observation, *, now: float,
                               prefix: str, terminal: str):
        label = describe_trigger(trigger)
        observation = observation if isinstance(observation, dict) else {
            "target": trigger.get("target"), "present": False}
        if record.get(f"{prefix}seen"):
            return self._retire(
                record, status=terminal, observation=observation,
                reason=f"{label} disappeared, so this exact condition can no longer be met")
        started = float(record.get(f"{prefix}started", record.get("created", now)) or now)
        deadline = started + WATCH_RUN_APPEARANCE_GRACE_S
        if now >= deadline:
            return self._retire(
                record, status=terminal, observation=observation,
                reason=(f"{label} never appeared in the "
                        f"{WATCH_RUN_APPEARANCE_GRACE_S / 60:g} minute appearance window"))
        attempts = int(record.get("attempts", 0)) + 1
        observation = {**observation, "present": False, "awaiting_first_sight": True,
                       "give_up_at": deadline}
        return self.store.update(
            record["id"], attempts=attempts, last_observation=observation,
            next_due=min(now + next_poll_delay(attempts), deadline),
            last_error=(f"{label} does not exist yet — waiting for it to appear "
                        f"(giving up in {max(0.0, deadline - now) / 60:.0f} min if it does not)"))

    def _service_run_state(self, record: dict, trigger: dict, *, now: float):
        run_id = trigger.get("run")
        try:
            row = self.observe_run(run_id)
        except Exception as exc:  # noqa: BLE001 - a read failure is a RETRY, never a terminal
            # Deliberately not `failed`: an unreadable run is usually a transient filesystem or
            # lock condition, and a watch that gives up on the first one is worse than useless on
            # the geesefs mounts a run root lives on.
            return self.store.update(
                record["id"], attempts=int(record.get("attempts", 0)) + 1,
                next_due=now + next_poll_delay(int(record.get("attempts", 0)) + 1),
                last_error=f"could not read run {run_id}: {type(exc).__name__}")
        if row is None:
            return self._service_absent_run(record, run_id, now=now)
        if not record.get("run_seen"):
            # First sight. Durable, and it is what makes a LATER absence provably "gone" rather than
            # "not yet" — one extra write per watch, once, for a fact no re-derivation can recover
            # (the run's own directory is the thing that disappears).
            record = self.store.update(record["id"], run_seen=True) or record
        states = observed_run_states(row)
        # A PROJECTION of the row, never the row: this is written to the durable record on every poll,
        # echoed by the HTTP list, and JSON-dumped into the wake-up preamble under a 1,500-char cap —
        # so an observation that carries whatever the read model happened to return is one that
        # truncates before the fields the model was woken for. `engine_running` keeps its tri-state
        # (`None` = the liveness probe could not tell) for the same reason `observed_run_states` does.
        observation = {"run": run_id, "phase": row.get("phase"),
                       "finished": bool(row.get("finished")),
                       "engine_running": row.get("engine_running"),
                       "nodes": row.get("nodes"), "best_metric": row.get("best_metric"),
                       "states": sorted(states)}
        if not states.intersection(trigger.get("until") or ()):
            attempts = int(record.get("attempts", 0)) + 1
            return self.store.update(
                record["id"], attempts=attempts, last_observation=observation,
                next_due=now + next_poll_delay(attempts), last_error="")
        return self._wake(record, observation=observation, now=now)

    def _service_absent_run(self, record: dict, run_id, *, now: float):
        """The run is not there — which is TWO facts (module docstring), told apart by `run_seen`."""
        if record.get("run_seen"):
            # GONE: this watch saw the run and now cannot. A condition that can never be met is a
            # terminal with a stated cause, not an eternal poll — and saying so is the difference
            # between a watch the operator can act on and one they find still "waiting" next week.
            return self._retire(
                record, status="failed",
                reason=f"run {run_id} no longer exists, so this condition can never be met")
        # NOT YET: a watch armed just before a launch, which is the natural gesture. Waiting is the
        # right answer and an unbounded wait is not, so the wait carries its own bound and the
        # record states it — an operator reading the strip sees that it is waiting on nothing yet,
        # and when it will stop doing so.
        deadline = float(record.get("created", now) or now) + WATCH_RUN_APPEARANCE_GRACE_S
        if now >= deadline:
            return self._retire(
                record, status="failed",
                reason=(f"run {run_id} never appeared in the "
                        f"{WATCH_RUN_APPEARANCE_GRACE_S / 60:g} minutes after this watch was armed, "
                        f"so it was stopped — check the run id, and re-arm it once the run exists"))
        attempts = int(record.get("attempts", 0)) + 1
        return self.store.update(
            record["id"], attempts=attempts,
            last_observation={"run": run_id, "present": False, "awaiting_first_sight": True,
                              "give_up_at": deadline},
            # Never poll PAST the deadline: the give-up must land when it was promised, not up to a
            # full backoff ceiling later.
            next_due=min(now + next_poll_delay(attempts), deadline),
            last_error=(f"run {run_id} does not exist yet — waiting for it to appear "
                        f"(giving up in {max(0.0, deadline - now) / 60:.0f} min if it does not)"))

    def _retire(self, record: dict, *, status: str, reason: str, observation=None) -> dict:
        """Settle a watch AND leave the operator a line about it. One helper because the two must
        not come apart: every terminal this scheduler decides was silent before, and the silence is
        the defect, not the terminal."""
        fields = {"status": status, "last_error": reason}
        if observation is not None:
            fields["last_observation"] = observation
        settled = self.store.update(record["id"], **fields)
        # `update` refuses to move an ALREADY-terminal record and returns it unchanged, so this is
        # what distinguishes "we retired it" from "the operator's stop got here first" — and only
        # the first of those owes the chat a notice.
        if (settled is not None and settled.get("status") == status
                and settled.get("last_error") == reason):
            self._notice(settled, reason)
        return settled if settled is not None else record

    def _notice(self, record: dict, reason: str) -> None:
        """Say IN THE CHAT that a standing watch stopped, and why.

        The counterpart of `_record_turn`, and deliberately the same surface: monitoring that ends
        somewhere the operator has to go looking for has ended silently. It is NOT a wake-up — no
        model call, no toolset, no turn slot, nothing counted against a budget — so it neither
        touches the wake-up ladder nor gives this module any new authority. `notice: true` on the
        turn is what lets a renderer tell the two apart.
        """
        turn = {
            "role": "assistant",
            "content": (
                f"[standing watch stopped] {reason}\n\n"
                f"It was waiting for: "
                f"{record.get('waiting_for') or describe_trigger(record.get('trigger') or {})}\n"
                f"Its standing instruction was: {str(record.get('instruction') or '')[:400]}"),
            "watch": {"id": record.get("id"), "waiting_for": record.get("waiting_for"),
                      "status": record.get("status"), "notice": True},
            "steps": [], "applied": [], "todos": [],
        }
        try:
            self.append_turn(record.get("session"), turn)
        except Exception:  # noqa: BLE001 - a transcript failure must not lose the watch's state
            pass

    def _wake(self, record: dict, *, observation, now: float):
        """Run ONE turn for this watch, then either re-arm it or retire it.

        The claim is what makes concurrent ticks safe: two threads reaching the same due record
        cannot both spend a model call on it, because only one `armed -> waking` write wins.
        """
        claimed = self.store.claim(record["id"], now=now)
        if claimed is None:
            return None
        # A CLAIM MUST BE SETTLED, including by an escape nobody planned for. Every settling write
        # after this point (`store.update` / `_retire` -> `_write` -> `atomic_write_text`, which has
        # no containment of its own) can raise on a transient OSError on the geesefs mount, and
        # `_loop`'s per-tick containment then swallows it — leaving the record `waking` on disk
        # forever. `due()` returns only `armed` records and `claimed_at` is written and read
        # NOWHERE, so nothing reclaims it: the watch is silently dead monitoring that still counts
        # against the session's active cap until the server restarts. `_unclaim` puts it back the
        # way `WatchDeferred` does — armed, one short interval out, `attempts` untouched, no wake-up
        # counted — because an escape here is evidence about the STORE and not about the condition.
        try:
            return self._wake_claimed(claimed, observation=observation, now=now)
        except WatchDeferred:
            raise
        except BaseException as exc:  # noqa: BLE001 — re-raised; this only settles the claim
            self._unclaim(claimed, now=now, exc=exc)
            raise

    def _unclaim(self, claimed: dict, *, now: float, exc: BaseException) -> None:
        """Return an unsettled claim to `armed`. Never raises — a store that just failed may fail
        again, and losing the ORIGINAL escape to a second one would hide the real cause."""
        try:
            self.store.update(
                claimed["id"], status="armed", next_due=now + WATCH_POLL_BASE_S,
                last_error=f"the wake-up could not be settled ({type(exc).__name__}); re-armed")
        except Exception:  # noqa: BLE001 — best effort; `reconcile_on_start` is the next rung
            pass

    def _wake_claimed(self, claimed: dict, *, observation, now: float):
        """The claimed half of `_wake`. Split out so the claim above has exactly one settling
        guard around everything that can fail, rather than a guard per write."""
        trigger = claimed.get("trigger") or {}
        instruction = wakeup_instruction(claimed, observation)
        try:
            result = self.run_turn_fn(claimed, instruction)
        except WatchDeferred:
            # Put it back exactly as it was, one short interval out. The wake-up is NOT counted and
            # no spend happened, so an operator having a long conversation delays their watch rather
            # than consuming it — and `attempts` is untouched, because deferral is not evidence
            # about the condition and must not ramp the backoff away from it.
            return self.store.update(claimed["id"], status="armed",
                                     next_due=now + WATCH_POLL_BASE_S,
                                     last_error="deferred: the chat was busy with a live turn")
        except Exception as exc:  # noqa: BLE001 - a failed wake-up is reported, never crashed on
            return self._retire(
                claimed, status=("blocked" if trigger.get("kind") == "work" else "failed"),
                observation=observation,
                reason=f"the wake-up turn failed: {type(exc).__name__}")
        self._record_turn(claimed, result, observation)
        wakeups = int(claimed.get("wakeups", 0)) + 1
        if trigger.get("kind") == "work":
            return self._complete_work_cycle(
                claimed, result, observation=observation, wakeups=wakeups, now=now)
        if trigger.get("kind") in ("run_state", "target_status"):
            # A run-state watch is a ONE-SHOT by construction: its condition was met, so re-arming
            # it would wake on the same fact forever. "Watch it again" is a new watch, which is also
            # the only shape under which the operator re-consents to the spend.
            return self.store.update(
                claimed["id"], status="done", wakeups=wakeups, last_observation=observation,
                last_error="")
        if wakeups >= int(claimed.get("max_wakeups", WATCH_DEFAULT_MAX_WAKEUPS)):
            # `wakeups` first, so the record that gets the notice carries the count it retired on.
            counted = self.store.update(claimed["id"], wakeups=wakeups) or claimed
            return self._retire(
                counted, status="expired", observation=observation,
                reason=f"reached its {wakeups}-wake-up budget")
        return self.store.update(
            claimed["id"], status="armed", wakeups=wakeups, last_observation=observation,
            next_due=now + float(trigger.get("every_s") or 300.0), last_error="")

    def _complete_work_cycle(self, record: dict, result, *, observation, wakeups: int, now: float):
        """Apply the cycle's explicit handoff; never infer ``continue`` from an absent one."""
        try:
            checkpoint = normalize_work_checkpoint(
                result.get("work_checkpoint") if isinstance(result, dict) else None)
        except WatchRefusal as exc:
            counted = self.store.update(
                record["id"], wakeups=wakeups, last_observation=observation) or record
            return self._retire(
                counted, status="blocked", observation=observation,
                reason=(f"continuous work stopped after cycle {wakeups}: {exc}; automatic replay "
                        "was refused because the previous cycle's outcome is not safely resumable"))

        counted = self.store.update(
            record["id"], wakeups=wakeups, checkpoint=checkpoint,
            last_observation=observation) or record
        if checkpoint["status"] == "done":
            return self.store.update(
                counted["id"], status="done", last_error="",
                waiting_for="continuous work completed")
        if checkpoint["status"] == "blocked":
            return self._retire(
                counted, status="blocked", observation=observation,
                reason=f"continuous work reported a blocker: {checkpoint['summary'][:300]}")
        if wakeups >= int(counted.get("max_wakeups", WATCH_DEFAULT_MAX_WAKEUPS)):
            return self._retire(
                counted, status="expired", observation=observation,
                reason=(f"continuous work reached its {wakeups}-cycle budget with checkpoint "
                        f"status {checkpoint['status']}"))

        if checkpoint["status"] == "waiting":
            wait = checkpoint["wait"]
            fields = {
                "status": "armed", "attempts": 0,
                "next_due": now + WATCH_POLL_BASE_S, "last_error": "",
                "waiting_for": f"continuous work; {describe_trigger(wait)}",
                "work_wait_seen": False, "work_wait_started": now,
                "work_wait_run_generation": None, "work_wait_attempt": None,
            }
            # Fence the dependency at the HANDOFF, not at the first scheduler poll seconds later.
            # A retry/reset in that gap is exactly the replacement-following race the identity
            # fields exist to prevent. This is still a server read and never a model call; a
            # transient read failure simply leaves first sight to the ordinary bounded poll path.
            if self.observe_target is not None:
                try:
                    first = self.observe_target(wait.get("target") or {})
                except Exception as exc:  # noqa: BLE001 - the next server poll retries this read
                    fields["last_error"] = (
                        f"could not read {describe_trigger(wait)} at handoff: "
                        f"{type(exc).__name__}")
                else:
                    if isinstance(first, dict):
                        fields["last_observation"] = first
                        if first.get("present"):
                            fields["work_wait_seen"] = True
                            fields["work_wait_run_generation"] = first.get("run_generation")
                            fields["work_wait_attempt"] = first.get("attempt")
                        states = frozenset(str(s) for s in (first.get("states") or ()))
                        if first.get("impossible") or states.intersection(wait.get("until") or ()):
                            fields["next_due"] = now
            return self.store.update(
                counted["id"], **fields)
        delay = float(checkpoint.get("next_in_s")
                      or (counted.get("trigger") or {}).get("every_s") or 60.0)
        return self.store.update(
            counted["id"], status="armed", attempts=0, next_due=now + delay,
            waiting_for="continuous work until it reports done or blocked", last_error="",
            work_wait_seen=False, work_wait_started=None,
            work_wait_run_generation=None, work_wait_attempt=None)

    def _record_turn(self, record: dict, result, observation) -> None:
        """Append the wake-up to the SESSION TRANSCRIPT, where the operator already looks.

        This is the "reports progress instead of going dark" property, and it is deliberately the
        ordinary chat surface rather than a second one: monitoring that lands somewhere the operator
        has to go looking for is monitoring they will not read. `watch` on the turn is what lets a
        renderer mark it as machine-initiated — without it, an assistant message nobody asked for
        reads as the chat talking to itself.
        """
        if not isinstance(result, dict):
            return
        reply = str(result.get("reply") or "")
        turn = {
            "role": "assistant",
            "content": reply,
            "watch": {"id": record["id"], "waiting_for": record.get("waiting_for"),
                      "wakeup": int(record.get("wakeups", 0)) + 1,
                      "observation": observation},
            "steps": result.get("steps") or [],
            "applied": result.get("applied") or [],
            "todos": result.get("todos") or [],
        }
        if isinstance(result.get("work_checkpoint"), dict):
            turn["work_checkpoint"] = result["work_checkpoint"]
        if result.get("error_kind"):
            turn["error_kind"] = result["error_kind"]
        # Same rule as an operator-typed turn: a salvage must not read as a conclusion.
        if result.get("budget_exhausted"):
            turn["budget_exhausted"] = result["budget_exhausted"]
        try:
            self.append_turn(record["session"], turn)
        except Exception:  # noqa: BLE001 - a transcript failure must not lose the watch's state
            pass
