"""Bounded, generation-fenced pages over a run's append-only event log.

The legacy ``/log`` route intentionally returns the whole recoverable JSONL prefix.  This pager is
the scalable transport used by the timeline: it indexes byte boundaries once, extends that index
incrementally as the file grows, and re-reads only the selected rows for each response.  Cursors
name a row boundary, the durable run generation, and one concrete cached index revision. Append keeps
that revision valid; replacement, rewrite, shrink, cache eviction, or restart fails closed instead of
splicing two different file snapshots into one browser timeline.
"""
from __future__ import annotations

import base64
import bisect
import json
import math
import os
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

import orjson
from fastapi import HTTPException

from looplab.core.models import Event
from looplab.serve.run_commands import run_generation_token


DEFAULT_ROWS = 200
MAX_ROWS = 500
DEFAULT_BYTES = 256 * 1024
MAX_BYTES = 512 * 1024
MIN_BYTES = 1024
MAX_INDEXED_RUNS = 8
# Bound parser memory independently of the response byte budget. Canonical event rows above this
# hard ceiling end the recoverable page prefix (and are reported as a source-limited torn tail).
MAX_SOURCE_ROW_BYTES = 8 * 1024 * 1024
MAX_PLACEHOLDER_TYPE_CHARS = 256

_GENERATION_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{32}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_DIRECTIONS = frozenset({"tail", "older", "newer", "around"})


@dataclass(frozen=True)
class _Row:
    start: int
    end: int
    seq: int
    event_type: Optional[str]
    ts: object

    @property
    def raw_bytes(self) -> int:
        return self.end - self.start


@dataclass
class _Index:
    identity: tuple[int, int]
    generation: Optional[str]
    metadata: tuple[int, int]
    # A cursor must identify this concrete cached file revision, not merely the run's stable
    # first-event generation. Atomic repairs may deliberately preserve that first event while
    # replacing every later row. A random per-index epoch also fails old cursors closed after LRU
    # eviction or a server restart; the client recovers through an exact-generation tail read.
    revision: str = field(default_factory=lambda: secrets.token_hex(16))
    observed_size: int = 0
    valid_end: int = 0
    torn_tail: bool = False
    source_tail_limited: bool = False
    rows: list[_Row] = field(default_factory=list)
    # Cached separately from rows so an 11fps scrubber jump is O(log n), not an O(n) list rebuild.
    seq_values: list[int] = field(default_factory=list)
    seq_row_indexes: list[int] = field(default_factory=list)


def _generation_for(first: object) -> Optional[str]:
    if not isinstance(first, dict):
        return None
    seq = first.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None
    try:
        event = Event(**first)
    except Exception:  # noqa: BLE001 - malformed first row means no safe generation fence
        return None
    token = run_generation_token([event])
    return token or None


def _first_generation(handle: BinaryIO, size: int) -> Optional[str]:
    """Read only through the first non-blank complete line in this file snapshot."""
    handle.seek(0)
    while handle.tell() < size:
        remaining = size - handle.tell()
        raw = handle.readline(min(remaining, MAX_SOURCE_ROW_BYTES + 1))
        if (not raw or len(raw) > MAX_SOURCE_ROW_BYTES or not raw.endswith(b"\n")):
            return None
        line = raw.strip()
        if not line:
            continue
        try:
            return _generation_for(orjson.loads(line))
        except orjson.JSONDecodeError:
            return None
    return None


def _metadata_signature(stat: os.stat_result) -> tuple[int, int]:
    """Metadata used only to distinguish same-size in-place rewrites from an unchanged snapshot.

    Size growth is handled as the normal append path and deliberately keeps the cursor epoch. A
    changed timestamp at the same size is ambiguous, so fail closed and rebuild the index.
    """
    return stat.st_mtime_ns, stat.st_ctime_ns


def _row_from(raw: bytes, start: int, end: int) -> Optional[_Row]:
    line = raw.strip()
    if not line:
        return None
    try:
        obj = orjson.loads(line)
    except orjson.JSONDecodeError as exc:
        raise ValueError("invalid JSONL tail") from exc
    if not isinstance(obj, dict):
        raise ValueError("non-object JSONL tail")
    try:
        event = Event(**obj)
    except Exception as exc:  # noqa: BLE001 - parity with EventStore.read_all's recoverable prefix
        raise ValueError("invalid event envelope") from exc
    seq = obj.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("event seq must be an explicit nonnegative integer")
    # Only placeholders retain these fields. Normalize through Event and cap the stored value so a
    # hostile giant type string cannot dominate the index or break the byte cap. Keep it ordinary:
    # interning attacker-controlled unique values would outlive this pager's bounded run LRU.
    event_type = event.type[:MAX_PLACEHOLDER_TYPE_CHARS]
    ts = event.ts if math.isfinite(event.ts) else 0.0
    return _Row(start=start, end=end, seq=seq, event_type=event_type, ts=ts)


def _scan(handle: BinaryIO, index: _Index, snapshot_size: int) -> None:
    """Extend ``index`` to ``snapshot_size`` with the same stop-at-first-bad contract as iter_jsonl."""
    handle.seek(index.valid_end)
    torn = False
    source_limited = False
    while handle.tell() < snapshot_size:
        start = handle.tell()
        remaining = snapshot_size - start
        raw = handle.readline(min(remaining, MAX_SOURCE_ROW_BYTES + 1))
        if len(raw) > MAX_SOURCE_ROW_BYTES:
            source_limited = True
            torn = True
            break
        if not raw or not raw.endswith(b"\n"):
            torn = bool(raw)
            break
        end = handle.tell()
        try:
            row = _row_from(raw, start, end)
        except ValueError:
            torn = True
            break
        # Blank complete lines are skipped by iter_jsonl but remain part of the durable valid prefix.
        if row is not None:
            # EventStore's writer allocates a strictly increasing seq. Gaps are legitimate after an
            # atomic rewrite, but duplicate/decreasing seq would make client identity ambiguous and
            # therefore ends the canonical timeline prefix just like a malformed envelope.
            if index.seq_values and row.seq <= index.seq_values[-1]:
                torn = True
                break
            index.valid_end = end
            row_index = len(index.rows)
            index.rows.append(row)
            index.seq_values.append(row.seq)
            index.seq_row_indexes.append(row_index)
        else:
            index.valid_end = end
    index.observed_size = snapshot_size
    index.torn_tail = torn
    index.source_tail_limited = source_limited


def _encode_cursor(generation: str, revision: str, boundary: int) -> str:
    raw = json.dumps({"g": generation, "i": boundary, "r": revision, "v": 2},
                     sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(value: object) -> tuple[str, str, int]:
    if not isinstance(value, str) or _CURSOR_RE.fullmatch(value) is None:
        raise HTTPException(400, {"code": "invalid_log_cursor", "message": "cursor is malformed"})
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            400, {"code": "invalid_log_cursor", "message": "cursor is malformed"}) from exc
    generation = payload.get("g") if isinstance(payload, dict) else None
    revision = payload.get("r") if isinstance(payload, dict) else None
    boundary = payload.get("i") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("v") != 2
            or not isinstance(generation, str) or _GENERATION_RE.fullmatch(generation) is None
            or not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None
            or not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0
            or _encode_cursor(generation.lower(), revision, boundary) != value):
        raise HTTPException(400, {"code": "invalid_log_cursor", "message": "cursor is malformed"})
    return generation.lower(), revision, boundary


def _normalize_generation(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or _GENERATION_RE.fullmatch(value) is None:
        raise HTTPException(400, {
            "code": "invalid_run_generation",
            "message": "generation must be an exact 64-character hexadecimal string",
        })
    return value.lower()


def _generation_changed(expected: Optional[str], actual: Optional[str]) -> HTTPException:
    return HTTPException(409, {
        "code": "run_generation_changed",
        "message": "The event log was reset or replaced; discard timeline cursors and load a new tail.",
        "expected_generation": expected,
        "actual_generation": actual,
        "remediation": "Request direction=tail without a cursor, then replace the local timeline.",
    })


def _placeholder(row: _Row) -> dict:
    return {
        "seq": row.seq,
        "ts": row.ts,
        "type": row.event_type,
        "data": {},
        "_log_page": {
            "truncated": True,
            "raw_bytes": row.raw_bytes,
            "reason": "row_exceeds_byte_limit",
        },
    }


def _row_cost(row: _Row, byte_limit: int) -> int:
    if row.raw_bytes <= byte_limit:
        return row.raw_bytes
    return len(orjson.dumps(_placeholder(row)))


def _bounded_tail(rows: list[_Row], end: int, limit: int, byte_limit: int) -> tuple[int, int]:
    start = end
    used = 0
    while start > 0 and end - start < limit:
        cost = _row_cost(rows[start - 1], byte_limit)
        if used + cost > byte_limit:
            break
        start -= 1
        used += cost
    return start, end


def _bounded_forward(rows: list[_Row], start: int, limit: int,
                     byte_limit: int) -> tuple[int, int]:
    end = start
    used = 0
    while end < len(rows) and end - start < limit:
        cost = _row_cost(rows[end], byte_limit)
        if used + cost > byte_limit:
            break
        used += cost
        end += 1
    return start, end


def _nearest_seq_index(index: _Index, anchor: int) -> int:
    if not index.seq_values:
        return max(0, len(index.rows) - 1)
    pos = bisect.bisect_left(index.seq_values, anchor)
    if pos == len(index.seq_values):
        return index.seq_row_indexes[-1]
    if (pos and anchor - index.seq_values[pos - 1]
            <= index.seq_values[pos] - anchor):
        return index.seq_row_indexes[pos - 1]
    return index.seq_row_indexes[pos]


def _bounded_around(rows: list[_Row], anchor_index: int, limit: int,
                    byte_limit: int) -> tuple[int, int]:
    half = limit // 2
    start = max(0, anchor_index - half)
    end = min(len(rows), start + limit)
    start = max(0, end - limit)
    used = sum(_row_cost(row, byte_limit) for row in rows[start:end])
    # Preserve a contiguous window containing the anchor. Remove the farther edge until bounded.
    remove_left = True
    while used > byte_limit and end - start > 1:
        left_distance = anchor_index - start
        right_distance = (end - 1) - anchor_index
        if left_distance > right_distance or (left_distance == right_distance and remove_left):
            used -= _row_cost(rows[start], byte_limit)
            start += 1
            remove_left = False
        else:
            end -= 1
            used -= _row_cost(rows[end], byte_limit)
            remove_left = True
    return start, end


class EventLogPager:
    """Thread-safe incremental byte index, scoped to one UI server process."""

    def __init__(self, *, max_indexed_runs: int = MAX_INDEXED_RUNS):
        if (not isinstance(max_indexed_runs, int) or isinstance(max_indexed_runs, bool)
                or max_indexed_runs < 1):
            raise ValueError("max_indexed_runs must be a positive integer")
        self.max_indexed_runs = max_indexed_runs
        # A dense boundary index makes page reads and anchor jumps fast. Keep only a small LRU of
        # active runs so browsing thousands of historical runs cannot retain every row forever.
        self._indexes: OrderedDict[str, _Index] = OrderedDict()
        self._lock = threading.RLock()

    def _refresh(self, path: Path, handle: BinaryIO, snapshot_size: int,
                 identity: tuple[int, int], metadata: tuple[int, int]) -> _Index:
        key = str(path)
        generation = _first_generation(handle, snapshot_size)
        index = self._indexes.get(key)
        if (index is None or index.identity != identity or index.generation != generation
                or snapshot_size < index.observed_size
                or (snapshot_size == index.observed_size and index.metadata != metadata)):
            index = _Index(identity=identity, generation=generation, metadata=metadata)
            self._indexes[key] = index
        self._indexes.move_to_end(key)
        while len(self._indexes) > self.max_indexed_runs:
            self._indexes.popitem(last=False)
        # Re-scan a previously torn boundary even if the size is unchanged: a writer may have filled
        # reserved bytes in place. For a normal append this extends only from the old valid boundary.
        if snapshot_size != index.observed_size or index.torn_tail or not index.rows:
            _scan(handle, index, snapshot_size)
        index.metadata = metadata
        return index

    @staticmethod
    def _materialize(handle: BinaryIO, rows: list[_Row], start: int, end: int,
                     byte_limit: int) -> tuple[list[dict], int]:
        events: list[dict] = []
        wire_bytes = 0
        for row in rows[start:end]:
            if row.raw_bytes > byte_limit:
                event = _placeholder(row)
            else:
                handle.seek(row.start)
                raw = handle.read(row.raw_bytes).strip()
                try:
                    obj = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    obj = None
                event = obj if isinstance(obj, dict) else _placeholder(row)
            size = len(orjson.dumps(event))
            # Selection accounts for source bytes (which are at least compact JSON bytes) or the
            # exact placeholder size. This assertion makes the wire cap an executable invariant.
            if wire_bytes + size > byte_limit:  # pragma: no cover - defensive against file mutation
                break
            events.append(event)
            wire_bytes += size
        return events, wire_bytes

    def page(self, path: str | os.PathLike, *, direction: str = "tail", limit: int = DEFAULT_ROWS,
             byte_limit: int = DEFAULT_BYTES, cursor: Optional[str] = None,
             generation: Optional[str] = None, anchor_seq: Optional[int] = None) -> dict:
        if direction not in _DIRECTIONS:
            raise HTTPException(400, {
                "code": "invalid_log_direction",
                "message": "direction must be tail, older, newer, or around",
            })
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
            raise HTTPException(400, {"code": "invalid_log_limit", "message": "limit is out of range"})
        if (not isinstance(byte_limit, int) or isinstance(byte_limit, bool)
                or not MIN_BYTES <= byte_limit <= MAX_BYTES):
            raise HTTPException(400, {
                "code": "invalid_log_byte_limit", "message": "byte_limit is out of range"})
        expected = _normalize_generation(generation)
        cursor_generation: Optional[str] = None
        cursor_revision: Optional[str] = None
        boundary: Optional[int] = None
        if direction in {"older", "newer"}:
            if cursor is None or anchor_seq is not None:
                raise HTTPException(400, {
                    "code": "invalid_log_page_request",
                    "message": "older/newer requires cursor and forbids anchor_seq",
                })
            cursor_generation, cursor_revision, boundary = _decode_cursor(cursor)
        elif direction == "around":
            if (cursor is not None or not isinstance(anchor_seq, int)
                    or isinstance(anchor_seq, bool) or anchor_seq < 0):
                raise HTTPException(400, {
                    "code": "invalid_log_page_request",
                    "message": "around requires an integer anchor_seq and forbids cursor",
                })
        elif cursor is not None or anchor_seq is not None:
            raise HTTPException(400, {
                "code": "invalid_log_page_request",
                "message": "tail forbids cursor and anchor_seq",
            })

        path = Path(path)
        try:
            handle = open(path, "rb")
        except OSError as exc:
            raise HTTPException(404, "no such run") from exc
        with handle, self._lock:
            stat = os.fstat(handle.fileno())
            snapshot_size = stat.st_size
            identity = (stat.st_dev, stat.st_ino)
            metadata = _metadata_signature(stat)
            index = self._refresh(path, handle, snapshot_size, identity, metadata)
            actual = index.generation
            for requested in (expected, cursor_generation):
                if requested is not None and requested != actual:
                    raise _generation_changed(requested, actual)
            if cursor_revision is not None and cursor_revision != index.revision:
                raise _generation_changed(cursor_generation, actual)
            rows = index.rows
            if boundary is not None and boundary > len(rows):
                raise _generation_changed(cursor_generation, actual)

            matched_seq = None
            if direction == "tail":
                start, end = _bounded_tail(rows, len(rows), limit, byte_limit)
            elif direction == "older":
                start, end = _bounded_tail(rows, int(boundary), limit, byte_limit)
            elif direction == "newer":
                start, end = _bounded_forward(rows, int(boundary), limit, byte_limit)
            else:
                if rows:
                    anchor_index = _nearest_seq_index(index, int(anchor_seq))
                    matched_seq = rows[anchor_index].seq
                    start, end = _bounded_around(rows, anchor_index, limit, byte_limit)
                else:
                    start = end = 0
            events, wire_bytes = self._materialize(handle, rows, start, end, byte_limit)
            # Detect the normal reset-by-replace race between opening and returning the page. An
            # append is fine (same identity, larger size); replacement or shrink must invalidate this
            # snapshot so the client never accepts it as belonging to the new path generation.
            try:
                with open(path, "rb") as current_handle:
                    current = os.fstat(current_handle.fileno())
                    current_generation = _first_generation(current_handle, current.st_size)
            except OSError:
                raise _generation_changed(actual, None)
            if ((current.st_dev, current.st_ino) != identity or current.st_size < snapshot_size
                    or current_generation != actual
                    or (current.st_size == snapshot_size
                        and _metadata_signature(current) != metadata)):
                raise _generation_changed(actual, current_generation)
            # A defensive materialization stop cannot advertise unseen rows as part of this page.
            end = start + len(events)
            cursors = ({"older": _encode_cursor(actual, index.revision, start),
                        "newer": _encode_cursor(actual, index.revision, end)}
                       if actual is not None else {"older": None, "newer": None})
            response = {
                "events": events,
                "generation": actual,
                "cursors": cursors,
                "has_more": {"older": start > 0, "newer": end < len(rows)},
                "torn_tail": index.torn_tail,
                "source_tail_limited": index.source_tail_limited,
                "bytes": wire_bytes,
                "limit": limit,
                "byte_limit": byte_limit,
                "total_events": len(rows),
                "range": {
                    "start_index": start,
                    "end_index": end,
                    "first_seq": events[0].get("seq") if events else None,
                    "last_seq": events[-1].get("seq") if events else None,
                },
            }
            if direction == "around":
                response.update({"anchor_seq": anchor_seq, "matched_seq": matched_seq})
            return response
