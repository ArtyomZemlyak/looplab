"""Bounded, process-local observations of authenticated external-agent requests.

This is request activity, never an agent/transport lease or proof of life. No
heartbeat thread, file write, engine gate or automatic recovery follows from age.
Server restart/eviction loses the observation; run generation fences resets.

WHAT COUNTS (review 2026-10-14 section 2): every SUCCESSFUL request a run's API answered for the harness
credential — reads (`GET /state`, `/harness-progress`, `/harness-checkpoints`, …) and writes
(`POST /commands`, `/harness-reviews`, `/harness-decisions`, a checkpoint answer, …). It used to be
only the two progress/checkpoint GETs, so an agent that was busy publishing a report through POSTs
read as "No recent agent reads". The auth middleware (`serve/server.py`) observes; owner/browser
polling never does, and a refused request (4xx/5xx — a stale generation's 409 included) never does.

WHEN IT IS A WARNING: silence alone is normal — six quiet minutes while training runs with no open
question is the expected state. `snapshot(waiting=…)` carries what LoopLab is actually waiting for
from the agent (an unanswered evaluation question, or the next-experiment decision of an idle
running engine); `attention == "waiting_on_agent"` only when such a wait exists AND the agent has
been quiet for `QUIET_AFTER_SECONDS` (or was never observed). `status` keeps its historical three
values so an older UI keeps reading the same field.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import threading
import time

QUIET_AFTER_SECONDS = 120
MAX_RUNS = 256
# What LoopLab may be waiting for from the agent. Anything else is "nothing required".
WAITING_REASONS = ("evaluation_question", "next_experiment")


def waiting_for_agent(progress: dict) -> dict | None:
    """What the harness-progress read says LoopLab is waiting for from the agent, or None.

    An unanswered checkpoint blocks an evaluation; a running engine whose next step is
    `choose_direction` idles until the agent submits or finishes. A paused/finished run, pending
    evaluations, or an incomplete read wait on nobody the chip can name."""
    try:
        count = int(progress.get("pending_checkpoint_count") or 0)
        if count > 0:
            return {"reason": "evaluation_question", "count": count}
        step = progress.get("next_step") or {}
        if (step.get("code") == "choose_direction"
                and (progress.get("execution") or {}).get("engine_running") is True):
            return {"reason": "next_experiment", "count": 1}
    except (AttributeError, TypeError, ValueError):
        return None
    return None


class AgentActivity:
    def __init__(self, *, clock=time.monotonic, timestamp=None):
        self._clock = clock
        self._timestamp = timestamp or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._reads = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, rd, generation, *, read_kind="harness-progress", write=False):
        key = str(rd), generation
        with self._lock:
            self._reads[key] = self._clock(), self._timestamp(), read_kind, bool(write)
            self._reads.move_to_end(key)
            while len(self._reads) > MAX_RUNS:
                self._reads.popitem(last=False)

    def observe_authored(self, rd, generation, kind):
        """An AUTHORED action of the harness credential (a command, a checkpoint answer, a
        decision/review) — kept apart from reads, so a bot that only polls cannot make the agent
        look alive (doc 78 §4). Rule and engine answers never reach here: LoopLab writes them."""
        key = str(rd), generation
        with self._lock:
            authored = getattr(self, "_authored", None)
            if authored is None:
                authored = self._authored = OrderedDict()
            authored[key] = self._clock(), self._timestamp(), str(kind)[:40]
            authored.move_to_end(key)
            while len(authored) > MAX_RUNS:
                authored.popitem(last=False)

    def snapshot(self, rd, generation, *, waiting=None):
        with self._lock:
            last = self._reads.get((str(rd), generation))
            age = max(0, int(self._clock() - last[0])) if last else None
            wrote = (getattr(self, "_authored", None) or {}).get((str(rd), generation))
            wrote_age = max(0, int(self._clock() - wrote[0])) if wrote else None
        quiet = last is None or age >= QUIET_AFTER_SECONDS
        waiting = waiting if isinstance(waiting, dict) and waiting.get("reason") in WAITING_REASONS else None
        return {"status": ("not_observed" if last is None else
                           "quiet" if age >= QUIET_AFTER_SECONDS else "recent_request"),
                "last_seen_at": last[1] if last else None, "age_seconds": age,
                "quiet_after_s": QUIET_AFTER_SECONDS,
                "source": ("authenticated_harness_progress_read" if last is None or last[2] == "harness-progress"
                           else "authenticated_harness_read"),
                "read_kind": last[2] if last else None,
                "request_kind": None if last is None else ("write" if last[3] else "read"),
                "waiting_for": waiting,
                "attention": ("waiting_on_agent" if waiting and quiet else
                              "agent_has_work" if waiting else "nothing_required"),
                "scope": "this_ui_process",
                # The last AUTHORED action, separate from reads (doc 78 §4).
                "authored_status": ("not_observed" if wrote is None else
                                    "quiet" if wrote_age >= QUIET_AFTER_SECONDS else "recent_action"),
                "last_authored_at": wrote[1] if wrote else None,
                "authored_age_seconds": wrote_age,
                "authored_kind": wrote[2] if wrote else None}
