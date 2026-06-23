"""Append-only event store (I1, ADR-1/17): orjson JSONL, single writer, fsync.

`read_all` tolerates a torn/partial final line (a crash mid-append) by stopping
at the first line without a trailing newline or that fails to parse. This is the
durability contract exercised by the replay-determinism keystone test.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator

import orjson

from .models import Event
from .tracing import current_ids


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

    def append(self, type: str, data: dict[str, Any]) -> Event:
        seq = self._seq + 1                 # advance only AFTER a durable write succeeds, so a
        trace_id, span_id = current_ids()   # failed dumps/write/fsync can't leave a seq gap
        e = Event(seq=seq, ts=time.time(), type=type, data=data,
                  trace_id=trace_id, span_id=span_id)
        line = orjson.dumps(e.model_dump(mode="json"))
        with open(self.path, "ab") as f:
            f.write(line + b"\n")
            f.flush()
            os.fsync(f.fileno())
        self._seq = seq
        return e

    def read_all(self) -> Iterator[Event]:
        for obj in iter_jsonl(self.path):
            yield Event(**obj)
