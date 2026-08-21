"""Append-only event store (I1, ADR-1/17): orjson JSONL with serialized appenders.

`read_all` tolerates a torn/partial final line (a crash mid-append) by stopping
at the first line without a trailing newline or that fails to parse. This is the
prefix-recovery contract exercised by the replay-determinism keystone test.
The engine is the single live reducer, while the engine and control server may both append under the
cross-process lock. Flush/fsync are best effort by default; paid-work/control claims opt into required
locking and durability at their call sites.
"""
from __future__ import annotations

import errno
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import orjson

from looplab.core.atomicio import best_effort_fsync, strict_fsync, strict_fsync_parent
from looplab.core.models import Event
from looplab.core.run_deletion import assert_run_deletion_write_allowed
from looplab.core.run_reset import assert_run_reset_write_allowed
from looplab.core.tracing import current_ids

# Generic JSONL store I/O moved DOWN to core (doc 25 EV-12) — nothing here is event-related, and
# subsystems that only wanted to read a JSONL file should not have to import `events` to do it.
# Re-exported so both spellings name the SAME objects: every existing import and monkeypatch seam
# through `looplab.events.eventstore` keeps working unchanged.
from looplab.core.jsonlio import (  # noqa: F401
    JsonlRecordInvalid,
    decode_jsonl_line,
    iter_jsonl,
    read_jsonl_lenient,
    read_jsonl_lenient_with_health,
    replace_jsonl_rows_atomic_preserving_quarantine,
    scan_jsonl_region,
    write_jsonl_atomic,
)

# Sentinel for EventStore.append's trace_id/span_id: distinguishes "not passed" (stamp with the ambient
# span via current_ids) from an EXPLICIT None (a telemetry event that must carry NO trace).
_UNSET_TRACE = object()

# ``append_many`` is a logical transaction, not a promise that one ``write`` syscall cannot tear. Keep
# its members in one newline-delimited envelope so the existing torn-final-line rule exposes either the
# complete batch or none of it. The guarded type is reserved and decoded below before any caller sees
# Events; its non-string disk shape makes pre-batch readers stop instead of dropping the members.
_EVENT_BATCH_TYPE = "__looplab_event_batch_v1__"
_EVENT_BATCH_SCHEMA = "looplab.event-batch/v1"
# New writers encode the reserved type as a one-item JSON array.  That shape is deliberately invalid
# for the historical ``Event.type: str`` contract: a pre-batch binary therefore hits its complete-row
# corruption fence and refuses to append, instead of accepting one unknown event while losing every
# nested lifecycle action.  Current readers normalize both this guarded marker and the short-lived
# string marker already present in logs written during the initial rollout.
_EVENT_BATCH_GUARD_TYPE = (_EVENT_BATCH_TYPE,)
# Keep construction/validation CPU and memory bounded independently of serialized size, while staying
# above every canonical fan-out knob (Settings.n_seeds/eval_parallel allow 1024; max_parallel is its
# legacy alias). Custom policy lanes may
# be wider, so retain generous headroom; the byte ceiling below remains the tighter bound for rich rows.
_MAX_EVENT_BATCH_MEMBERS = 4096
_MAX_EVENT_BATCH_BYTES = 8 * 1024 * 1024
# Public physical-record ceiling for bounded event-log readers. It includes the terminating newline,
# matching ``append_many``'s payload check; readers must not reject a writer-valid batch prelude.
MAX_EVENT_BATCH_BYTES = _MAX_EVENT_BATCH_BYTES
_EVENT_FIELDS = frozenset({"v", "seq", "ts", "type", "data", "trace_id", "span_id"})
_EVENT_BATCH_DATA_FIELDS = frozenset({"schema", "count", "first_seq", "last_seq", "events"})


def is_event_batch_record(obj: object) -> bool:
    """Whether a parsed physical event-log object claims the reserved batch protocol."""

    if not isinstance(obj, dict):
        return False
    marker = obj.get("type")
    return marker == _EVENT_BATCH_TYPE or marker == list(_EVENT_BATCH_GUARD_TYPE)


# Event-envelope FORMAT versions this build understands (`Event.v`, ADR-1). A migration that changes
# what a row MEANS bumps this set and adds an explicit upgrade path; until then a row declaring
# anything else is undecodable here rather than folded under v1 semantics.
SUPPORTED_EVENT_ENVELOPE_VERSIONS = frozenset({1})


class UnsupportedEventVersionError(ValueError):
    """A row declares an envelope version this build does not understand.

    A ValueError subclass so every existing tolerant reader (which stops the log at any decode
    failure) is unchanged, while a caller that wants to say WHICH kind of unreadable this is —
    "written by a newer LoopLab" rather than "corrupt" — can catch it specifically."""


def _decode_batch_envelope(obj: dict) -> list[Event]:
    """Strictly validate and expand one internal crash-atomic batch envelope."""

    if set(obj) != _EVENT_FIELDS or not is_event_batch_record(obj):
        raise ValueError("invalid event batch envelope")
    # This is an internal v1 storage protocol, not an ordinary backwards-compatible Event row.  Never
    # let Pydantic coercion make a non-canonical envelope valid (notably ``True == 1`` for seq/version).
    canonical_outer = dict(obj)
    canonical_outer["type"] = _EVENT_BATCH_TYPE
    outer = Event.model_validate(canonical_outer, strict=True)
    if outer.model_dump(mode="json") != canonical_outer:
        raise ValueError("non-canonical event batch envelope")
    if outer.v != 1:
        raise ValueError("unsupported event batch envelope version")
    data = outer.data
    if set(data) != _EVENT_BATCH_DATA_FIELDS or data.get("schema") != _EVENT_BATCH_SCHEMA:
        raise ValueError("invalid event batch schema")
    count = data.get("count")
    first_seq = data.get("first_seq")
    last_seq = data.get("last_seq")
    members = data.get("events")
    if (
        type(count) is not int
        or not 1 <= count <= _MAX_EVENT_BATCH_MEMBERS
        or type(first_seq) is not int
        or type(last_seq) is not int
        or first_seq < 0
        or last_seq != outer.seq
        or first_seq != last_seq - count + 1
        or not isinstance(members, list)
        or len(members) != count
        or len(orjson.dumps(obj)) + 1 > _MAX_EVENT_BATCH_BYTES
    ):
        raise ValueError("invalid event batch bounds")

    events: list[Event] = []
    for offset, raw in enumerate(members):
        if not isinstance(raw, dict) or set(raw) != _EVENT_FIELDS:
            raise ValueError("invalid event batch member")
        event = Event.model_validate(raw, strict=True)
        if (
            event.model_dump(mode="json") != raw
            or event.v != 1
            or event.type == _EVENT_BATCH_TYPE
            or event.seq != first_seq + offset
        ):
            raise ValueError("invalid event batch member")
        events.append(event)
    return events


def decode_event_record(obj: dict, *, strict: bool = False) -> list[Event]:
    """Decode one physical event-log object into its logical Events.

    Ordinary historical rows retain EventStore's tolerant Pydantic compatibility unless ``strict`` is
    requested by an authority boundary.  The reserved batch protocol is always strict and canonical.
    Callers that read arbitrary JSONL must not use this helper: a foreign object may legitimately carry
    the same ``type`` string without being an event envelope.
    """

    if is_event_batch_record(obj):
        return _decode_batch_envelope(obj)
    event = Event.model_validate(obj, strict=True) if strict else Event(**obj)
    # `v` is the envelope's FORMAT version (ADR-1), and an unsupported value means this build cannot
    # know what the row means. Accepting it and folding with today's semantics is the dangerous
    # reading: a future v2 row would silently acquire v1 run and command authority. The batch
    # protocol already refused a non-1 version; ordinary rows treated `v` as inert metadata.
    # Raising is what every caller already handles — an undecodable row is where the log STOPS, so a
    # newer tail fails closed (`repair-log`/upgrade) instead of being half-understood. The raw check
    # additionally rejects `true`, which lax Pydantic would coerce to 1 (mirrors _decode_batch_envelope).
    raw_v = obj.get("v", 1)
    if isinstance(raw_v, bool) or event.v not in SUPPORTED_EVENT_ENVELOPE_VERSIONS:
        raise UnsupportedEventVersionError(f"unsupported event envelope version {raw_v!r}")
    return [event]


def event_sequence_continues(events: Sequence[Event], expected_seq: int) -> bool:
    """Whether logical events form the DENSE prefix beginning at ``expected_seq``.

    The engine only ever appends densely (``seq = last + 1``), and ``repair-log`` truncates a corrupt
    TAIL, leaving a dense prefix — so NO legitimate workflow produces a monotonic gap. A forward seq
    jump can therefore only come from corruption/tampering (e.g. a same-size in-place rewrite that bumps
    a mid-file seq), and MUST fail closed rather than be admitted as a "repaired gap" that a CAS append
    then writes on top of. (Restores the 5f011a2 fail-closed fence that 16f941f's gap tolerance voided;
    that tolerance covered a repair workflow that does not exist in this codebase.)
    """
    # EMPTY is not a continuation: `all([])` is vacuously True, but every caller then dereferences
    # `events[-1].seq` (iter_event_jsonl / log_divergence / the incremental cache) -> IndexError. Today's
    # callers pre-filter empties (the batch decoder requires count>=1), so this is a defensive guard on a
    # public exported helper, restoring the pre-rewrite `if not events: return False`.
    if not events:
        return False
    return all(
        type(event.seq) is int and event.seq == expected_seq + offset
        for offset, event in enumerate(events)
    )


# Private compatibility name for older internal call sites.  New event-specific readers should import the
# public helper so the generic JSONL reader can remain format-agnostic.
_decode_event_record = decode_event_record


class EventLogCorruptionError(RuntimeError):
    """A COMPLETE corrupt record was found in an append-only event log. `read_all` stops at that
    record, so even when it is currently the last line, the next append would create a durable tail
    that `fold` can never see. The store therefore FAILS CLOSED until `looplab repair-log` backs up and
    truncates the invalid record. Reads still return the recoverable prefix."""

    def __init__(self, path: "str | os.PathLike", detail: dict):
        self.path = str(path)
        self.detail = dict(detail)
        run_dir = Path(path).parent
        super().__init__(
            f"event log {path} is corrupted at line {detail.get('corrupt_line')}: "
            f"the newline-terminated record is invalid and {detail.get('dropped_lines')} later "
            f"record(s) are DROPPED on replay. Refusing to append. Run `looplab repair-log {run_dir}` "
            f"to back up and truncate the log to its last valid boundary before resuming.")


class EventStoreConcurrencyError(RuntimeError):
    """Optimistic-concurrency (explicit-seq) check failed on `append(expected_last_seq=...)`: the log
    tail moved between the caller reading state and appending, so another writer landed an event in
    between. The lean 'explicit seq' half of P1-12 (arch-review §2): the engine writer is already
    lock-serialized, so this is for a CALLER that wants to append only against the exact state it saw
    (e.g. a UI control intent raised on a now-stale view). Full multi-writer CAS stays deferred."""

    def __init__(self, path: "str | os.PathLike", expected: int, actual: int):
        self.path = str(path)
        self.expected = int(expected)
        self.actual = int(actual)
        super().__init__(
            f"append to {path} expected the log to be at seq {expected}, but it is at {actual} — "
            f"another writer appended in between; re-read the state and retry.")


class EventStoreLockError(RuntimeError):
    """A caller requiring cross-process append serialization could not acquire that guarantee.

    Ordinary engine writers retain the historical best-effort behavior.  Security-sensitive
    multi-writer protocols opt into this error rather than silently appending unlocked.
    """

    def __init__(self, path: "str | os.PathLike", cause: BaseException):
        self.path = str(path)
        self.cause = cause
        super().__init__(f"cross-process event append lock is unavailable for {path}: {cause}")


class InterprocessLockContended(RuntimeError):
    """A requested non-blocking cross-process lock is held by another process."""

    def __init__(self, path: "str | os.PathLike"):
        self.path = str(path)
        super().__init__(f"cross-process lock is already held for {path}")


@contextmanager
def _interprocess_lock(lock_path: Path, *, required: bool = False, blocking: bool = True):
    """Best-effort exclusive cross-process lock (msvcrt on Windows, fcntl on POSIX). The live UI
    server appends control events to the SAME events.jsonl the engine subprocess writes; without
    serialization their appends can interleave into a torn line (which `iter_jsonl` truncates at,
    silently dropping later events). A non-blocking caller gets ``InterprocessLockContended`` when
    another owner holds the byte/inode instead of waiting behind it.

    Two DIFFERENT failures, deliberately treated differently — the docstring used to describe only
    the first and readers took "degrades to a no-op if locking is unavailable" as universal:
    * a locking CAPABILITY gap (no fcntl/msvcrt, or a filesystem that reports advisory locks as
      unsupported) degrades to an unlocked no-op for an ordinary caller, and raises
      ``EventStoreLockError`` when ``required`` is set;
    * an inaccessible lock PATH (the sibling ``.lock`` cannot be opened) raises for EVERY caller —
      wrapped in ``EventStoreLockError`` when ``required``, re-raised as the bare ``OSError``
      otherwise. It does not degrade. A mount where events.jsonl is appendable but its ``.lock``
      cannot be created therefore aborts the append rather than running it unlocked, which is the
      long-standing engine-writer behaviour and is preserved on purpose."""
    f = None
    locked = False
    try:
        try:
            f = open(lock_path, "a+")
        except OSError as exc:
            if required:
                raise EventStoreLockError(lock_path, exc) from exc
            # An inaccessible lock PATH never degrades, for a `required` or an ordinary caller alike
            # — see the docstring's two-failure split. This preserves the existing engine-writer
            # behavior: abort the append rather than run it unlocked.
            raise
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(
                    f.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), operation)
            locked = True
        # Some filesystems/runtimes expose the module but report an unsupported advisory-lock
        # operation as ValueError/NotImplementedError rather than OSError.  A strict caller must
        # receive one stable failure type for every such capability gap; otherwise the command
        # worker would turn it into a generic 200/command_worker_failed response.
        except (OSError, ImportError, AttributeError, NotImplementedError, ValueError) as exc:
            contended = not blocking and (
                isinstance(exc, BlockingIOError)
                or (os.name == "nt" and getattr(exc, "errno", None) in {
                    errno.EACCES, getattr(errno, "EDEADLOCK", 36)})
            )
            if contended:
                raise InterprocessLockContended(lock_path) from exc
            if required:
                raise EventStoreLockError(lock_path, exc) from exc
            pass  # ordinary engine writer: retain the historical single-writer degradation
        yield
    finally:
        if f is not None:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (OSError, AttributeError, NotImplementedError, ValueError):
                    pass
            f.close()




def iter_event_jsonl(path: str | os.PathLike) -> Iterator[dict]:
    """Yield logical event envelopes while preserving ``iter_jsonl`` torn-tail semantics.

    A physical ``append_many`` envelope is an implementation detail and expands atomically: malformed
    batches or non-dense logical sequences end the recoverable prefix and no rejected-row member is
    exposed. Keeping this event-aware behavior out of ``iter_jsonl`` is required for non-event JSONL
    stores that share the generic reader.
    """

    expected_seq = 0
    for obj in iter_jsonl(path):
        try:
            events = decode_event_record(obj)
        except Exception:  # noqa: BLE001 - malformed event/batch envelope is log corruption
            break
        if not event_sequence_continues(events, expected_seq):
            break
        expected_seq = events[-1].seq + 1
        if is_event_batch_record(obj):
            for event in events:
                yield event.model_dump(mode="json")
        else:
            # Preserve the legacy raw-envelope projection for ordinary rows (including tolerated
            # additive fields and absent optional defaults). Event construction above is validation,
            # not permission for an event-aware transport or maintenance rewrite to canonicalize it.
            yield obj




def log_divergence(path: str | os.PathLike) -> Optional[dict]:
    """Detect a COMPLETE-record corruption, distinct from the normal torn tail. `iter_jsonl` STOPS at the
    first bad line, so a byte flipped in the MIDDLE of an append-only log — impossible with a single
    local writer, but seen on FUSE / NFS / S3 mounts — silently truncates the replay to the prefix,
    dropping a valid tail with no signal. Returns `{good_records, corrupt_line, dropped_lines}` for
    any invalid COMPLETE (newline-terminated) line, even when it is currently last. Only a final line
    WITHOUT a newline is a normal torn write: append can safely heal it before writing. Treating an
    invalid complete last line as harmless lets the next append create an invisible tail behind it."""
    p = Path(path)
    if not p.exists():
        return None
    lines = p.read_bytes().split(b"\n")
    # A file ending in "\n" leaves a trailing "" element (all lines complete); otherwise the last
    # element is the torn/partial final line. Either way, only the elements BEFORE the last are
    # newline-terminated ("complete") records that iter_jsonl would consume.
    complete = lines[:-1]
    expected_seq = 0
    for i, line in enumerate(complete):
        s = line.strip()
        if not s:
            continue
        # A line is "good" only if `read_all` would ACCEPT it — i.e. it passes the shared line rule
        # AND is a constructible `Event`. read_all stops not just at non-JSON/non-dict lines but ALSO
        # at a dict-valid line that fails `Event(**o)` (a byte-flip that renames a required key like
        # `type`, or makes `data` a non-dict). Checking only `isinstance(..., dict)` here was strictly
        # weaker than read_all's stop condition, so such a corruption dropped the tail on read yet
        # went UNDETECTED — defeating the fail-closed guard that gates on this (review of arch-review
        # §3 P0-4). This walk shares `decode_jsonl_line` with the readers but deliberately NOT their
        # stop: it must continue PAST the bad line to count what they would drop.
        try:
            obj = decode_jsonl_line(s)   # never None: blank lines were skipped above
        except JsonlRecordInvalid:
            ok = False
        else:
            try:
                events = _decode_event_record(obj)
            except Exception:  # noqa: BLE001 — a dict that isn't a valid Event is where read_all stops
                ok = False
            else:
                ok = event_sequence_continues(events, expected_seq)
        if not ok:
            dropped = sum(1 for later in complete[i + 1:] if later.strip())
            return {"good_records": sum(1 for e in complete[:i] if e.strip()),
                    "corrupt_line": i + 1, "dropped_lines": dropped}
        expected_seq = events[-1].seq + 1
    return None


# The ONE operator-facing statement about whether a log's readable prefix IS the log. `log_divergence`
# above answers the WRITER's question ("may I append?"); this answers the READER's ("is what I am
# about to show you the run?"), and until 2026-08-14 nothing asked it on a display path.
#
# The cost of not asking, measured on the shipped corpus: `runs/rubertlite-dense-retrieval` has 1,624
# physical rows, ALL of them valid JSON, and `read_all()` returns 20 — seqs 20-24 are absent, so the
# dense-prefix fence ends the recoverable prefix at line 21 and 1,603 records are dropped from every
# fold. `EventStore` computed exactly that divergence and it went nowhere an operator looks: on the
# same server in the same second the timeline pager reported `total_events: 1624, torn_tail: false`
# while `/state` reported `event_count: 20`, and the run-list row read `nodes: 2, best_metric: 0.8077`
# for a run whose own `budget` row says `nodes: 81`. Every one of those numbers is arithmetically
# correct on 1.2 % of the run and none of them said so.
#
# Two rules this receipt keeps (docs/36, and invariant #5 one file over):
#   * it is DERIVED FROM THE BYTES and never inferred. There is no heuristic here and no softening —
#     the whole body is `log_divergence`, which reads the file. An unreadable file is INCOMPLETE with
#     `unreadable: True`, because "we could not look" must never render as "we looked and it is fine".
#   * it is a STATEMENT, not a fence. It reads nothing the fence does not already stop at and it
#     changes no reader's stop condition, so every log that loads today still loads identically —
#     including the ten runs whose `read_all()` legitimately returns MORE records than physical lines
#     (an `append_many` envelope carries several events on one line).
INTEGRITY_COMPLETE = {"complete": True}
# The keys a WIRE receipt always carries, so a client never has to tell "absent because healthy" from
# "absent because this server is older" — the latter has no `source_integrity` object at all. Stated
# once because four HTTP surfaces publish this (`/state`, `/lifecycle`, the run list, `/cost` and
# `/log-page`) and two envelopes describing the SAME file in two shapes is the original defect's own
# signature. `serve/routers/runs.py::RunSourceIntegrity` declares exactly this set.
INTEGRITY_WIRE_FIELDS = ("complete", "good_records", "corrupt_line", "dropped_lines", "unreadable")


def log_integrity(path: str | os.PathLike) -> dict:
    """Whether the recoverable prefix of an event log is the WHOLE log, for a display surface.

    Returns `{"complete": True}`, or `{"complete": False, ...}` carrying `good_records` (how many
    records a reader gets), `corrupt_line` (where it stops) and `dropped_lines` (how many complete
    records are on disk BEHIND that boundary and invisible to every fold). `unreadable: True` marks
    the file we could not scan at all — the conservative direction, never `complete`.

    Callers must treat an incomplete receipt as "we cannot show you this run", not as a footnote on
    numbers they print anyway: `good_records` is the denominator every derived figure is really over.
    """
    try:
        div = log_divergence(path)
    except (OSError, ValueError, TypeError):
        return {"complete": False, "unreadable": True}
    if div is None:
        return dict(INTEGRITY_COMPLETE)
    return {"complete": False, "good_records": div.get("good_records"),
            "corrupt_line": div.get("corrupt_line"), "dropped_lines": div.get("dropped_lines")}


def integrity_wire(receipt: dict) -> dict:
    """Widen a receipt to the fixed wire shape (`INTEGRITY_WIRE_FIELDS`), absent keys as null."""
    return {field: receipt.get(field) for field in INTEGRITY_WIRE_FIELDS}


def integrity_sentence(receipt: Optional[dict], *, run_label: str = "this run") -> str:
    """The ONE wording every TEXT surface prints for an incomplete log, or "" when it is complete.

    Spelled once so the CLI, the tool surfaces and any future reader cannot come to describe the same
    bytes differently. Phrased so a reader cannot convert a truncated read into an ABSENCE claim —
    the specific failure `tools/_runcache.py::source_note` already guards for an in-loop agent, and
    the identical one an operator makes when a 20-event view of an 81-node run reads as a small run.
    """
    if not receipt or receipt.get("complete"):
        return ""
    if receipt.get("unreadable"):
        return (f"[INCOMPLETE RECORD] {run_label}'s event log could not be read at all; nothing "
                f"below describes the run.")
    good = receipt.get("good_records")
    dropped = receipt.get("dropped_lines")
    line = receipt.get("corrupt_line")
    # good + the BOUNDARY row + dropped. The boundary line is itself a complete record on disk, so
    # leaving it out would state a denominator one short of the file — and the timeline pager, which
    # counts physical rows, would print a different total for the same bytes. Two surfaces disagreeing
    # about the size of the log is the original defect in miniature.
    total = (good + dropped + 1) if isinstance(good, int) and isinstance(dropped, int) else None
    scope = f"{good} of {total} records" if total else f"{good} record(s)"
    return (f"[INCOMPLETE RECORD] {run_label}'s event log stops being readable at line {line}: only "
            f"{scope} are visible to replay and {dropped} durable record(s) behind that boundary are "
            f"NOT included. Everything reported here describes the readable prefix ONLY — it is not "
            f"evidence that the rest did not happen. `looplab repair-log` names the boundary.")


def repair_log(path: str | os.PathLike) -> dict:
    """Operator recovery for a MID-FILE divergence (see `EventLogCorruptionError`). Idempotent:
    returns `{}` (no-op) when the log has no divergence. Otherwise it (1) backs up the ORIGINAL bytes
    to `events.jsonl.corrupt-<ts>.bak` (never destroy evidence — the dropped tail may be salvageable by
    hand), (2) atomically truncates the log to its last valid boundary — exactly the recoverable prefix
    `iter_jsonl`/`fold` already consume — and (3) records the repair provenance as a `log_repaired`
    diagnostic event (unfolded) appended to the now-clean log. Returns the repair record.

    Detection, backup and truncation happen under the event log's OWN lock — the one every appender
    takes — and the divergence is re-derived INSIDE it. Deriving the boundary from a pre-lock read
    and then writing the file back is a read/derive/replace with a window in it: an append that lands
    in that window is authoritative and durable, and the replace would erase it. (Requiring the run
    to be offline is the CLI's job — `repair-log` refuses while the engine holds engine.lock; this
    lock is the last-resort fence for anything that repairs a log another writer can still reach.)"""
    p = Path(path)
    from looplab.core.atomicio import atomic_write_bytes
    with _interprocess_lock(Path(str(p) + ".lock"), required=True):
        assert_run_reset_write_allowed(p.parent)
        assert_run_deletion_write_allowed(p.parent)
        # Authoritative INSIDE the lock. A caller's earlier peek proves nothing: the log may have been
        # repaired, extended past the divergence, or replaced entirely while we waited for the lock.
        div = log_divergence(p)
        if div is None:
            return {}
        raw = p.read_bytes()
        ts_ns = time.time_ns()
        ts = ts_ns // 1_000_000_000
        # Nanosecond suffix: two repairs in the same second must never overwrite forensic evidence.
        backup = p.with_name(p.name + f".corrupt-{ts_ns}.bak")
        backup.write_bytes(raw)  # full original preserved before we truncate
        # Truncate to the last valid boundary: keep every COMPLETE line before the first corrupt one.
        # Those lines are newline-terminated, so re-joining reproduces the exact bytes iter_jsonl
        # would have read.
        lines = raw.split(b"\n")
        keep = lines[: div["corrupt_line"] - 1]
        truncated = (b"\n".join(keep) + b"\n") if keep else b""
        atomic_write_bytes(p, truncated)
    record = {"backup": backup.name, "corrupt_line": div["corrupt_line"],
              "dropped_lines": div["dropped_lines"], "good_records": div["good_records"], "ts": ts}
    # The log is clean now, so a fresh store folds/appends without tripping the fail-closed guard.
    # OUTSIDE the block above because `append` takes the very same lock — and it is safe there: the
    # file is already valid, so a writer that slips in first simply appends ahead of the marker.
    from looplab.events.types import EV_LOG_REPAIRED
    EventStore(p).append(EV_LOG_REPAIRED, record, trace_id=None, span_id=None)
    return record


# How much of the cached prefix the continuity anchor covers at each end. Bounded so the check costs
# a fixed two reads no matter how large the log grows, and only when the file actually changed.
_CACHE_ANCHOR_BYTES = 64 * 1024


def prefix_anchor_from_handle(handle, consumed: int) -> Optional[tuple[bytes, bytes]]:
    """A bounded fingerprint of the first and last `_CACHE_ANCHOR_BYTES` of a consumed prefix, for a
    caller that already holds an open handle (the UI's log pager). Restores the handle's position so
    it composes with an in-progress scan.

    WHAT THIS PROVES, exactly: that the head and the tail of the bytes we already parsed are still the
    bytes we parsed. It is a detector, not a proof of whole-prefix continuity — a surgical edit that
    changes only the middle, preserves both windows AND preserves dense seq numbering would still pass.
    Digesting the entire prefix would be a proof, but it is O(log size) on every read and this cache
    exists precisely to keep reads O(new bytes).

    The two windows are chosen for the failure this closes: a prefix REWRITE, whether the file was
    rebuilt from the start (head moves) or edited up to the append point (tail moves). Growth alone
    proved neither, so a rewrite-then-append landed a fresh tail on stale cached Events and the
    process silently diverged from disk.

    `EventStore.read_all` keeps the same two windows (as raw bytes it can extend in memory, so a
    poll costs one bounded read instead of a re-hash of the whole prefix) but does NOT rely on them
    alone: it still runs the full-prefix sha256 proof whenever the prefix has doubled since that
    proof last ran. See the comment on that check for why both arms are needed."""
    if consumed <= 0:
        return None
    where = handle.tell()
    try:
        window = min(_CACHE_ANCHOR_BYTES, consumed)
        handle.seek(0)
        head = handle.read(window)
        handle.seek(consumed - window)
        tail = handle.read(window)
    except OSError:
        return None
    finally:
        try:
            handle.seek(where)
        except OSError:
            pass
    if len(head) < window or len(tail) < window:
        return None                       # the file no longer holds the prefix we claim to have read
    return (hashlib.sha256(head).digest(), hashlib.sha256(tail).digest())



def _parse_jsonl_region(buf: bytes) -> tuple[list[tuple[dict, int]], int]:
    """Parse complete records from a byte buffer, applying `iter_jsonl`'s EXACT durability rules
    (stop at the first torn/blank-then-nonterminated/corrupt line), and report how many bytes were
    consumed (always a newline boundary). Each record is paired with the byte offset consumed
    through the end of its line, so a caller that rejects a record can rewind to the exact
    boundary before it. This is the incremental core shared by the read cache: the set of records
    it yields for a full-file buffer is identical to `iter_jsonl`, so caching can never change
    what `read_all` returns — it only avoids re-reading+re-parsing bytes already seen."""
    records, consumed = scan_jsonl_region(buf)
    # `scan_jsonl_region` reports the newline's OWN offset; this caller's contract is the boundary
    # AFTER the line, which is what it rewinds to when it rejects a record.
    return [(obj, end + 1) for obj, _start, end in records], consumed


def retry_tail_cas(store, plan, *, attempts: int = 64, on_exhaust):
    """Run one append `plan` against a freshly-read log tail until it lands, or the log stops moving.

    Eight hand-rolled copies of this loop used to live in the engine (doc 25 ES-07). Each re-read the
    log, computed `tail = events[-1].seq if events else -1`, appended with `expected_last_seq=tail`,
    and swallowed `EventStoreConcurrencyError` to retry — but each also re-decided the EXHAUSTION
    behaviour independently, so `_drop_card_once` raised while `_append_rung_promotion` silently
    returned False. That divergence was accidental, not designed. Here the policy is a REQUIRED
    argument: a caller must state what "the log moved 64 times under me" means for its operation.

    `plan(events, tail)` is called with the snapshot it must decide from, and returns this function's
    result. Two things make it a valid CAS attempt rather than an ordinary callback:

      * it must pass exactly the `tail` it was given as `expected_last_seq`, so the append either
        lands directly on the prefix it validated or loses;
      * it must let `EventStoreConcurrencyError` propagate — that exception IS the retry signal. A
        plan that catches it turns a lost race into a silent success.

    Everything else stays the caller's: precondition/idempotency checks simply `return` their own
    value from inside the plan (no retry), and any lock discipline is chosen by wrapping either this
    call or just the append inside the plan, exactly as before.
    """
    for _attempt in range(attempts):
        events = store.read_all()
        tail = events[-1].seq if events else -1
        try:
            return plan(events, tail)
        except EventStoreConcurrencyError:
            continue
    return on_exhaust()


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
        self._cache_mtime_ns: Optional[int] = None
        self._cache_ctime_ns: Optional[int] = None
        self._cache_identity: Optional[tuple[int, int]] = None
        self._cache_hasher = hashlib.sha256()
        # Successful writes from this EventStore carry an exact fstat receipt. That common path can
        # extend the incremental cache without re-hashing its prefix; externally observed growth
        # must prove the cached prefix still matches disk before joining a new tail to it.
        self._trusted_growth_stat: Optional[tuple] = None
        # Cheap arm of that check, for a store that only READS a log another process writes (the UI
        # server tailing the engine, the engine observing UI control appends): the first and last
        # `_CACHE_ANCHOR_BYTES` of the consumed prefix, kept as RAW bytes so an extension updates
        # them in memory and a poll costs one bounded read instead of re-hashing the whole prefix.
        # Seeded LAZILY from the full-prefix read the first external observation does anyway, so a
        # writer — which never observes external growth — never allocates them. `_full_verified_bytes`
        # records the prefix length that full-prefix proof last covered.
        self._prefix_head: Optional[bytes] = None
        self._prefix_tail: Optional[bytes] = None
        self._full_verified_bytes = 0
        # Latch for `_publish_dir_entry`: the log's directory entry only needs a strict sync from the
        # append that CREATES the file, and `strict_fsync` is a process-wide single-flight.
        self._dir_entry_published = False
        # The abort watcher (and, under eval_parallel fan-out, several concurrent watchers) call read_all()
        # from worker THREADS while the main loop may also read — guard the cache top-up so a
        # concurrent extend/offset update can't race into a corrupt cache.
        self._read_lock = threading.Lock()
        # Serialize appends WITHIN this process. The interprocess flock already serializes across
        # processes (UI server + engine) AND, via a fresh fd per call, across threads — but it
        # DEGRADES TO A NO-OP where flock is unavailable (some FUSE/S3 mounts). Since the engine can
        # now append from a worker thread (concurrent deep-research memo) while the main loop appends,
        # this intra-process lock keeps seq-derivation + `_seq` update atomic even when the flock is a
        # no-op — no torn line / duplicate seq. Held OUTSIDE the flock (consistent order, no deadlock).
        self._append_lock = threading.Lock()
        self._divergence: Optional[dict] = None
        self._seq = self._scan_last_seq()
        # Fail closed on a MID-FILE divergence (a corrupt COMPLETE line followed by MORE records —
        # a FUSE/NFS/S3 mount can flip a middle byte; a single local writer never can). read_all()
        # stops at it, so a later append is durable-but-invisible to fold (arch-review §3 P0-4).
        # Seed the diagnostic here; incremental read_all revalidates changed bytes before each
        # append, so corruption introduced after construction also fails closed without rescanning
        # unchanged history. Reads keep returning the recoverable prefix for repair/inspection.
        self._divergence = log_divergence(self.path) or self._divergence

    @property
    def divergence(self) -> Optional[dict]:
        """The complete-record corruption currently detected, or None (see the error class)."""
        return self._divergence

    @contextmanager
    def paid_effect_guard(self, *, required: bool = True) -> Iterator[None]:
        """Serialize a paid effect's claim, dispatch, and terminal observation.

        This lock is intentionally separate from the append lock file: callers must be able to
        append the durable claim and provider receipt while holding it.  ``required=True`` makes an
        unsupported advisory-lock filesystem fail closed instead of silently allowing two buyers.
        Free reconciliation may pass ``required=False``: it waits for a live buyer where locking is
        supported, while safely degrading when a required buyer could not have started there.
        """
        # a durable marker alone prevents crash replay, but it does not stop two live
        # processes that both observed the marker as absent.  Hold this required interprocess guard
        # across the complete paid-attempt window; EventStore.append uses its own distinct lock.
        with _interprocess_lock(
            Path(str(self.path) + ".paid-effects.lock"),
            required=required,
        ):
            yield

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
        for raw in reversed([line for line in tail.split(b"\n") if line.strip()]):
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "seq" in obj:
                return int(obj["seq"])
        # Tail window missed the last seq (e.g. a >64KB final line with no newline in the window):
        # fall back to a full scan. Non-recursive.
        # DEFENSIVE, not load-bearing: `_scan_last_seq` is `for e in self.read_all(): last = e.seq` —
        # the same source that sets `self._seq` — and every `append` calls `read_all()` under the
        # append lock immediately before `max(self._seq, self._disk_last_seq())`. So this can never
        # exceed `self._seq`, and returning -1 here is observationally identical (verified by
        # mutation). Keep it anyway: it makes the helper correct on its own terms for any future
        # caller that has NOT just refreshed, which is the only way it could ever matter.
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

    def _publish_dir_entry(self, existed: bool) -> None:
        """Durably publish the log's directory ENTRY, once, for the append that created the file.

        `fsync(file)` confirms contents but not the parent directory entry that makes a
        first-created paid-work claim discoverable after power loss. Only the CREATING append needs
        this: once the entry is synced it stays durable, and every later append is covered by the
        content sync alone.

        Deliberately not unconditional. `strict_fsync` is process-wide SINGLE-FLIGHT and spawns a
        worker thread per call, so syncing the directory on every durable append would double the
        traffic through that serialized resource on the hot claim path for no added durability.
        """
        if existed or self._dir_entry_published:
            self._dir_entry_published = True
            return
        strict_fsync_parent(self.path)
        self._dir_entry_published = True

    def _uncertain_append_needs_reservation(self, seq: int) -> bool:
        """Whether an append that failed mid-sync left bytes only a SEQ RESERVATION can fence.

        Three outcomes, and only one of them needs the reservation:
        * the complete record landed — `_disk_last_seq` sees it, which already prevents reuse, so
          reserving adds nothing;
        * nothing landed — the buffer never reached the file, and reserving would open a durable gap;
        * a TORN partial landed — invisible to `_disk_last_seq` (it skips undecodable lines) yet
          occupying that seq, so the reservation is the only thing stopping the next append from
          writing a DUPLICATE seq behind the partial line.

        A complete append always ends in a newline, so "the file does not end in one" is exactly the
        torn case. An unreadable file answers True: reserving is the conservative direction.
        """
        try:
            if self._disk_last_seq() >= seq:
                return False
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    return False
                f.seek(-1, os.SEEK_END)
                return f.read(1) != b"\n"
        except OSError:
            return True

    def _mark_uncertain_append(self, seq: int) -> None:
        """Fence a seq whose bytes were accepted but whose sync was unconfirmed.

        A failed strict fsync does not roll back the preceding write+flush: the complete line is
        normally already visible even though the caller must fail closed before paid work. Reserve
        that possibly-written seq and discard the incremental view so the next read reparses disk
        truth instead of letting this long-lived store reuse the seq.
        """
        with self._read_lock:
            # Reserve ONLY when bytes may actually be on disk. `f.write` is BUFFERED, so a flush or
            # fsync that raised (ENOSPC, EIO) can mean NOTHING reached the file — and reserving then
            # made this store's next append write seq+1 onto a tail still at seq-1. The restored dense
            # fence (`event_sequence_continues`) classifies that gap as corruption: the next append
            # RETURNS SUCCESS while its event is invisible to the fold, and every append after it
            # raises EventLogCorruptionError. A self-inflicted brick, and `append_many` passes
            # `events[-1].seq`, so it could reserve up to 4096 seqs at once.
            #
            # Disk truth decides, in `_uncertain_append_needs_reservation`.
            if self._uncertain_append_needs_reservation(seq):
                self._seq = max(self._seq, seq)
            self._cache = []
            self._cache_bytes = 0
            self._cache_mtime_ns = None
            self._cache_ctime_ns = None
            self._cache_identity = None
            self._cache_hasher = hashlib.sha256()
            self._trusted_growth_stat = None
            # Both prefix witnesses describe the view we just discarded; keep them in step with
            # `_cache_bytes` or the next external observation would compare against other bytes.
            self._prefix_head = None
            self._prefix_tail = None
            self._full_verified_bytes = 0
            self._divergence = None

    def _locked_append(self, build, *, expected_last_seq: "int | None",
                       require_lock: bool, require_durable: bool):
        """Own the ONE critical section both public appenders need; return what ``build`` produced.

        `append` and `append_many` used to carry a verbatim copy of this body (doc 25 EV-06), so
        every rule below — the reset/deletion write fences, the fail-closed divergence check, the
        torn-tail heal, the two-source seq derivation, the explicit-seq CAS, the accepted-bytes
        reservation on a failed sync, the directory-entry publish, and the cache/stat bookkeeping —
        existed twice. That is the shape where a durability fix lands on one spelling and is
        silently absent from the other; both copies had already been extended several times.

        ``build(cur)`` is called INSIDE the lock, after the tail seq is derived and the CAS has
        passed, and returns ``(payload_bytes, last_logical_seq, result)``:

        * ``payload_bytes`` — the exact bytes to append, INCLUDING the terminating newline (the
          torn-tail contract is a per-record newline, so a builder that omits it writes a record
          every reader stops at);
        * ``last_logical_seq`` — the highest logical seq those bytes carry. This is what ``self._seq``
          advances to and what an uncertain-sync reservation must fence; for a batch it is the LAST
          member's seq, not the envelope's position.
        * ``result`` — this method's return value, untouched.

        It cannot run BEFORE the lock, which is why the doc's "pre-serialized payload" shape is a
        callback here: `cur` is only knowable inside the critical section, and a payload serialized
        against a tail read outside it would carry a seq another writer already used.
        """
        with self._append_lock, _interprocess_lock(
                Path(str(self.path) + ".lock"), required=require_lock):
            # Reset publishes its marker while owning this SAME append lock.  Checking inside the
            # critical section closes marker-check -> append races, and the replacement engine is
            # admitted only when it inherited the exact operation id.
            assert_run_reset_write_allowed(self.path.parent)
            assert_run_deletion_write_allowed(self.path.parent)
            # Revalidate bytes written or replaced since the last read WHILE holding the writer lock.
            # A construction-time snapshot is insufficient on FUSE/network mounts: corruption can
            # appear mid-run, and appending after it would make every new event invisible to replay.
            self.read_all()
            if self._divergence is not None:
                raise EventLogCorruptionError(self.path, self._divergence)
            self._heal_torn_tail()
            # Derive seq from max(in-memory, on-disk tail) so a concurrent writer can't collide.
            # Single-process: _disk_last_seq == self._seq, so seq == self._seq + 1 (unchanged).
            cur = max(self._seq, self._disk_last_seq())
            # P1-12 explicit-seq CAS: reject the append if the tail moved since the caller read state.
            # Inside the critical section, so the check + write are atomic against another writer.
            if expected_last_seq is not None and cur != expected_last_seq:
                raise EventStoreConcurrencyError(self.path, expected_last_seq, cur)
            payload, last_logical_seq, result = build(cur)
            accepted = False
            written_stat = None
            existed = os.path.exists(self.path)
            try:
                with open(self.path, "ab") as f:
                    f.write(payload)
                    # From here onward the record may reach disk even if flush, sync, or close
                    # reports failure. Reserve its seq on every exceptional exit.
                    accepted = True
                    f.flush()
                    # `require_durable` syncs the file CONTENTS, and `_publish_dir_entry` below adds
                    # the parent DIRECTORY entry the first time it is needed. Content-only was a real
                    # durability gap: when the durable append is the one that created events.jsonl —
                    # or an earlier best-effort append created the link and it was never flushed — a
                    # power loss can lose the WHOLE FILE even though the record's bytes were strictly
                    # synced, which is exactly the "paid_work_claimed survives a crash before the
                    # external side effect" contract this flag exists for. A durable paid claim could
                    # vanish and be re-billed. `strict_atomic_write_bytes` has always paired the two.
                    if require_durable:
                        strict_fsync(f.fileno())
                    else:
                        best_effort_fsync(f.fileno())  # read tolerates a torn final line
                    written_stat = os.fstat(f.fileno())
            except BaseException:
                if accepted:
                    self._mark_uncertain_append(last_logical_seq)
                raise
            if require_durable:
                self._publish_dir_entry(existed)
            self._seq = last_logical_seq
            if written_stat is not None:
                self._trusted_growth_stat = (
                    written_stat.st_dev, written_stat.st_ino, written_stat.st_size,
                    written_stat.st_mtime_ns, written_stat.st_ctime_ns)
            # Keep cache bytes + file identity synchronized with our own successful write. Without
            # this top-up, a store that appended but had not yet read the new record could retain
            # `_cache_identity=None`; replacing its one-record log with an empty file would then look
            # indistinguishable from the original pre-create state and an OLD expected seq could pass.
            # Incremental read makes this O(the single new record), not a full rescan.
            self.read_all()
        return result

    def append(self, type: str, data: dict[str, Any], *,
               trace_id: "str | None" = _UNSET_TRACE, span_id: "str | None" = _UNSET_TRACE,
               expected_last_seq: "int | None" = None, require_lock: bool = False,
               require_durable: bool = False) -> Event:
        """Append one event and return its assigned envelope.

        Durability and lock enforcement are best effort unless ``require_durable`` and/or
        ``require_lock`` are true.

        Under a best-effort cross-process lock (the UI server and the engine write the SAME
        events.jsonl): first heal a torn tail (truncate a final line without a trailing
        newline so this record can't glue onto a partial write), then derive the next seq
        from max(in-memory, on-disk tail) so a concurrent writer can't mint a duplicate.
        The record is written as one line + flush + best-effort fsync — fsync failures
        (FUSE/S3) never abort the engine, because reads tolerate a torn final line. Once a write is
        accepted its seq stays reserved; a required sync failure still raises, invalidates the read
        cache, and prevents the caller from treating the record as a durable paid-work claim.

        `expected_last_seq` (P1-12 explicit-seq CAS): when given, the append lands ONLY if the log
        tail is still exactly that seq at the moment we hold the lock — else raise
        `EventStoreConcurrencyError`. The check + append are one critical section, so a caller that
        read state at seq N can guarantee no other event slipped in before its intent (optimistic
        concurrency for a UI control raised on a possibly-stale view). None = today's behavior.

        ``require_lock`` is deliberately opt-in so existing engine behavior is unchanged.  Versioned
        collaboration enables it and fails visibly if the cross-process lock cannot be acquired.

        ``require_durable`` fails unless fsync succeeds within the configured deadline. Paid
        external work uses it before starting; ordinary logging remains FUSE-friendly best effort.
        """
        if type == _EVENT_BATCH_TYPE:
            raise ValueError("the internal event batch type is reserved")
        # Stamp the event with the active span's (trace_id, span_id) so the UI can join events to the
        # trace — UNLESS the caller passes an EXPLICIT pair (even None): a telemetry event emitted AFTER
        # its op's span closed (foresight ranking) carries the captured trace_id of that op, and an
        # explicit None means "no trace" (so it never inherits the ambient node/eval trace by accident).
        if trace_id is _UNSET_TRACE:
            trace_id, span_id = current_ids()

        def _build(cur: int) -> tuple[bytes, int, Event]:
            e = Event(seq=cur + 1, ts=time.time(), type=type, data=data,
                      trace_id=trace_id, span_id=span_id)
            return orjson.dumps(e.model_dump(mode="json")) + b"\n", e.seq, e

        return self._locked_append(
            _build, expected_last_seq=expected_last_seq,
            require_lock=require_lock, require_durable=require_durable)

    def append_many(self, records: Sequence[tuple[str, dict[str, Any]]], *,
                    trace_id: "str | None" = _UNSET_TRACE,
                    span_id: "str | None" = _UNSET_TRACE,
                    expected_last_seq: "int | None" = None,
                    require_lock: bool = False,
                    require_durable: bool = False) -> list[Event]:
        """Append a bounded group of events under one tail CAS and writer lock.

        The group is serialized into one bounded internal JSONL envelope while the same interprocess
        lock used by :meth:`append` is held. Readers strictly validate and expand that one record before
        returning Events. Its guarded disk type makes pre-batch readers fail closed. A torn final line
        therefore exposes zero members, while a complete envelope preserves the historical contiguous
        sequences and return value. Another writer can land before the group or after it, never between
        its records. An empty group is a no-op.
        """
        if not records:
            return []
        if len(records) > _MAX_EVENT_BATCH_MEMBERS:
            raise ValueError(f"event batch exceeds {_MAX_EVENT_BATCH_MEMBERS} members")
        if any(event_type == _EVENT_BATCH_TYPE for event_type, _data in records):
            raise ValueError("the internal event batch type is reserved")
        if trace_id is _UNSET_TRACE:
            trace_id, span_id = current_ids()

        def _build(cur: int) -> tuple[bytes, int, list[Event]]:
            events = [
                Event(
                    seq=cur + offset,
                    ts=time.time(),
                    type=event_type,
                    data=data,
                    trace_id=trace_id,
                    span_id=span_id,
                )
                for offset, (event_type, data) in enumerate(records, start=1)
            ]
            envelope = Event(
                seq=events[-1].seq,
                ts=events[-1].ts,
                type=_EVENT_BATCH_TYPE,
                data={
                    "schema": _EVENT_BATCH_SCHEMA,
                    "count": len(events),
                    "first_seq": events[0].seq,
                    "last_seq": events[-1].seq,
                    "events": [event.model_dump(mode="json") for event in events],
                },
                trace_id=trace_id,
                span_id=span_id,
            )
            physical_envelope = envelope.model_dump(mode="json")
            physical_envelope["type"] = list(_EVENT_BATCH_GUARD_TYPE)
            payload = orjson.dumps(physical_envelope) + b"\n"
            if len(payload) > _MAX_EVENT_BATCH_BYTES:
                raise ValueError(f"event batch exceeds {_MAX_EVENT_BATCH_BYTES} serialized bytes")
            # The LAST member's seq, not the envelope's own: one physical record carries `count`
            # logical events, and both `self._seq` and an uncertain-sync reservation must fence the
            # whole run of them or the next append reuses a seq that is already on disk.
            return payload, events[-1].seq, events

        return self._locked_append(
            _build, expected_last_seq=expected_last_seq,
            require_lock=require_lock, require_durable=require_durable)

    def _read_prefix_windows(self, consumed: int) -> Optional[tuple[bytes, bytes]]:
        """The first and last `_CACHE_ANCHOR_BYTES` of the first `consumed` bytes on disk, or None if
        they cannot be taken (the file is shorter than the prefix we claim to hold, or unreadable).
        None fails CLOSED at the call site — it reads as "rewritten", which forces a full rescan."""
        if consumed <= 0:
            return None
        window = min(_CACHE_ANCHOR_BYTES, consumed)
        try:
            with open(self.path, "rb") as f:
                head = f.read(window)
                if consumed > window:            # one read covers both windows on a short prefix
                    f.seek(consumed - window)
                    tail = f.read(window)
                else:
                    tail = head
        except OSError:
            return None
        if len(head) < window or len(tail) < window:
            return None
        return (head, tail)

    def read_all(self) -> list[Event]:
        """Return every Event on disk (up to the first torn/corrupt line), served from an incremental
        cache. Only bytes appended since the previous call are read+parsed; the returned sequence is
        identical to a full event-aware scan, including its monotonic-sequence boundary. Falls back to
        a full rescan if the file shrank/was replaced (a heal-truncate or a fresh file) so the cache can
        never go stale. A historical sequence GAP fails closed — `event_sequence_continues` wants a
        dense prefix (the 5f011a2 fence), so the returned prefix ends AT the gap; duplicate/backward
        rows fail closed the same way. See
        tests/test_events_replay.py::test_monotonic_logical_sequence_gap_fails_closed."""
        with self._read_lock:
            try:
                st = self.path.stat() if self.path.exists() else None
                size = st.st_size if st is not None else 0
                mtime_ns = st.st_mtime_ns if st is not None else None
                ctime_ns = st.st_ctime_ns if st is not None else None
                identity = (st.st_dev, st.st_ino) if st is not None else None
            except OSError:
                size = 0
                mtime_ns = None
                ctime_ns = None
                identity = None
            current_stat = (
                identity[0], identity[1], size, mtime_ns, ctime_ns) if identity is not None else None
            replaced = (self._cache_identity is not None and identity != self._cache_identity)
            same_size_rewrite = (size == self._cache_bytes and self._cache_mtime_ns is not None
                                 and (mtime_ns != self._cache_mtime_ns
                                      or ctime_ns != self._cache_ctime_ns))
            prefix_rewritten = False
            trusted_growth = (
                size > self._cache_bytes
                and current_stat is not None
                and current_stat == self._trusted_growth_stat
            )
            # Growth is "trusted" only for THIS store's own immediately-preceding append, so a store
            # that merely READS a log another process writes (the UI server tailing the engine at
            # POLL_SECONDS, the engine observing UI control appends) has to re-validate the cached
            # prefix on every poll that sees new bytes. Doing that with a full-prefix sha256 EVERY
            # time is O(log size) per external append — O(n^2) over a run, exactly the quadratic cost
            # this cache exists to avoid. So the check has two arms:
            #   * the bounded head/tail windows run on every poll. They catch the failure this guard
            #     exists for — a prefix REWRITE, whether the file was rebuilt from the start (head
            #     moves) or edited up to the append point (tail moves) — at a fixed cost, and the
            #     comparison is over raw bytes, so it is exact for what it covers.
            #   * the full-prefix digest, the actual PROOF, still runs on the first external
            #     observation (nothing is seeded yet) and again whenever the prefix has DOUBLED since
            #     that proof last ran.
            # Doubling keeps total proof work at ~2*N bytes over a run that grows to N — amortized
            # O(1) per appended byte — while bounding how long the one case the windows miss (a
            # middle-only edit that also preserves dense seq numbering) could hide to one doubling.
            # Below `_CACHE_ANCHOR_BYTES` the two windows ARE the whole prefix, so a short log is
            # fully compared on every poll regardless.
            external_growth = (size > self._cache_bytes and self._cache_bytes > 0
                               and not trusted_growth and not replaced)
            if external_growth:
                windows_seeded = self._prefix_head is not None
                proof_due = self._cache_bytes >= 2 * self._full_verified_bytes
                if windows_seeded and not proof_due:
                    observed = self._read_prefix_windows(self._cache_bytes)
                    prefix_rewritten = (observed is None
                                        or observed != (self._prefix_head, self._prefix_tail))
                else:
                    try:
                        with open(self.path, "rb") as f:
                            prefix = f.read(self._cache_bytes)
                        prefix_rewritten = (
                            len(prefix) != self._cache_bytes
                            or hashlib.sha256(prefix).digest() != self._cache_hasher.digest()
                        )
                    except OSError:
                        prefix_rewritten = True
                    if not prefix_rewritten:
                        # Seed (or re-seed) the windows from bytes the proof just validated, so the
                        # cheap arm never has to read them separately.
                        window = min(_CACHE_ANCHOR_BYTES, self._cache_bytes)
                        self._prefix_head = prefix[:window]
                        self._prefix_tail = prefix[len(prefix) - window:]
                        self._full_verified_bytes = self._cache_bytes
            self._trusted_growth_stat = None
            cache_invalidated = (
                size < self._cache_bytes or replaced or same_size_rewrite or prefix_rewritten)
            if cache_invalidated:
                # File shrank, was replaced, or was rewritten in place without changing length. The
                # last case matters on network/FUSE mounts: returning the old cached Events would split
                # the process from disk truth and could hide a newly-corrupt complete record.
                self._cache = []
                self._cache_bytes = 0
                self._cache_hasher = hashlib.sha256()
                self._divergence = None
                # Both prefix witnesses describe bytes we just discarded. Dropping them also sends the
                # next external observation to the proof arm, so a rescan is followed by a full
                # digest rather than by a window comparison against a prefix that no longer exists.
                self._prefix_head = None
                self._prefix_tail = None
                self._full_verified_bytes = 0
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
                expected_seq = self._cache[-1].seq + 1 if self._cache else 0
                for o, end in objs:
                    try:
                        record_events = _decode_event_record(o)
                    except Exception:  # noqa: BLE001
                        break
                    if not event_sequence_continues(record_events, expected_seq):
                        break
                    evs.extend(record_events)
                    expected_seq = record_events[-1].seq + 1
                    ok_bytes = end
                else:
                    ok_bytes = consumed   # all records valid — trailing blanks count too
                self._cache.extend(evs)
                self._cache_hasher.update(new[:ok_bytes])
                self._cache_bytes += ok_bytes
                if self._prefix_head is not None:
                    # Extend the windows over the SAME bytes the digest just consumed, entirely in
                    # memory — the head only grows until it fills, the tail slides. Both stay exact,
                    # because the stored tail already covers the last `_CACHE_ANCHOR_BYTES` (or all)
                    # of the old prefix, which is everything the new tail can reach back into.
                    accepted = new[:ok_bytes]
                    window = min(_CACHE_ANCHOR_BYTES, self._cache_bytes)
                    self._prefix_head = (self._prefix_head + accepted)[:window]
                    self._prefix_tail = (self._prefix_tail + accepted)[-window:]
                remainder = new[ok_bytes:]
                if b"\n" in remainder:
                    # An unconsumed newline means the first rejected record is COMPLETE, not a normal
                    # torn tail. Compute exact line/count detail only on this exceptional path.
                    self._divergence = log_divergence(self.path) or {
                        "good_records": len(self._cache),
                        "corrupt_line": len(self._cache) + 1,
                        "dropped_lines": max(0, remainder.count(b"\n") - 1),
                    }
            self._cache_mtime_ns = mtime_ns
            self._cache_ctime_ns = ctime_ns
            self._cache_identity = identity
            if cache_invalidated and hasattr(self, "_seq"):
                # `_seq` is part of the compare-and-set truth, not merely a next-id optimization.
                # Keeping the pre-replacement high-water mark would let a caller holding that OLD
                # tail pass `expected_last_seq` after a reset (and would mint a large seq gap in the
                # replacement log). Rebase it to the bytes we just reparsed so an old CAS conflicts
                # and a current CAS advances from the current tail. During __init__, `_scan_last_seq()` calls us
                # before `_seq` exists, hence the narrow hasattr guard.
                self._seq = self._cache[-1].seq if self._cache else -1
            # Return a snapshot copy so a caller iterating the result can't be perturbed by a
            # concurrent top-up (and so callers expecting a re-iterable list still work).
            return list(self._cache)

    def readable_horizon(self) -> tuple[int, int]:
        """`(events this store will SERVE, lines on disk)` — how far into this log a reader can see.

        `read_all` returns the log's dense prefix and stops at the first logical-sequence GAP, which
        is the right call for a state fold and is tested (`test_monotonic_logical_sequence_gap_
        fails_closed`). What it cannot do is TELL anyone it stopped, and a bounded reader that does
        not name its bound reads as complete coverage.

        Measured 2026-08-21 on `runs/rubertlite-dense-retrieval`: seq jumps 19 -> 25 at file line 21,
        so the store serves **20 events of 1,624 lines** and a run with 81 `node_created` and 71
        `node_evaluated` rows on disk folds to TWO nodes. Anything built on the fold — both offline
        backfills, any audit, any coverage claim — silently describes that prefix and calls it the
        run. `backfill-applied-params` shipped exactly that way; its "23 events" is the prefix's
        answer, not the corpus's.

        Lives here, beside `read_all`, because it is a property of the STORE rather than of any
        consumer: two copies of "how far can this be read" is the drift `tests/
        test_shared_identity_rules.py` exists to refuse, and the second copy would be written by
        whoever next needed the number.

        Cheap and side-effect free: `read_all` is cache-served, and the line count is one buffered
        pass. An unreadable path answers `(served, 0)` — 0 lines can never be less than `served`, so
        a caller comparing them reports nothing rather than inventing a horizon from a failed stat.
        """
        served = len(self.read_all())
        try:
            with open(self.path, "rb") as fh:
                lines = sum(1 for _ in fh)
        except OSError:
            lines = 0
        return served, lines
