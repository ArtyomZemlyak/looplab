"""Incremental observations for append-only run-command monitoring.

Command workers poll an append-only ``events.jsonl`` while waiting for an exact intent,
acknowledgement, domain failure, or terminal postcondition.  Re-reading and folding the complete
log in every helper turns a long run into quadratic disk parsing.  This module scans each recoverable
event byte once, retains only eight active run indexes, and gives one stable logical observation to
all helpers in a monitor iteration.

The scanner deliberately mirrors :func:`looplab.events.eventstore.iter_event_jsonl`: the first partial,
invalid, non-object, invalid-Event, malformed batch, or duplicate/backward sequence row ends the recoverable
prefix. A later append resumes from the last valid byte boundary, so completing a torn tail is observed
without re-reading the prefix.
Replacement, shrink, and a sampled same-size sentinel change rebuild the index before it is trusted.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Mapping, Optional

import orjson

from looplab.core.models import Event, RunState
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.eventstore import decode_event_record, event_sequence_continues
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_DROPPED, EV_COMMAND_ACK, EV_RUN_ABORT, EV_RUN_FINISHED
from looplab.serve.protocol import CONTROL_EVENTS


MAX_INDEXED_RUNS = 8
_PROBE_WINDOW_BYTES = 4 * 1024
_PROBE_FULL_FILE_LIMIT = 3 * _PROBE_WINDOW_BYTES
_DUPLICATE_INTENT = object()


class _ObservationChanged(RuntimeError):
    """The sampled file content changed while one logical snapshot was being built."""


@dataclass
class ObservationMetrics:
    """White-box counters used by scale tests and local performance diagnostics."""

    refreshes: int = 0
    cache_hits: int = 0
    rebuilds: int = 0
    scan_calls: int = 0
    bytes_read: int = 0
    records_parsed: int = 0
    last_bytes_read: int = 0
    last_records_parsed: int = 0


@dataclass
class _Index:
    identity: tuple[int, int]
    metadata: tuple[int, int]
    probe_signature: bytes = b""
    revision: str = field(default_factory=lambda: secrets.token_hex(16))
    observed_size: int = 0
    valid_end: int = 0
    stopped_tail: bool = False
    event_chunks: tuple[tuple[Event, ...], ...] = ()
    event_count: int = 0
    latest_seq: int = -1
    max_non_control_seq: int = -1
    # Copy-on-write containers make an observation immutable even if another caller extends the
    # same index concurrently after ``observe`` releases the cache lock.
    intents: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    acknowledgements: Mapping[str, tuple[object, ...]] = field(
        default_factory=lambda: MappingProxyType({}))
    run_finishes: tuple[Event, ...] = ()
    latest_run_abort: Optional[Event] = None
    materialized_revision: Optional[str] = None
    materialized_events: Optional[tuple[Event, ...]] = None
    folded_revision: Optional[str] = None
    folded_state: Optional[RunState] = None
    finalize_revision: Optional[str] = None
    finalize_scope: Optional[str] = None

    def invalidate_materializations(self) -> None:
        self.materialized_revision = None
        self.materialized_events = None
        self.folded_revision = None
        self.folded_state = None
        self.finalize_revision = None
        self.finalize_scope = None


def _identity(stat: os.stat_result) -> tuple[int, int]:
    return stat.st_dev, stat.st_ino


def _metadata(stat: os.stat_result) -> tuple[int, int]:
    # Windows can report a transiently different ctime through ``fstat`` and ``Path.stat`` for the
    # same open file.  mtime is the cross-view content signal promised by this index; identity and
    # size provide the other reset fences. Keep a two-item tuple for an explicit schema/version slot.
    return stat.st_mtime_ns, 1


def _probe_signature(handle: BinaryIO, size: int) -> bytes:
    """Hash bounded content sentinels without charging them as event-log scan bytes.

    Some Windows filesystems can preserve the same observable mtime across a fast in-place
    rewrite.  Size and metadata therefore cannot be the only cache fences.  Small logs are probed
    completely; larger logs sample fixed-size start, middle, and end windows.  Probe work remains
    O(1) as a log grows, while ``ObservationMetrics.last_bytes_read`` continues to describe only
    bytes parsed by the incremental event scanner.
    """
    position = handle.tell()
    try:
        digest = hashlib.blake2b(digest_size=16, person=b"looplab-cmd-log")
        digest.update(size.to_bytes(8, byteorder="little", signed=False))
        if size <= _PROBE_FULL_FILE_LIMIT:
            spans = ((0, size),)
        else:
            middle = max(0, (size - _PROBE_WINDOW_BYTES) // 2)
            spans = (
                (0, _PROBE_WINDOW_BYTES),
                (middle, _PROBE_WINDOW_BYTES),
                (size - _PROBE_WINDOW_BYTES, _PROBE_WINDOW_BYTES),
            )
        for offset, length in spans:
            handle.seek(offset)
            raw = handle.read(length)
            digest.update(offset.to_bytes(8, byteorder="little", signed=False))
            digest.update(len(raw).to_bytes(8, byteorder="little", signed=False))
            digest.update(raw)
        return digest.digest()
    finally:
        handle.seek(position)


def _parse_event(raw: bytes) -> Optional[tuple[Event, ...]]:
    """Parse one physical row and atomically expand its logical Event members."""

    line = raw.strip()
    if not line:
        return None
    try:
        value = orjson.loads(line)
    except orjson.JSONDecodeError as exc:
        raise ValueError("invalid JSON event row") from exc
    if not isinstance(value, dict):
        raise ValueError("event row is not an object")
    try:
        return tuple(decode_event_record(value))
    except Exception as exc:  # noqa: BLE001 - identical recoverable boundary to EventStore.read_all
        raise ValueError("invalid event envelope") from exc


def _apply_delta(index: _Index, events: list[Event]) -> None:
    if not events:
        return
    intents: Optional[dict[str, object]] = None
    acknowledgements: Optional[dict[str, tuple[object, ...]]] = None
    finishes: Optional[list[Event]] = None
    latest_abort = index.latest_run_abort
    max_non_control = index.max_non_control_seq

    for event in events:
        index.latest_seq = event.seq
        data = event.data or {}
        # Compatibility only: releases before `card_auto_dropped` wrote engine domain effects with the
        # operator command type. Treat those historical rows as progress. New writers have disjoint
        # types, so no new observation path depends on this payload-level provenance exception.
        engine_card_drop = (event.type == EV_CARD_DROPPED
                            and data.get("dropped_by") != "operator")
        if event.type not in CONTROL_EVENTS or engine_card_drop:
            max_non_control = max(max_non_control, event.seq)
        marker = data.get("_command_id")
        if isinstance(marker, str):
            if intents is None:
                intents = dict(index.intents)
            prior = intents.get(marker)
            intents[marker] = event if prior is None else _DUPLICATE_INTENT
        if event.type == EV_COMMAND_ACK:
            command_id = str(data.get("command_id") or "")
            if acknowledgements is None:
                acknowledgements = dict(index.acknowledgements)
            acknowledgements[command_id] = acknowledgements.get(command_id, ()) + (
                data.get("event_seq"),)
        if event.type == EV_RUN_FINISHED:
            if finishes is None:
                finishes = list(index.run_finishes)
            finishes.append(event)
        if event.type == EV_RUN_ABORT:
            latest_abort = event

    if intents is not None:
        index.intents = MappingProxyType(intents)
    if acknowledgements is not None:
        index.acknowledgements = MappingProxyType(acknowledgements)
    if finishes is not None:
        index.run_finishes = tuple(finishes)
    index.latest_run_abort = latest_abort
    index.max_non_control_seq = max_non_control
    index.event_chunks = index.event_chunks + (tuple(events),)
    index.event_count += len(events)
    index.revision = secrets.token_hex(16)
    index.invalidate_materializations()


def _scan(handle: BinaryIO, index: _Index, snapshot_size: int,
          metrics: ObservationMetrics) -> None:
    """Scan from ``valid_end`` through one stable-size prefix."""
    handle.seek(index.valid_end)
    delta: list[Event] = []
    bytes_read = 0
    parsed = 0
    stopped = False
    valid_end = index.valid_end
    expected_seq = index.latest_seq + 1
    while handle.tell() < snapshot_size:
        start = handle.tell()
        raw = handle.readline(snapshot_size - start)
        bytes_read += len(raw)
        if not raw or not raw.endswith(b"\n"):
            stopped = bool(raw)
            break
        end = handle.tell()
        try:
            events = _parse_event(raw)
        except ValueError:
            stopped = True
            break
        # Blank complete rows are part of the valid byte prefix but not EventStore events.
        if events is not None:
            if not event_sequence_continues(events, expected_seq):
                stopped = True
                break
            delta.extend(events)
            parsed += len(events)
            expected_seq = events[-1].seq + 1
        valid_end = end

    index.valid_end = valid_end
    index.observed_size = snapshot_size
    index.stopped_tail = stopped
    _apply_delta(index, delta)
    metrics.scan_calls += 1
    metrics.bytes_read += bytes_read
    metrics.records_parsed += parsed
    metrics.last_bytes_read = bytes_read
    metrics.last_records_parsed = parsed


@dataclass(frozen=True)
class CommandObservation:
    """One stable recoverable-prefix view whose public model accessors return defensive copies."""

    path: Path
    revision: str
    observed_size: int
    valid_end: int
    torn_tail: bool
    event_count: int
    latest_seq: int
    max_non_control_seq: int
    _intents: Mapping[str, object]
    _acknowledgements: Mapping[str, tuple[object, ...]]
    _run_finishes: tuple[Event, ...]
    _latest_run_abort: Optional[Event]
    _chunks: tuple[tuple[Event, ...], ...]
    _owner: "CommandObservationIndex" = field(repr=False, compare=False)
    _index: _Index = field(repr=False, compare=False)

    def marked_intent(self, command_id: str) -> Optional[Event]:
        value = self._intents.get(command_id)
        return value.model_copy(deep=True) if isinstance(value, Event) else None

    def has_ack(self, command_id: str, event_seq: object) -> bool:
        # Tuple membership retains Python's exact historical equality semantics (including legacy
        # oddities such as ``True == 1``) instead of narrowing old logs to a new integer schema.
        return event_seq in self._acknowledgements.get(command_id, ())

    def has_domain_progress(self, after_seq: int) -> bool:
        return self.max_non_control_seq > after_seq

    def domain_failure_after(self, after_seq: int) -> Optional[Event]:
        event = next(
            (event for event in self._run_finishes
             if event.seq > after_seq and (event.data or {}).get("reason") == "error"),
            None,
        )
        return event.model_copy(deep=True) if event is not None else None

    def has_non_error_finish_after(self, after_seq: int) -> bool:
        return any(
            event.seq > after_seq
            and str((event.data or {}).get("reason") or "").lower() != "error"
            for event in self._run_finishes
        )

    @property
    def latest_run_abort(self) -> Optional[Event]:
        return (self._latest_run_abort.model_copy(deep=True)
                if self._latest_run_abort is not None else None)

    def events(self) -> tuple[Event, ...]:
        return tuple(event.model_copy(deep=True) for event in self._owner._materialize(self))

    def state(self) -> RunState:
        return self._owner._fold(self).model_copy(deep=True)

    def incomplete_finalize_scope(self) -> Optional[str]:
        return self._owner._incomplete_finalize_scope(self)


class CommandObservationIndex:
    """Thread-safe incremental LRU over event logs used by command workers."""

    def __init__(self, *, max_indexed_runs: int = MAX_INDEXED_RUNS):
        if (not isinstance(max_indexed_runs, int) or isinstance(max_indexed_runs, bool)
                or max_indexed_runs < 1):
            raise ValueError("max_indexed_runs must be a positive integer")
        self.max_indexed_runs = max_indexed_runs
        self._indexes: OrderedDict[str, _Index] = OrderedDict()
        self._lock = threading.RLock()
        self.metrics = ObservationMetrics()

    @property
    def cached_paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._indexes)

    def _new_index(self, stat: os.stat_result) -> _Index:
        self.metrics.rebuilds += 1
        return _Index(identity=_identity(stat), metadata=_metadata(stat))

    def _refresh_locked(self, path: Path, handle: BinaryIO, stat: os.stat_result) -> _Index:
        key = str(path)
        size = stat.st_size
        identity = _identity(stat)
        metadata = _metadata(stat)
        probe_before = _probe_signature(handle, size)
        index = self._indexes.get(key)
        rebuild = (
            index is None
            or index.identity != identity
            or size < index.observed_size
            or (size == index.observed_size and metadata != index.metadata)
            or (size == index.observed_size and probe_before != index.probe_signature)
        )
        if rebuild:
            index = self._new_index(stat)
            self._indexes[key] = index
        self._indexes.move_to_end(key)
        while len(self._indexes) > self.max_indexed_runs:
            self._indexes.popitem(last=False)

        # An unchanged stopped tail is a cache hit. Growth replays only from the valid boundary,
        # including the formerly partial row that may now have its terminating newline.
        cache_hit = not rebuild and size == index.observed_size
        if not cache_hit:
            _scan(handle, index, size, self.metrics)
        probe_after = _probe_signature(handle, size)
        if probe_after != probe_before:
            # Never pair events parsed from one same-size image with the sentinel signature from a
            # later image. Without this fence the next poll could trust that stale pairing forever.
            self._indexes.pop(key, None)
            raise _ObservationChanged
        if cache_hit:
            self.metrics.cache_hits += 1
            self.metrics.last_bytes_read = 0
            self.metrics.last_records_parsed = 0
        index.metadata = metadata
        index.probe_signature = probe_after
        return index

    def observe(self, path: str | os.PathLike) -> CommandObservation:
        path = Path(path)
        # A replacement can race the first open. Retry a bounded number of times until the path still
        # names the indexed handle. Ordinary append growth after the snapshot is fine: this
        # observation consistently names the earlier complete prefix and the next poll reads delta.
        for attempt in range(3):
            with open(path, "rb") as handle, self._lock:
                self.metrics.refreshes += 1
                stat = os.fstat(handle.fileno())
                try:
                    index = self._refresh_locked(path, handle, stat)
                except _ObservationChanged as exc:
                    if attempt < 2:
                        continue
                    raise OSError(f"event log changed while observing {path}") from exc
                try:
                    current = path.stat()
                except OSError:
                    if attempt < 2:
                        continue
                    raise
                sampled_content_changed = (
                    _probe_signature(handle, stat.st_size) != index.probe_signature)
                if (_identity(current) != _identity(stat)
                        or current.st_size < stat.st_size
                        or (current.st_size == stat.st_size
                            and _metadata(current) != _metadata(stat))
                        or sampled_content_changed):
                    self._indexes.pop(str(path), None)
                    if attempt < 2:
                        continue
                    raise OSError(f"event log changed while observing {path}")
                return CommandObservation(
                    path=path,
                    revision=index.revision,
                    observed_size=index.observed_size,
                    valid_end=index.valid_end,
                    torn_tail=index.stopped_tail,
                    event_count=index.event_count,
                    latest_seq=index.latest_seq,
                    max_non_control_seq=index.max_non_control_seq,
                    _intents=index.intents,
                    _acknowledgements=index.acknowledgements,
                    _run_finishes=index.run_finishes,
                    _latest_run_abort=index.latest_run_abort,
                    _chunks=index.event_chunks,
                    _owner=self,
                    _index=index,
                )
        raise OSError(f"event log did not stabilize while observing {path}")  # pragma: no cover

    def _materialize(self, observation: CommandObservation) -> tuple[Event, ...]:
        index = observation._index
        with self._lock:
            if (index.materialized_revision == observation.revision
                    and index.materialized_events is not None):
                return index.materialized_events
        materialized = tuple(chain.from_iterable(observation._chunks))
        with self._lock:
            if index.revision == observation.revision:
                index.materialized_revision = observation.revision
                index.materialized_events = materialized
        return materialized

    def _fold(self, observation: CommandObservation) -> RunState:
        index = observation._index
        with self._lock:
            if index.folded_revision == observation.revision and index.folded_state is not None:
                return index.folded_state
        state = fold(self._materialize(observation))
        with self._lock:
            if index.revision == observation.revision:
                index.folded_revision = observation.revision
                index.folded_state = state
        return state

    def _incomplete_finalize_scope(self, observation: CommandObservation) -> Optional[str]:
        index = observation._index
        with self._lock:
            if index.finalize_revision == observation.revision:
                return index.finalize_scope
        scope = incomplete_finalize_scope(self._materialize(observation))
        with self._lock:
            if index.revision == observation.revision:
                index.finalize_revision = observation.revision
                index.finalize_scope = scope
        return scope
