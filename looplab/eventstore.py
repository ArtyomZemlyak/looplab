"""Append-only event store (I1, ADR-1/17): orjson JSONL, single writer, fsync.

`read_all` tolerates a torn/partial final line (a crash mid-append) by stopping
at the first line without a trailing newline or that fails to parse. This is the
durability contract exercised by the replay-determinism keystone test.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import orjson

from .models import Event
from .tracing import current_ids


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


class EventStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def append(self, type: str, data: dict[str, Any]) -> Event:
        trace_id, span_id = current_ids()
        with _interprocess_lock(Path(str(self.path) + ".lock")):
            # Derive seq from max(in-memory, on-disk tail) so a concurrent writer can't collide.
            # Single-process: _disk_last_seq == self._seq, so seq == self._seq + 1 (unchanged).
            seq = max(self._seq, self._disk_last_seq()) + 1
            e = Event(seq=seq, ts=time.time(), type=type, data=data,
                      trace_id=trace_id, span_id=span_id)
            line = orjson.dumps(e.model_dump(mode="json"))
            with open(self.path, "ab") as f:    # advance _seq only AFTER a durable write succeeds
                f.write(line + b"\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq = seq
        return e

    def read_all(self) -> Iterator[Event]:
        for obj in iter_jsonl(self.path):
            yield Event(**obj)
