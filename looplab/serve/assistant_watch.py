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
WATCH_TRIGGER_KINDS = ("run_state", "schedule")

# What a `run_state` watch may wait for. The eight run PHASES the read model already publishes, plus
# ONE derived state the phase vocabulary cannot express: `engine_stopped` means no engine process
# holds the run's lock. That is the difference between "the run is finished" and "the run is not
# being worked on", and an operator who says "tell me when it stops" usually means the second — a
# crashed engine leaves the phase at `search` forever.
WATCH_RUN_STATES = (
    PHASE_FINISHED, PHASE_FINALIZING, PHASE_PAUSED, PHASE_APPROVAL, PHASE_SPEC_APPROVAL,
    PHASE_ONBOARDING, PHASE_GROUNDING, PHASE_SEARCH, "engine_stopped",
)

# `armed` -> `waking` -> (`armed` again for a schedule | a terminal). Terminals are final: nothing
# in this module transitions out of one, so a resolved watch cannot be resurrected by a stale poll.
WATCH_STATUSES = ("armed", "waking", "done", "cancelled", "expired", "failed", "interrupted")
WATCH_TERMINAL_STATUSES = frozenset({"done", "cancelled", "expired", "failed", "interrupted"})

# ---- the floor under "effectively infinite" (doc 36: a budget plus a judgment, never neither) ----
WATCH_MIN_INTERVAL_S = 15.0          # below this a "monitor every N" is a tight poll wearing a name
WATCH_MAX_INTERVAL_S = 24 * 60 * 60.0
WATCH_POLL_BASE_S = 5.0              # first re-check of an unmet run-state condition
WATCH_POLL_CEILING_S = 60.0          # …ramping to here, and no further
WATCH_POLL_RAMP = 1.6
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


def observed_run_states(row: Optional[dict], *, engine_running: Optional[bool] = None) -> frozenset:
    """Which of `WATCH_RUN_STATES` a run is in RIGHT NOW, from the server's own read model.

    The trigger's whole evidentiary basis, and deliberately a projection of `run_summaries`' folded
    row rather than anything the assistant produced — see the module docstring's first property.
    An absent row (deleted run, unreadable log) is the empty set, which is not the same as "not yet":
    `WatchService` treats a run that has vanished as a reason to stop watching, with a stated cause,
    rather than as a condition that will eventually be met.
    """
    if not isinstance(row, dict):
        return frozenset()
    states = set()
    phase = row.get("phase")
    if phase in WATCH_RUN_STATES:
        states.add(phase)
    if engine_running is None:
        engine_running = bool(row.get("engine_running"))
    if not engine_running:
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

    # ---- paths / io -----------------------------------------------------------------------
    def _path(self, watch_id: str) -> Path:
        if not isinstance(watch_id, str) or WATCH_ID_RE.fullmatch(watch_id) is None:
            raise WatchRefusal("bad watch id")
        return self.dir / f"{watch_id}.json"

    def _read(self, watch_id: str) -> Optional[dict]:
        try:
            record = json.loads(self._path(watch_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, WatchRefusal):
            return None
        return record if self._valid(record) else None

    def _write(self, record: dict) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {**record, "updated": time.time()}
        atomic_write_text(self._path(record["id"]), json.dumps(record))
        return record

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
        ts = time.time() if now is None else float(now)
        wakeups_cap = int(_bounded_float(
            max_wakeups, low=1, high=WATCH_MAX_WAKEUPS_CEILING,
            default=WATCH_DEFAULT_MAX_WAKEUPS, what="max_wakeups"))
        lifetime = _bounded_float(lifetime_s, low=WATCH_MIN_INTERVAL_S, high=WATCH_MAX_LIFETIME_S,
                                  default=WATCH_DEFAULT_LIFETIME_S, what="lifetime_s")
        active = [w for w in self.list(session=session) if w["status"] not in WATCH_TERMINAL_STATUSES]
        if len(active) >= WATCH_MAX_ACTIVE_PER_SESSION:
            raise WatchRefusal(
                f"this chat already has {len(active)} active watches (max "
                f"{WATCH_MAX_ACTIVE_PER_SESSION}); stop one before arming another")
        record = {
            "id": secrets.token_hex(8),
            "session": str(session),
            "mode": normalize_mode(mode),
            "instruction": instruction,
            "trigger": trigger,
            "status": "armed",
            "created": ts,
            "updated": ts,
            # The first check of a `run_state` watch is IMMEDIATE (the condition may already hold —
            # "tell me when run X finishes" about a run that finished an hour ago must answer now,
            # not in five seconds). A schedule's first wake-up is one interval away, because "every
            # N minutes" starting instantly would fire twice for the operator's first interval.
            "next_due": ts if trigger["kind"] == "run_state" else ts + trigger["every_s"],
            "attempts": 0,
            "wakeups": 0,
            "max_wakeups": wakeups_cap,
            "expires_at": ts + lifetime,
            "waiting_for": (str(waiting_for or "").strip() or describe_trigger(trigger))[:300],
            "last_observation": None,
            "last_error": "",
        }
        with self._lock:
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

    def list(self, *, session: Optional[str] = None, active_only: bool = False) -> list[dict]:
        try:
            entries = sorted(self.dir.iterdir())
        except OSError:
            return []
        out = []
        for path in entries:
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

    def due(self, *, now: Optional[float] = None) -> list[dict]:
        """Armed watches whose next check has come round, oldest-due first (fair under a cap)."""
        ts = time.time() if now is None else float(now)
        ready = [r for r in self.list()
                 if r.get("status") == "armed" and float(r.get("next_due", 0) or 0) <= ts]
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
        with self._lock:
            record = self._read(watch_id)
            if record is None:
                return None
            if record["status"] in WATCH_TERMINAL_STATUSES:
                return record
            return self._write({**record, "status": "cancelled", "last_error": str(reason)[:300]})

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


def wakeup_instruction(record: dict, observation) -> str:
    """The full model-facing instruction for one wake-up — the record's own sentence, in context."""
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
                 append_turn: Callable, interval_s: float = 2.0, on_error: Optional[Callable] = None):
        self.store = store
        self.observe_run = observe_run          # run_id -> the read model's row (or None)
        self.run_turn_fn = run_turn_fn          # (record, instruction) -> the turn result dict
        self.append_turn = append_turn          # (session, turn dict) -> None
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
        """
        changed = self.store.reconcile_on_start()
        if self.store.list(active_only=True):
            self.ensure_started()
        return changed

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
                touched.append(self.store.update(
                    record["id"], status="expired",
                    last_error="the watch reached its lifetime without being resolved") or record)
                continue
            touched.append(self._service(record, now=ts))
        return [r for r in touched if r is not None]

    def _service(self, record: dict, *, now: float):
        trigger = record.get("trigger") or {}
        if trigger.get("kind") == "run_state":
            return self._service_run_state(record, trigger, now=now)
        return self._wake(record, observation={"reason": "scheduled", "at": now}, now=now)

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
            # The run is GONE (deleted, or never existed). A condition that can never be met is a
            # terminal with a stated cause, not an eternal poll — and saying so is the difference
            # between a watch the operator can act on and one they find still "waiting" next week.
            return self.store.update(
                record["id"], status="failed",
                last_error=f"run {run_id} no longer exists, so this condition can never be met")
        states = observed_run_states(row)
        observation = {"run": run_id, "phase": row.get("phase"),
                       "finished": bool(row.get("finished")),
                       "engine_running": bool(row.get("engine_running")),
                       "nodes": row.get("nodes"), "best_metric": row.get("best_metric"),
                       "states": sorted(states)}
        if not states.intersection(trigger.get("until") or ()):
            attempts = int(record.get("attempts", 0)) + 1
            return self.store.update(
                record["id"], attempts=attempts, last_observation=observation,
                next_due=now + next_poll_delay(attempts), last_error="")
        return self._wake(record, observation=observation, now=now)

    def _wake(self, record: dict, *, observation, now: float):
        """Run ONE turn for this watch, then either re-arm it or retire it.

        The claim is what makes concurrent ticks safe: two threads reaching the same due record
        cannot both spend a model call on it, because only one `armed -> waking` write wins.
        """
        claimed = self.store.claim(record["id"], now=now)
        if claimed is None:
            return None
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
            return self.store.update(
                claimed["id"], status="failed", last_observation=observation,
                last_error=f"the wake-up turn failed: {type(exc).__name__}")
        self._record_turn(claimed, result, observation)
        wakeups = int(claimed.get("wakeups", 0)) + 1
        trigger = claimed.get("trigger") or {}
        if trigger.get("kind") == "run_state":
            # A run-state watch is a ONE-SHOT by construction: its condition was met, so re-arming
            # it would wake on the same fact forever. "Watch it again" is a new watch, which is also
            # the only shape under which the operator re-consents to the spend.
            return self.store.update(
                claimed["id"], status="done", wakeups=wakeups, last_observation=observation,
                last_error="")
        if wakeups >= int(claimed.get("max_wakeups", WATCH_DEFAULT_MAX_WAKEUPS)):
            return self.store.update(
                claimed["id"], status="expired", wakeups=wakeups, last_observation=observation,
                last_error=f"reached its {wakeups}-wake-up budget")
        return self.store.update(
            claimed["id"], status="armed", wakeups=wakeups, last_observation=observation,
            next_due=now + float(trigger.get("every_s") or 300.0), last_error="")

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
        if result.get("error_kind"):
            turn["error_kind"] = result["error_kind"]
        # Same rule as an operator-typed turn: a salvage must not read as a conclusion.
        if result.get("budget_exhausted"):
            turn["budget_exhausted"] = result["budget_exhausted"]
        try:
            self.append_turn(record["session"], turn)
        except Exception:  # noqa: BLE001 - a transcript failure must not lose the watch's state
            pass
