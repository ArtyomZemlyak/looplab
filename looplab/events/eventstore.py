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
from typing import Any, Iterator, Optional

import orjson

from looplab.core.atomicio import best_effort_fsync
from looplab.core.models import Event
from looplab.core.tracing import current_ids

# Sentinel for EventStore.append's trace_id/span_id: distinguishes "not passed" (stamp with the ambient
# span via current_ids) from an EXPLICIT None (a telemetry event that must carry NO trace).
_UNSET_TRACE = object()


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


@contextmanager
def _interprocess_lock(lock_path: Path, *, required: bool = False):
    """Best-effort exclusive cross-process lock (msvcrt on Windows, fcntl on POSIX). The live UI
    server appends control events to the SAME events.jsonl the engine subprocess writes; without
    serialization their appends can interleave into a torn line (which `iter_jsonl` truncates at,
    silently dropping later events). Degrades to a no-op if locking is unavailable."""
    f = None
    locked = False
    try:
        try:
            f = open(lock_path, "a+")
        except OSError as exc:
            if required:
                raise EventStoreLockError(lock_path, exc) from exc
            raise  # preserve the existing engine-writer behavior for an inaccessible lock path
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            locked = True
        # Some filesystems/runtimes expose the module but report an unsupported advisory-lock
        # operation as ValueError/NotImplementedError rather than OSError.  A strict caller must
        # receive one stable failure type for every such capability gap; otherwise the command
        # worker would turn it into a generic 200/command_worker_failed response.
        except (OSError, ImportError, AttributeError, NotImplementedError, ValueError) as exc:
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


def read_jsonl_lenient(path: str | os.PathLike, *, loads=orjson.loads,
                       keep_bad: bool = False, dicts_only: bool = True,
                       errors: str = "strict") -> list:
    """Read a MUTABLE JSONL store (lessons / meta-notes / cases / exploit patterns), SKIPPING
    corrupt lines and continuing. Contrast `iter_jsonl`, which STOPS at the first bad line —
    correct for the append-only event log (a bad line there is a torn tail), wrong for stores
    that are rewritten/compacted in place, where one damaged line must not hide everything
    after it. Previously copy-pasted at ~8 sites (lessons ×4, memory, knowledge/memory tools,
    trust/harden) with drift-prone small variations — the parameters below ARE those variations:

    - `loads`: the parser the store was WRITTEN with — orjson for the orjson-written stores,
      stdlib `json.loads` for the stdlib-written ones. NOT interchangeable: stdlib accepts the
      NaN/Infinity literals stdlib `json.dumps` emits for non-finite floats; orjson rejects them.
    - `keep_bad=True`: emit a None placeholder per bad/blank/non-dict line, so list indices stay
      aligned with RAW file line numbers (the lessons reconcile rewrite and the knowledge-index
      record ids are index-keyed).
    - `dicts_only=False`: keep any parsed JSON value, not just objects (the memory case-library's
      historical reload shape).
    - `errors`: passed to read_text — the spans reader uses "replace" (a mid-file mojibake byte
      must cost one span, not the whole timings report).

    Missing file -> []. An unreadable file raises OSError (callers decide how to degrade)."""
    p = Path(path)
    out: list = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors=errors).splitlines():
        rec = None
        if line.strip():
            try:
                v = loads(line)
                rec = v if (not dicts_only or isinstance(v, dict)) else None
            except Exception:  # noqa: BLE001 — any unparseable line is "damage to step over"
                rec = None
        if rec is not None:
            out.append(rec)
        elif keep_bad:
            out.append(None)
    return out


def write_jsonl_atomic(path: str | os.PathLike, rows, *, dumps=orjson.dumps) -> None:
    """Atomically REWRITE a whole mutable JSONL store (temp + os.replace via core.atomicio):
    one record per line, each line newline-terminated. `dumps` is the serializer the store's
    readers expect (orjson for the lessons store / spans.jsonl, stdlib `json.dumps` for the
    stdlib-written stores — the output bytes differ, so the per-store choice is part of the
    contract; see `read_jsonl_lenient`).

    NEVER route an APPEND-mode site through this (e.g. lessons.append_lessons appends under an
    interprocess lock — a whole-file rewrite there would drop concurrent runs' rows). Callers
    needing cross-process exclusion must hold their store's lock AROUND this call; the write
    itself is only crash-atomic, not concurrency-safe."""
    from looplab.core.atomicio import atomic_write_bytes

    def _line(o) -> bytes:
        d = dumps(o)
        return (d if isinstance(d, bytes) else d.encode("utf-8")) + b"\n"

    atomic_write_bytes(Path(path), b"".join(_line(o) for o in rows))


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
    for i, line in enumerate(complete):
        s = line.strip()
        if not s:
            continue
        # A line is "good" only if `read_all` would ACCEPT it — i.e. it is a valid JSON object AND a
        # constructible `Event`. read_all stops not just at non-JSON/non-dict lines but ALSO at a
        # dict-valid line that fails `Event(**o)` (a byte-flip that renames a required key like `type`,
        # or makes `data` a non-dict). Checking only `isinstance(..., dict)` here was strictly weaker
        # than read_all's stop condition, so such a corruption dropped the tail on read yet went
        # UNDETECTED — defeating the fail-closed guard that gates on this (review of arch-review §3 P0-4).
        try:
            obj = orjson.loads(s)
        except orjson.JSONDecodeError:
            ok = False
        else:
            ok = isinstance(obj, dict)
            if ok:
                try:
                    Event(**obj)
                except Exception:  # noqa: BLE001 — a dict that isn't a valid Event is where read_all stops
                    ok = False
        if not ok:
            dropped = sum(1 for later in complete[i + 1:] if later.strip())
            return {"good_records": sum(1 for e in complete[:i] if e.strip()),
                    "corrupt_line": i + 1, "dropped_lines": dropped}
    return None


def repair_log(path: str | os.PathLike) -> dict:
    """Operator recovery for a MID-FILE divergence (see `EventLogCorruptionError`). Idempotent:
    returns `{}` (no-op) when the log has no divergence. Otherwise it (1) backs up the ORIGINAL bytes
    to `events.jsonl.corrupt-<ts>.bak` (never destroy evidence — the dropped tail may be salvageable by
    hand), (2) atomically truncates the log to its last valid boundary — exactly the recoverable prefix
    `iter_jsonl`/`fold` already consume — and (3) records the repair provenance as a `log_repaired`
    diagnostic event (unfolded) appended to the now-clean log. Returns the repair record."""
    p = Path(path)
    div = log_divergence(p)
    if div is None:
        return {}
    raw = p.read_bytes()
    ts_ns = time.time_ns()
    ts = ts_ns // 1_000_000_000
    # Nanosecond suffix: two repairs in the same second must never overwrite forensic evidence.
    backup = p.with_name(p.name + f".corrupt-{ts_ns}.bak")
    backup.write_bytes(raw)  # full original preserved before we truncate
    # Truncate to the last valid boundary: keep every COMPLETE line before the first corrupt one. Those
    # lines are newline-terminated, so re-joining reproduces the exact bytes iter_jsonl would have read.
    lines = raw.split(b"\n")
    keep = lines[: div["corrupt_line"] - 1]
    truncated = (b"\n".join(keep) + b"\n") if keep else b""
    from looplab.core.atomicio import atomic_write_bytes
    atomic_write_bytes(p, truncated)
    record = {"backup": backup.name, "corrupt_line": div["corrupt_line"],
              "dropped_lines": div["dropped_lines"], "good_records": div["good_records"], "ts": ts}
    # The log is clean now, so a fresh store folds/appends without tripping the fail-closed guard.
    from looplab.events.types import EV_LOG_REPAIRED
    EventStore(p).append(EV_LOG_REPAIRED, record, trace_id=None, span_id=None)
    return record


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
        self._cache_mtime_ns: Optional[int] = None
        self._cache_identity: Optional[tuple[int, int]] = None
        # The abort watcher (and, under max_parallel>1, several concurrent watchers) call read_all()
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

    def append(self, type: str, data: dict[str, Any], *,
               trace_id: "str | None" = _UNSET_TRACE, span_id: "str | None" = _UNSET_TRACE,
               expected_last_seq: "int | None" = None, require_lock: bool = False) -> Event:
        """Durably append one event and return it (the envelope with its assigned seq).

        Under a best-effort cross-process lock (the UI server and the engine write the SAME
        events.jsonl): first heal a torn tail (truncate a final line without a trailing
        newline so this record can't glue onto a partial write), then derive the next seq
        from max(in-memory, on-disk tail) so a concurrent writer can't mint a duplicate.
        The record is written as one line + flush + best-effort fsync — fsync failures
        (FUSE/S3) never abort the engine, because reads tolerate a torn final line.
        `_seq` advances only AFTER the durable write succeeds.

        `expected_last_seq` (P1-12 explicit-seq CAS): when given, the append lands ONLY if the log
        tail is still exactly that seq at the moment we hold the lock — else raise
        `EventStoreConcurrencyError`. The check + append are one critical section, so a caller that
        read state at seq N can guarantee no other event slipped in before its intent (optimistic
        concurrency for a UI control raised on a possibly-stale view). None = today's behavior.

        ``require_lock`` is deliberately opt-in so existing engine behavior is unchanged.  Versioned
        collaboration enables it and fails visibly if the cross-process lock cannot be acquired.
        """
        # Stamp the event with the active span's (trace_id, span_id) so the UI can join events to the
        # trace — UNLESS the caller passes an EXPLICIT pair (even None): a telemetry event emitted AFTER
        # its op's span closed (foresight ranking) carries the captured trace_id of that op, and an
        # explicit None means "no trace" (so it never inherits the ambient node/eval trace by accident).
        if trace_id is _UNSET_TRACE:
            trace_id, span_id = current_ids()
        with self._append_lock, _interprocess_lock(
                Path(str(self.path) + ".lock"), required=require_lock):
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
            seq = cur + 1
            e = Event(seq=seq, ts=time.time(), type=type, data=data,
                      trace_id=trace_id, span_id=span_id)
            line = orjson.dumps(e.model_dump(mode="json"))
            with open(self.path, "ab") as f:    # advance _seq only AFTER a durable write succeeds
                f.write(line + b"\n")
                f.flush()
                best_effort_fsync(f.fileno())   # FUSE/S3 fsync may raise/throttle — must not abort the
                #                                 engine mid-run (read tolerates a torn final line)
            self._seq = seq
            # Keep cache bytes + file identity synchronized with our own successful write. Without
            # this top-up, a store that appended but had not yet read the new record could retain
            # `_cache_identity=None`; replacing its one-record log with an empty file would then look
            # indistinguishable from the original pre-create state and an OLD expected seq could pass.
            # Incremental read makes this O(the single new record), not a full rescan.
            self.read_all()
        return e

    def read_all(self) -> list[Event]:
        """Return every Event on disk (up to the first torn/corrupt line), served from an incremental
        cache. Only bytes appended since the previous call are read+parsed; the returned sequence is
        byte-for-byte identical to a full `iter_jsonl` scan. Falls back to a full rescan if the file
        shrank/was replaced (a heal-truncate or a fresh file) so the cache can never go stale."""
        with self._read_lock:
            try:
                st = self.path.stat() if self.path.exists() else None
                size = st.st_size if st is not None else 0
                mtime_ns = st.st_mtime_ns if st is not None else None
                identity = (st.st_dev, st.st_ino) if st is not None else None
            except OSError:
                size = 0
                mtime_ns = None
                identity = None
            replaced = (self._cache_identity is not None and identity != self._cache_identity)
            same_size_rewrite = (size == self._cache_bytes and self._cache_mtime_ns is not None
                                 and mtime_ns != self._cache_mtime_ns)
            cache_invalidated = size < self._cache_bytes or replaced or same_size_rewrite
            if cache_invalidated:
                # File shrank, was replaced, or was rewritten in place without changing length. The
                # last case matters on network/FUSE mounts: returning the old cached Events would split
                # the process from disk truth and could hide a newly-corrupt complete record.
                self._cache = []
                self._cache_bytes = 0
                self._divergence = None
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
            self._cache_identity = identity
            if cache_invalidated and hasattr(self, "_seq"):
                # `_seq` is part of the compare-and-set truth, not merely a next-id optimization.
                # Keeping the pre-replacement high-water mark would let a caller holding that OLD
                # tail pass `expected_last_seq` after a reset (and would mint a large seq gap in the
                # replacement log). Rebase it to the bytes we just reparsed so an old CAS conflicts
                # and a current CAS continues densely. During __init__, `_scan_last_seq()` calls us
                # before `_seq` exists, hence the narrow hasattr guard.
                self._seq = self._cache[-1].seq if self._cache else -1
            # Return a snapshot copy so a caller iterating the result can't be perturbed by a
            # concurrent top-up (and so callers expecting a re-iterable list still work).
            return list(self._cache)
