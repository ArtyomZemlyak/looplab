"""Append-only event store (I1, ADR-1/17): orjson JSONL, single writer, fsync.

`read_all` tolerates a torn/partial final line (a crash mid-append) by stopping
at the first line without a trailing newline or that fails to parse. This is the
durability contract exercised by the replay-determinism keystone test.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import orjson

from looplab.core.atomicio import best_effort_fsync
from looplab.core.models import Event
from looplab.core.tracing import current_ids


@contextmanager
def _interprocess_lock(lock_path: Path):
    """Best-effort exclusive cross-process lock (msvcrt on Windows, fcntl on POSIX). The live UI
    server appends control events to the SAME events.jsonl the engine subprocess writes; without
    serialization their appends can interleave into a torn line (which `iter_jsonl` truncates at,
    silently dropping later events). Degrades to a no-op if locking is unavailable."""
    f = None
    try:
        f = open(lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass  # lock unavailable -> degrade to the single-writer assumption
        yield
    finally:
        if f is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            f.close()


def iter_jsonl(path: str | os.PathLike) -> Iterator[dict]:
    """Yield dict records from an append-only JSONL file, tolerating a torn/partial final line
    (a crash mid-append): stop at the first line without a trailing newline or that fails to
    parse. Shared by the event store and the span reader so both files have identical
    durability semantics."""
    p = Path(path)
    if not p.exists():
        return
    with open(p, "rb") as f:
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # torn final write — ignore the partial record
            line = raw.strip()
            if not line:
                continue
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                break  # corrupt tail — stop cleanly
            if not isinstance(obj, dict):
                break  # a valid-JSON but non-object line is corruption, not a record
            yield obj


def _parse_jsonl_region(buf: bytes) -> tuple[list[tuple[dict, int]], int]:
    """Parse complete records from a byte buffer, applying `iter_jsonl`'s EXACT durability rules
    (stop at the first torn/blank-then-nonterminated/corrupt line), and report how many bytes were
    consumed (always a newline boundary). Each record is paired with the byte offset consumed
    through the end of its line, so a caller that rejects a record can rewind to the exact
    boundary before it. This is the incremental core shared by the read cache: the set of records
    it yields for a full-file buffer is identical to `iter_jsonl`, so caching can never change
    what `read_all` returns — it only avoids re-reading+re-parsing bytes already seen."""
    out: list[tuple[dict, int]] = []
    consumed = 0
    n = len(buf)
    i = 0
    while i < n:
        nl = buf.find(b"\n", i)
        if nl == -1:
            break  # torn final write (no trailing newline) — leave it for a later top-up
        raw = buf[i:nl]
        line = raw.strip()
        if not line:
            # blank line: iter_jsonl `continue`s over it (it is newline-terminated here)
            i = nl + 1
            consumed = i
            continue
        try:
            obj = orjson.loads(line)
        except orjson.JSONDecodeError:
            break  # corrupt tail — stop cleanly (matches iter_jsonl), don't advance past it
        if not isinstance(obj, dict):
            break  # valid JSON but non-object => corruption, not a record
        i = nl + 1
        consumed = i
        out.append((obj, consumed))
    return out, consumed


class EventStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Incremental read cache (perf): the folded loop calls read_all() many times per iteration,
        # and each call used to re-read + re-parse the WHOLE log (O(events) IO+orjson+Event() every
        # time => O(events^2) per run, plus the mid-eval abort watcher re-scanning every 0.3s). The
        # cache keeps already-parsed Events and only reads the bytes appended since the last call, so
        # read_all() is amortized O(new events). `_cache_bytes` always ends on a newline boundary.
        self._cache: list[Event] = []
        self._cache_bytes: int = 0
        # The abort watcher (and, under max_parallel>1, several concurrent watchers) call read_all()
        # from worker THREADS while the main loop may also read — guard the cache top-up so a
        # concurrent extend/offset update can't race into a corrupt cache.
        self._read_lock = threading.Lock()
        self._seq = self._scan_last_seq()

    def _scan_last_seq(self) -> int:
        last = -1
        for e in self.read_all():
            last = e.seq
        return last

    def _disk_last_seq(self) -> int:
        """Last seq currently on disk, read cheaply from the file TAIL (O(1), not O(events)). Used
        under the append lock so a concurrent writer's (the UI server's) events are accounted for —
        keeps seq monotonic across two processes without rescanning the whole log each append."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                if end == 0:
                    return -1
                size = min(65536, end)
                f.seek(end - size)
                tail = f.read(size)
        except OSError:
            return -1
        for raw in reversed([l for l in tail.split(b"\n") if l.strip()]):
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "seq" in obj:
                return int(obj["seq"])
        # Tail window missed the last seq (e.g. a >64KB final line with no newline in the window):
        # fall back to a full scan so a concurrent writer can't mint a duplicate seq. Non-recursive.
        return self._scan_last_seq()

    def _heal_torn_tail(self) -> None:
        """A final line without a trailing newline is always a torn write (append writes `line +
        b"\\n"` atomically-per-record). `iter_jsonl` already ignores that partial record on read, but
        the NEXT append would glue its bytes onto the partial line, producing one unparseable merged
        line at which every reader stops — silently dropping this event and all events after it.
        Truncate the torn partial line before appending so new records stay readable. No-op when the
        file is absent, empty, or already newline-terminated. Called under the append lock."""
        try:
            with open(self.path, "r+b") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                if end == 0:
                    return
                f.seek(end - 1)
                if f.read(1) == b"\n":
                    return
                pos, newline_at = end, -1
                while pos > 0:
                    start = max(0, pos - 65536)
                    f.seek(start)
                    buf = f.read(pos - start)
                    idx = buf.rfind(b"\n")
                    if idx != -1:
                        newline_at = start + idx
                        break
                    pos = start
                f.truncate(newline_at + 1 if newline_at != -1 else 0)
        except OSError:
            pass  # best-effort healing; a read still tolerates the torn tail

    def append(self, type: str, data: dict[str, Any]) -> Event:
        trace_id, span_id = current_ids()
        with _interprocess_lock(Path(str(self.path) + ".lock")):
            self._heal_torn_tail()
            # Derive seq from max(in-memory, on-disk tail) so a concurrent writer can't collide.
            # Single-process: _disk_last_seq == self._seq, so seq == self._seq + 1 (unchanged).
            seq = max(self._seq, self._disk_last_seq()) + 1
            e = Event(seq=seq, ts=time.time(), type=type, data=data,
                      trace_id=trace_id, span_id=span_id)
            line = orjson.dumps(e.model_dump(mode="json"))
            with open(self.path, "ab") as f:    # advance _seq only AFTER a durable write succeeds
                f.write(line + b"\n")
                f.flush()
                best_effort_fsync(f.fileno())   # FUSE/S3 fsync may raise/throttle — must not abort the
                #                                 engine mid-run (read tolerates a torn final line)
            self._seq = seq
        return e

    def read_all(self) -> Iterator[Event]:
        """Yield every Event on disk (up to the first torn/corrupt line), served from an incremental
        cache. Only bytes appended since the previous call are read+parsed; the returned sequence is
        byte-for-byte identical to a full `iter_jsonl` scan. Falls back to a full rescan if the file
        shrank/was replaced (a heal-truncate or a fresh file) so the cache can never go stale."""
        with self._read_lock:
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
            except OSError:
                size = 0
            if size < self._cache_bytes:
                # File shrank/was replaced (heal-truncate / new run at same path) — rebuild from zero.
                self._cache = []
                self._cache_bytes = 0
            if size > self._cache_bytes:
                try:
                    with open(self.path, "rb") as f:
                        f.seek(self._cache_bytes)
                        new = f.read(size - self._cache_bytes)
                except OSError:
                    new = b""
                objs, consumed = _parse_jsonl_region(new)
                # Materialize BEFORE mutating the cache, and tolerate a record that is valid JSON
                # but not a valid Event (foreign writer / version skew): stop at the byte boundary
                # BEFORE it, like a torn line, instead of half-extending the cache mid-generator
                # and re-appending the same prefix on every later call.
                evs: list[Event] = []
                ok_bytes = 0
                for o, end in objs:
                    try:
                        evs.append(Event(**o))
                    except Exception:  # noqa: BLE001
                        break
                    ok_bytes = end
                else:
                    ok_bytes = consumed   # all records valid — trailing blanks count too
                self._cache.extend(evs)
                self._cache_bytes += ok_bytes
            # Return a snapshot copy so a caller iterating the result can't be perturbed by a
            # concurrent top-up (and so callers expecting a re-iterable list still work).
            return list(self._cache)
