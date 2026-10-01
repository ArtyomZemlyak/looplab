"""Bounded, process-local observations of authenticated external progress reads.

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

    def observe(self, rd, generation):
        key = str(rd), generation
        with self._lock:
            self._reads[key] = self._clock(), self._timestamp()
            self._reads.move_to_end(key)
            while len(self._reads) > MAX_RUNS:
                self._reads.popitem(last=False)

    def snapshot(self, rd, generation):
        with self._lock:
            last = self._reads.get((str(rd), generation))
            age = max(0, int(self._clock() - last[0])) if last else None
        return {"status": ("not_observed" if last is None else
                           "quiet" if age >= QUIET_AFTER_SECONDS else "recent_request"),
                "last_seen_at": last[1] if last else None, "age_seconds": age,
                "quiet_after_s": QUIET_AFTER_SECONDS,
                "source": "authenticated_harness_progress_read", "scope": "this_ui_process"}
