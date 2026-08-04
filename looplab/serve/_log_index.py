"""The scaffolding both incremental event-log indexes share (doc 25 SC-04).

`serve/command_observation.py` (intents/acks for the command workers) and `serve/log_pages.py`
(byte-offset rows for the timeline) are two indexes over the SAME append-only file, built the same
way: resume a scan from the last valid byte boundary, fence a rewrite/shrink, keep a small LRU of
active runs, and serialize the expensive per-path work without serializing every other run behind it.

Their PAYLOADS differ enough that a shared scanner would be an abstraction over two unrelated row
grammars, so `_Index`/`_scan` stay per-module. What lives here is the machinery that was literally
copy-pasted — `PathLocks` was byte-identical between the two files, docstring included — plus the
one shared constructor guard, so a fix to either reaches both.
"""
from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class LogIndexCursor:
    """Where an incremental index has read to, and the fences that decide whether it may resume.

    Both indexes carried these five fields and both updated them the same way at the end of a scan
    (doc 25 SC-04). The PAYLOAD each accumulates is genuinely different — decoded events with a
    dense-seq rule vs byte-offset rows with a monotonic-seq rule, one probing content sentinels and
    the other anchoring a prefix — so `_Index` and `_scan` stay per-module and subclass this.

    `revision` must identify one concrete cached file revision, not merely the run's stable
    first-event generation. Atomic repairs may deliberately preserve that first event while replacing
    every later row. A random per-index epoch also fails old cursors closed after LRU eviction or a
    server restart; the client recovers through an exact-generation tail read.
    """

    identity: tuple[int, int]
    metadata: tuple[int, int]
    revision: str = field(default_factory=lambda: secrets.token_hex(16))
    observed_size: int = 0
    valid_end: int = 0
    torn_tail: bool = False

    def mint_revision(self) -> None:
        """Rotate the revision, failing every outstanding cursor closed."""
        self.revision = secrets.token_hex(16)

    def note_scanned(self, valid_end: int, snapshot_size: int, torn: bool) -> None:
        """Record where the scan stopped and how far the file was read.

        All three together: a `valid_end` advanced without its `observed_size` would let the next
        poll skip the bytes between them, and a `torn_tail` left stale would stop the re-scan that a
        writer filling reserved bytes in place depends on.
        """
        self.valid_end = valid_end
        self.observed_size = snapshot_size
        self.torn_tail = torn


class PathLocks:
    """Per-path mutual exclusion for the incremental byte indexes.

    Both indexes used ONE process-global lock held across a whole request, so the first full scan of
    a multi-MB log — or a cold network read — stalled every OTHER run's poll behind it. The work is
    already per-path (each `_Index` is scanned and read independently), so the exclusion should be
    too; only the registry map itself needs a shared lock, and that is held for dictionary
    operations alone.

    A lock is evicted only while UNREFERENCED. Handing a second lock object out for a path whose
    first is still held would let two threads scan and mutate the same `_Index` concurrently, which
    is exactly the corruption the lock exists to prevent — so the LRU bound yields to liveness.
    """

    __slots__ = ("_locks", "_registry", "_maximum")

    def __init__(self, maximum: int):
        self._locks: "OrderedDict[str, list]" = OrderedDict()   # key -> [RLock, waiters]
        self._registry = threading.RLock()
        self._maximum = max(1, int(maximum))

    @contextmanager
    def hold(self, key: str):
        with self._registry:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [threading.RLock(), 0]
            entry[1] += 1
            self._locks.move_to_end(key)
        try:
            with entry[0]:
                yield
        finally:
            with self._registry:
                entry[1] -= 1
                excess = len(self._locks) - self._maximum
                if excess > 0:
                    for stale in [k for k, e in self._locks.items() if e[1] == 0][:excess]:
                        self._locks.pop(stale, None)


def validated_index_bound(max_indexed_runs: object) -> int:
    """The LRU bound both index constructors accept, rejected identically by both.

    `bool` is excluded explicitly because it is an `int` subclass: `EventLogPager(max_indexed_runs=
    True)` would otherwise build a one-entry cache that silently evicts every run but the newest.
    """
    if (not isinstance(max_indexed_runs, int) or isinstance(max_indexed_runs, bool)
            or max_indexed_runs < 1):
        raise ValueError("max_indexed_runs must be a positive integer")
    return max_indexed_runs
