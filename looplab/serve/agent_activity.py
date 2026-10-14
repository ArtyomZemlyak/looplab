"""Bounded, process-local observations of authenticated external workflow reads.

This is request activity, never an agent/transport lease or proof of life. No
heartbeat thread, file write, engine gate or automatic recovery follows from age.
Server restart/eviction loses the observation; run generation fences resets.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import threading
import time

QUIET_AFTER_SECONDS = 120
MAX_RUNS = 256


class AgentActivity:
    def __init__(self, *, clock=time.monotonic, timestamp=None):
        self._clock = clock
        self._timestamp = timestamp or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._reads = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, rd, generation, *, read_kind="harness-progress"):
        key = str(rd), generation
        with self._lock:
            self._reads[key] = self._clock(), self._timestamp(), read_kind
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

    def snapshot(self, rd, generation):
        with self._lock:
            last = self._reads.get((str(rd), generation))
            age = max(0, int(self._clock() - last[0])) if last else None
            wrote = (getattr(self, "_authored", None) or {}).get((str(rd), generation))
            wrote_age = max(0, int(self._clock() - wrote[0])) if wrote else None
        return {"status": ("not_observed" if last is None else
                           "quiet" if age >= QUIET_AFTER_SECONDS else "recent_request"),
                "last_seen_at": last[1] if last else None, "age_seconds": age,
                "quiet_after_s": QUIET_AFTER_SECONDS,
                "source": ("authenticated_harness_progress_read" if last is None or last[2] == "harness-progress"
                           else "authenticated_harness_read"),
                "read_kind": last[2] if last else None, "scope": "this_ui_process",
                # The last AUTHORED action, separate from reads (doc 78 §4).
                "authored_status": ("not_observed" if wrote is None else
                                    "quiet" if wrote_age >= QUIET_AFTER_SECONDS else "recent_action"),
                "last_authored_at": wrote[1] if wrote else None,
                "authored_age_seconds": wrote_age,
                "authored_kind": wrote[2] if wrote else None}
