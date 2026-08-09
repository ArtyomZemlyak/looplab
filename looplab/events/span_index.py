"""Light span index (perf): make the trace views O(structure) instead of O(all-span-bytes).

`spans.jsonl` is the execution-detail payload the trace views read (ADR-17). On a long run it is
DOMINATED by heavy generation I/O — each generation span carries the full re-sent message history
plus its prompt/output/reasoning, so a 1 GB run is ~90 % `input`/`output`/`thinking` bytes. But the
run-level timeline (`build_trace_view(light=True)`) drops all of that; it needs only the light
structure (ids, kind, name, timing, token usage). Reading+parsing the whole 1 GB just to throw 90 %
away is what made opening the trace stall ~15 s (measured: cold `load_spans` of a 476 MB file = 6.4 s,
mostly disk read of bytes the light view discards).

This module keeps a compact **light span index** derived from `spans.jsonl` — the same projection
`traceview._strip_span_io` produces (span minus `input`/`output`/`thinking`), ~30× smaller — plus the
byte `(offset, length)` of each span's line in `spans.jsonl`. So:

  * the timeline reads only the tiny index (16 MB vs 476 MB → sub-second cold);
  * per-span I/O (`/spans/{sid}`) and per-node/-trace detail seek to exact byte offsets instead of
    scanning the whole file.

Built INCREMENTALLY (parse only bytes appended since last read — mirrors
`eventstore._parse_jsonl_region`), cached in-process, and PERSISTED atomically to `spans.index.jsonl`
so a cold/restarted server reads 16 MB, not 1 GB. It is STRICTLY an accelerator: any validation
failure (identity/size mismatch, corruption, offset drift, missing file) falls back to a full rebuild.
Records are the versioned, bounded/redacted trace projection rather than raw durable dictionaries;
`spans.jsonl` remains the sole source of truth and the only place full diagnostics live.
"""
from __future__ import annotations

import errno
import hashlib
import os
import threading
import weakref
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import orjson

from looplab.core.atomicio import atomic_write_bytes
from looplab.core.run_deletion import RunDeletionStorageError, load_run_deletion_fence
from looplab.core.run_reset import RunResetStorageError, load_run_reset_marker
from looplab.core.trace_append import (
    SPAN_APPEND_JOURNAL_MAX_BYTES, SPAN_APPEND_JOURNAL_NAME,
    SPAN_APPEND_RECEIPT_SCHEMA)
from looplab.core.trace_files import (
    TRACE_JSONL_ROW_MAX_BYTES, assert_private_trace_file,
    iter_bounded_trace_jsonl_lines as _iter_bounded_trace_jsonl_lines,
    open_private_trace_file, trace_file_identity)
from looplab.events.eventstore import (
    JsonlRecordInvalid, _interprocess_lock, decode_jsonl_line, scan_jsonl_region)
from looplab.events.traceview import (
    _normalize_span, _strip_span_io, effective_node_id, trace_root_generation,
    trace_root_node_id)

# Bump when the persisted record shape changes so an old `spans.index.jsonl` is ignored (rebuilt),
# never mis-read. The index is a cache — a version skew simply triggers one rebuild.
_SCHEMA = 9
_INDEX_NAME = "spans.index.jsonl"
_INDEX_LOCK_NAME = ".spans-index.lock"
# Geometric re-persist factor (see `_persist`): re-write the persisted index only when the indexed
# span bytes have grown by this factor since the last write. Bounds a live run's total index-write
# volume to ~O(n) (a handful of full-object PUTs on S3/geesefs) instead of ~O(n²) full rewrites every
# few MB. The first index (from covers 0) always persists.
_PERSIST_GROWTH = 1.5
# Bound the in-process cache by BOTH entry count and source-byte proxy. A 1 GB source's light Python
# dicts measured ~220 MB, so count-only `_CACHE_MAX=3` could retain ~660 MB. Keep at most 1 GiB of
# represented source (or one oversize active index); eviction reloads the persisted accelerator.
_CACHE_MAX = 3
_CACHE_SOURCE_BYTES_MAX = 1024 * 1024 * 1024
# Bytes read per scan step. The OS readahead does the sequential-throughput work, so this only needs
# to be large enough to amortize the per-chunk scan; keeping it small is what bounds peak RSS to the
# derived index instead of the source file. The shared trace JSONL reader additionally caps a single
# physical row at TRACE_JSONL_ROW_MAX_BYTES, so even a corrupt multi-GB line cannot become its carry.
_SCAN_CHUNK_BYTES = 1024 * 1024
# A contiguous exporter receipt chain is the proof that an index may be extended. Size/mtime alone
# cannot distinguish a true append from an in-place prefix rewrite followed by append, while hashing
# the whole old prefix on every append is O(n²) live I/O. Receipts bind exact appended byte ranges to
# source identity/size/mtime; absent, torn, rotated or inconsistent metadata fails closed to rebuild.

_CACHE: "OrderedDict[str, SpanIndex]" = OrderedDict()
# Only protects the two tiny process-local maps below. Slow source scans, persisted-index reads and
# advisory-lock waits must never hold it: a cold index for run A is unrelated to a cold index for run
# B and the serve threadpool needs to build both concurrently.
_CACHE_LOCK = threading.RLock()
# Weak values keep the registry proportional to paths that are actively using a guard. Every waiter
# owns a strong local reference before releasing ``_CACHE_LOCK``, so one canonical path always maps
# to one RLock for the entire acquire/wait/critical-section lifetime without leaking one lock per run
# ever opened by a long-lived server.
_PATH_LOCKS: "weakref.WeakValueDictionary[str, object]" = weakref.WeakValueDictionary()


def _path_key(path: str | os.PathLike) -> str:
    """Canonical process-local key for cache and writer serialization.

    ``resolve()`` would add a filesystem lookup (and changes failure behaviour for missing/FUSE
    sources); an absolute lexical path is enough to make relative and absolute spellings in this
    process share the same guard.
    """
    return os.path.abspath(os.fspath(path))


def _path_lock(key: str):
    with _CACHE_LOCK:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _cache_get(key: str) -> Optional["SpanIndex"]:
    with _CACHE_LOCK:
        idx = _CACHE.get(key)
        if idx is not None:
            _CACHE.move_to_end(key)
        return idx


def _cache_contains(key: str) -> bool:
    with _CACHE_LOCK:
        return key in _CACHE


def _cache_store(key: str, idx: "SpanIndex") -> None:
    """Publish/move one LRU entry while holding the map lock for dict operations only."""
    with _CACHE_LOCK:
        _CACHE[key] = idx
        _CACHE.move_to_end(key)
        represented = sum(max(0, item.source_size or item.covers) for item in _CACHE.values())
        while (len(_CACHE) > 1
               and (len(_CACHE) > _CACHE_MAX or represented > _CACHE_SOURCE_BYTES_MAX)):
            _old_key, old = _CACHE.popitem(last=False)
            represented -= max(0, old.source_size or old.covers)


@contextmanager
def span_index_write_guard(
        spans_path: str | os.PathLike, *, required: bool = False):
    """Serialize persisted span-index writers with whole-run archive/reset.

    A per-path process lock protects the in-memory cache and the file lock covers another UI server
    process. Reset takes this same guard before publishing its writer marker and keeps it through
    artifact archive, so a cold trace GET cannot recreate generation-A derived data behind the
    archive. Different run paths intentionally do not share the process lock: their source/index
    files and cross-process locks are independent.
    """
    path = Path(_path_key(spans_path))
    lock = _path_lock(str(path))
    with lock:
        manager = _interprocess_lock(
            path.with_name(_INDEX_LOCK_NAME), required=required)
        try:
            manager.__enter__()
        except OSError:
            if required:
                raise
            # The index is an optional accelerator. A read-only/FUSE mount that cannot even create
            # the advisory lock must keep trace reads functional; reset passes ``required=True`` and
            # therefore still refuses to archive without this cross-process serialization.
            yield
            return
        try:
            yield
        finally:
            manager.__exit__(None, None, None)


def _scan_light_stream(stream, base: int, size: int, *,
                       label: str) -> tuple[list[tuple[dict, int, int]], int]:
    """`_scan_light` over a descriptor snapshot with bounded chunks *and* bounded rows.

    Complete invalid JSON/non-object rows and rows above TRACE_JSONL_ROW_MAX_BYTES both end the
    readable prefix without advancing ``consumed``. A valid object with an invalid span shape is
    still quarantined individually and consumed. A short read before the snapshotted ``size`` is an
    I/O failure unless iteration deliberately stopped at an oversized row.
    """
    records: list[tuple[dict, int, int]] = []
    consumed = base
    for raw in _iter_bounded_trace_jsonl_lines(
            stream, size=size, label=label,
            max_line_bytes=TRACE_JSONL_ROW_MAX_BYTES,
            read_chunk_bytes=_SCAN_CHUNK_BYTES):
        found, end = _scan_light(raw, consumed)
        if end == consumed:
            break                       # a complete corrupt line ends the append-log prefix
        records.extend(found)
        consumed = end
    return records, consumed


def _read_exact(stream, size: int, *, label: str) -> bytes:
    """Read exactly the snapshotted byte count or fail without publishing a partial index.

    Regular files normally satisfy ``read(size)`` in one call, but remote/FUSE file objects may
    legally return a short chunk before EOF. Keep reading in that case; a zero-length chunk before
    ``size`` is reached means the stat/read snapshot is no longer available and must propagate to
    the projection boundary as an I/O failure.
    """
    remaining = max(0, int(size))
    chunks: list[bytes] = []
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            got = size - remaining
            raise OSError(f"short read of {label}: expected {size} bytes, got {got}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _source_region_sha256(stream, offset: int, length: int) -> str:
    """SHA-256 one receipt-bounded source range through a fixed-size streaming buffer."""
    stream.seek(offset)
    remaining = max(0, int(length))
    digest = hashlib.sha256()
    while remaining:
        chunk = stream.read(min(_SCAN_CHUNK_BYTES, remaining))
        if not chunk:
            got = length - remaining
            raise OSError(
                f"short read of trace append: expected {length} bytes, got {got}")
        remaining -= len(chunk)
        digest.update(chunk)
    return digest.hexdigest()


def _decode_sha256(value) -> Optional[str]:
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    if len(raw) != hashlib.sha256().digest_size:
        return None
    return value.lower()


def _assert_source_descriptor(handle, path: Path, *, expected_identity=None):
    """Validate an open source descriptor and optionally bind it to an existing index identity."""
    stt = os.fstat(handle.fileno())
    assert_private_trace_file(stt, path)
    identity = trace_file_identity(stt)
    if expected_identity is not None and identity != expected_identity:
        raise OSError(getattr(errno, "ESTALE", errno.EIO),
                      "trace source no longer matches its index", path)
    return stt


def _journal_complete_coverage(stream, size: int) -> int:
    """Last newline boundary in a bounded receipt journal snapshot."""
    if size <= 0:
        return 0
    stream.seek(size - 1)
    if stream.read(1) == b"\n":
        return size
    pos = size
    while pos > 0:
        start = max(0, pos - 65_536)
        stream.seek(start)
        chunk = stream.read(pos - start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        pos = start
    return 0


def _journal_checkpoint(source_path: Path) -> tuple[Optional[tuple[int, int]], int]:
    """Checkpoint committed receipt EOF after a rebuild; invalid metadata stays untrusted."""
    journal = source_path.with_name(SPAN_APPEND_JOURNAL_NAME)
    try:
        with open_private_trace_file(journal, open_file=open) as stream:
            stt = os.fstat(stream.fileno())
            if stt.st_size > SPAN_APPEND_JOURNAL_MAX_BYTES:
                return None, 0
            return trace_file_identity(stt), _journal_complete_coverage(stream, stt.st_size)
    except (FileNotFoundError, OSError):
        return None, 0


_APPEND_RECEIPT_KEYS = frozenset({
    "schema", "dev", "ino", "before_size", "before_mtime_ns", "before_ctime_ns",
    "after_size", "after_mtime_ns", "after_ctime_ns", "append_sha256",
})


def _decode_append_receipt(raw: bytes) -> Optional[dict]:
    if not raw.endswith(b"\n"):
        return None
    try:
        value = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    if (not isinstance(value, dict) or set(value) != _APPEND_RECEIPT_KEYS
            or value.get("schema") != SPAN_APPEND_RECEIPT_SCHEMA):
        return None
    for key in ("dev", "ino", "before_size", "before_mtime_ns", "before_ctime_ns",
                "after_size", "after_mtime_ns", "after_ctime_ns"):
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            return None
    if value["after_size"] <= value["before_size"]:
        return None
    digest = _decode_sha256(value.get("append_sha256"))
    if digest is None:
        return None
    value["append_sha256"] = digest
    return value


def _validated_append_transition(
        idx: "SpanIndex", source, size: int, mtime_ns: int, ctime_ns: int,
) -> Optional[tuple[tuple[int, int], int]]:
    """Validate a contiguous receipt chain and only its appended source bytes.

    This is the incremental fast-path authority. Any missing/torn/rotated/forged transition returns
    ``None`` and the caller rebuilds from source truth; it never attempts to repair the journal.
    """
    journal = idx.path.with_name(SPAN_APPEND_JOURNAL_NAME)
    try:
        with open_private_trace_file(
                journal, expected_identity=idx.append_journal_identity,
                open_file=open) as stream:
            stt = os.fstat(stream.fileno())
            identity = trace_file_identity(stt)
            if (stt.st_size > SPAN_APPEND_JOURNAL_MAX_BYTES
                    or idx.append_journal_covers < 0
                    or idx.append_journal_covers > stt.st_size):
                return None
            stream.seek(idx.append_journal_covers)
            raw_tail = _read_exact(
                stream, stt.st_size - idx.append_journal_covers,
                label="trace append receipts")
    except OSError:
        return None
    if not raw_tail or not raw_tail.endswith(b"\n"):
        return None

    expected_size = idx.source_size
    expected_mtime = idx.mtime_ns
    expected_ctime = idx.ctime_ns
    if (expected_size is None or expected_mtime is None or expected_ctime is None
            or idx.identity is None):
        return None
    for raw in raw_tail.splitlines(keepends=True):
        receipt = _decode_append_receipt(raw)
        if receipt is None:
            return None
        if ((receipt["dev"], receipt["ino"]) != idx.identity
                or receipt["before_size"] != expected_size
                or receipt["before_mtime_ns"] != expected_mtime
                or receipt["before_ctime_ns"] != expected_ctime
                or receipt["after_size"] > size
                or _source_region_sha256(
                    source, receipt["before_size"],
                    receipt["after_size"] - receipt["before_size"]
                ) != receipt["append_sha256"]):
            return None
        expected_size = receipt["after_size"]
        expected_mtime = receipt["after_mtime_ns"]
        expected_ctime = receipt["after_ctime_ns"]
    if (expected_size != size or expected_mtime != mtime_ns or expected_ctime != ctime_ns):
        return None
    return identity, stt.st_size


@contextmanager
def _reading(path: Path, handle, offset: int, *, expected_identity=None):
    """Read from `handle` when the caller owns one, else open `path` for this read alone.

    `get_index` opens spans.jsonl ONCE and derives identity/size from `fstat` on that descriptor, so
    passing it down keeps the validation and the bytes on the same inode — a rewrite mid-scan cannot
    splice two generations of the file into one index. The `handle is None` branch preserves the old
    self-contained behaviour for any caller that has no descriptor to lend.
    """
    if handle is None:
        with open_private_trace_file(
                path, expected_identity=expected_identity, open_file=open) as opened:
            opened.seek(offset)
            yield opened
        return
    _assert_source_descriptor(handle, path, expected_identity=expected_identity)
    handle.seek(offset)
    yield handle


def _scan_light(buf: bytes, base: int) -> tuple[list[tuple[dict, int, int]], int]:
    """Parse complete JSONL lines from `buf` (a slice of spans.jsonl starting at file offset `base`),
    applying `iter_jsonl`'s durability rules (stop at the first torn/corrupt line). Yields
    `(light_span, off, length)` where `off` is the line-start offset IN THE FILE and `length` is the
    line length WITHOUT the trailing newline — so a reader can `seek(off); read(length)` to recover
    the FULL span line verbatim. Returns `(records, consumed)`; `consumed` lands on a newline boundary
    (the exact prefix `iter_jsonl` would have accepted), so it is the index's coverage watermark."""
    records: list[tuple[dict, int, int]] = []
    parsed, consumed = scan_jsonl_region(buf)
    for obj, start, end in parsed:
        # A span that does not normalize is DROPPED but still CONSUMED: it is a well-formed record
        # this projection has no use for, not damage, so it must not stall the watermark.
        normalized = _normalize_span(obj)
        if normalized is not None:
            records.append((_strip_span_io(normalized), base + start, end - start))
    # `consumed` is the offset of the last complete-newline boundary within buf (a torn/corrupt tail
    # is NOT consumed — it is left for a later top-up once completed). Absolute coverage = base+consumed.
    return records, base + consumed


class SpanIndex:
    """In-memory light-span index for one `spans.jsonl`. `covers` = bytes of spans.jsonl indexed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.light: list[dict] = []               # light spans, file order (fed to build_trace_view)
        self.meta: list[tuple[int, int]] = []     # (offset, length) in spans.jsonl, parallel to light
        self.by_sid: dict[str, int] = {}          # span_id -> row
        self.by_tid: dict[str, list[int]] = defaultdict(list)   # trace_id -> rows
        self.node_tids: dict[str, set] = defaultdict(set)       # str(node_id) -> {trace_id}
        self.covers: int = 0
        self.identity: Optional[tuple] = None
        # The complete source size observed by the last successful refresh. It is deliberately
        # separate from ``covers``: a torn/corrupt suffix leaves covers at the last good newline,
        # so comparing a later stat only with covers mistakes an in-place, same-size rewrite for an
        # append. source_size + mtime/ctime identify non-growing mutations while true appends use the
        # receipt chain below (ctime catches a rewrite whose mtime was deliberately restored).
        self.source_size: Optional[int] = None
        self.mtime_ns: Optional[int] = None
        self.ctime_ns: Optional[int] = None
        # Cursor/identity of the append receipt journal at the same source snapshot. Growth may top
        # up only through a contiguous chain after this boundary; otherwise source truth is rebuilt.
        self.append_journal_identity: Optional[tuple[int, int]] = None
        self.append_journal_covers: int = 0
        self._persisted_covers: int = -1          # covers at last persist (throttle re-writes)
        # Guards the mutable in-memory maps against a concurrent read. `get_index` runs a `_topup`
        # (append) under this index path's write guard, but the READ methods below are called
        # lock-free by the serve threadpool AFTER get_index returns — so a read that iterates
        # `node_tids`/`by_tid` (or copies `light`) would otherwise race a concurrent topup's
        # `.add()`/`.append()` ("set/dict changed size during iteration"). Reads take a cheap
        # in-memory SNAPSHOT under this lock, then do the (slow) disk seeks OUTSIDE it. Per-INDEX so
        # a read of run A never waits on a slow rebuild of run B. Held strictly inside the path
        # guard's scope in get_index (order: path RLock → _rlock), never the reverse.
        self._rlock = threading.Lock()

    # -- construction --------------------------------------------------------------------------
    def _append(self, light: dict, off: int, length: int) -> bool:
        # Normalizes UNCONDITIONALLY, including records `_extend` already normalized during
        # `_scan_light`. That second pass is deliberate: this is the ONE gate every row passes
        # through, and `_load_persisted` feeds it untrusted on-disk records that must be validated
        # here or not at all. The redundancy is cheap by construction — the expensive
        # redaction/entropy fields (input/output/thinking) were stripped on the first pass, so the
        # repeat runs only over the small light record. A pre-validated flag would save that pass at
        # the cost of making the trust boundary opt-in, which is the wrong default for a parser.
        normalized = _normalize_span(light)
        if normalized is None:
            return False
        light = normalized
        row = len(self.light)
        self.light.append(light)
        self.meta.append((off, length))
        sid = light.get("span_id")
        if sid is not None:
            self.by_sid[sid] = row
        tid = light.get("trace_id")
        if tid is not None:
            self.by_tid[tid].append(row)
            attributes = light.get("attributes")
            nid = attributes.get("node_id") if isinstance(attributes, dict) else None
            if nid is not None:
                self.node_tids[str(nid)].add(tid)
        return True

    def _extend(self, records: list[tuple[dict, int, int]]) -> None:
        for light, off, length in records:
            self._append(light, off, length)

    def _rebuild(self, size: int, handle=None) -> None:
        # unavailable trace bytes must propagate as unavailable; publishing an empty
        # index here would turn an ACL/read failure into false evidence that the run had no spans.
        # A readable empty source and an unreadable source are different facts. Let I/O failures
        # reach the HTTP projection boundary instead of publishing a complete-looking empty index.
        # `handle` is the descriptor `get_index` already validated by `fstat`; reading through it
        # keeps identity, size and content on ONE inode instead of resolving the pathname again.
        with _reading(self.path, handle, 0, expected_identity=self.identity) as f:
            # Streamed in bounded chunks: the index is ~30x smaller than its source, so a multi-GB
            # trace must never be materialized whole just to derive it. Parsed OUTSIDE the lock (the
            # slow part) and published below only after the pass completes.
            records, consumed = _scan_light_stream(f, 0, size, label="trace source")
        journal_identity, journal_covers = _journal_checkpoint(self.path)
        with self._rlock:                            # publish the new maps atomically vs a lock-free read
            self.light.clear()
            self.meta.clear()
            self.by_sid.clear()
            self.by_tid.clear()
            self.node_tids.clear()
            self._extend(records)
            self.covers = consumed
            self.append_journal_identity = journal_identity
            self.append_journal_covers = journal_covers

    def _topup(self, size: int, handle=None) -> None:
        """Parse only receipt-validated newly appended bytes.

        The caller must validate the append transition first. ``handle`` is `get_index`'s already-
        validated descriptor — see `_rebuild`.
        """
        if size <= self.covers:
            return
        with _reading(
                self.path, handle, self.covers, expected_identity=self.identity) as f:
            old_covers = self.covers
            # Same bounded stream as `_rebuild`: an appended tail is usually small, but the first
            # top-up after a long detach can be arbitrarily large.
            records, consumed = _scan_light_stream(
                f, old_covers, size - old_covers, label="trace tail")   # parsed OUTSIDE the lock
        with self._rlock:                                   # append is atomic vs a lock-free read
            self._extend(records)
            self.covers = consumed

    # -- reads (snapshot the in-memory maps under `_rlock`, then do disk I/O outside it) ---------
    def light_spans(self, limit: Optional[int] = None) -> list[dict]:
        """A SNAPSHOT of the light span list (for `build_trace_view(light=True)`). A copy, not a live
        reference: build_trace_view iterates it while a concurrent `_topup` may append to `self.light`,
        and a plain-reference iteration would silently pick up half-appended tail spans. The dicts
        inside are shared (never mutated after creation), so the copy is shallow and cheap (~ms)."""
        with self._rlock:
            cap = None if limit is None else max(0, int(limit))
            values = self.light if cap is None else (self.light[-cap:] if cap else ())
            return list(values)

    def span_count(self) -> int:
        with self._rlock:
            return len(self.light)

    def _read_full(self, rows: list[int]) -> list[dict]:
        """Read and safely project selected full span lines by seeking to their byte offsets —
        so a per-node/-trace/-span detail view touches only those bytes, not the whole file. `rows`
        is a snapshot taken under `_rlock` by the caller; `self.meta` is append-only, so reading
        `meta[r]` here (outside the lock) is safe — a concurrent append never moves an existing row."""
        out: list[dict] = []
        # Offset reads may happen after `get_index` released its source descriptor. Re-open with the
        # same no-follow/type/identity fence so replacing the run's sidecar with a symlink cannot
        # redirect a warm or persisted index into another run's private diagnostics.
        with open_private_trace_file(
                self.path, expected_identity=self.identity, open_file=open) as f:
            for r in sorted(rows):                 # sorted → mostly-sequential reads
                off, length = self.meta[r]
                f.seek(off)
                data = _read_exact(f, length, label="indexed trace span")
                try:
                    obj = orjson.loads(data)
                except orjson.JSONDecodeError:
                    continue                       # offset drift on a span — skip it, don't crash
                if not isinstance(obj, dict):
                    continue
                # An offset that drifted onto a DIFFERENT but still-valid span line (bit-rot on a
                # network mount, or a same-size in-place rewrite the single-span spotcheck missed)
                # would otherwise be returned as if it were this row's span. Cross-check the read
                # span_id against the one this row indexes: on a provable mismatch skip it, so the
                # accelerator returns None/less — never WRONG data — as its docstring promises.
                normalized = _normalize_span(obj)
                if normalized is None:
                    continue
                expected = self.light[r].get("span_id")
                got_id = normalized.get("span_id")
                if expected is not None and got_id is not None and got_id != expected:
                    continue
                out.append(normalized)
        return out

    def full_span(self, sid: str) -> Optional[dict]:
        with self._rlock:
            row = self.by_sid.get(sid)
        if row is None:
            return None
        got = self._read_full([row])
        return got[0] if got else None

    def full_spans_for_trace(self, tid: str, limit: Optional[int] = None,
                             *, anchor_sid: Optional[str] = None) -> list[dict]:
        with self._rlock:
            rows = list(self.by_tid.get(tid, ()))
            if anchor_sid is not None:
                anchor = self.by_sid.get(anchor_sid)
                # a caller may find a just-appended span by scanning past this index's
                # snapshot. In an append-only log that missing anchor follows every indexed row, so
                # keep the whole indexed prefix; emptying it would destroy the input_from ancestry.
                if anchor is not None:
                    rows = [row for row in rows if row <= anchor]
            if limit is not None:
                cap = max(0, int(limit))
                rows = rows[-cap:] if cap else []
        return self._read_full(rows)

    def trace_span_count(self, tid: str) -> int:
        with self._rlock:
            return len(self.by_tid.get(tid, ()))

    def _rows_for_node(self, node_id, generation: Optional[int] = None) -> list[int]:
        """Return file-order rows for one node, optionally fenced to one lifecycle generation.

        The generation is a TRACE-root property: descendants inherit the root's lifecycle even when
        their own attributes contain an unrelated retry ``attempt`` or no generation field at all.
        Legacy unstamped traces predate resets and therefore belong only to generation zero.
        Caller holds ``_rlock``.

        The root comes from ``traceview.trace_root_span`` — the SHARED rule both views attribute
        from — never a local re-derivation. This site used to take the first root in FILE order while
        the views take the earliest by ``start``, and the two are not the same order: spans.jsonl is
        written on span CLOSE, so the span that OPENED a trace is usually written LAST. So the fence
        could be read off a DIFFERENT span than the one `build_trace_view` and `build_conversation`
        attribute from, and a node's trace card would then be fenced to a lifecycle nobody else
        believed it was in (doc 25 EV-10, the fourth copy the first cut left behind).

        Stays an in-memory accelerator: ``self.light`` rows are already normalized, so this passes
        the dicts the index ALREADY holds and adds no disk read, no dependency on ``spans.jsonl``
        being complete, and no new failure mode. A truncated/quarantined source simply indexes fewer
        rows here, exactly as before.
        """
        target = str(node_id)
        tids = self.node_tids.get(target, ())
        rows: list[int] = []
        for tid in tids:
            trace_rows = list(self.by_tid.get(tid, ()))
            if not trace_rows:
                continue
            trace_spans = [self.light[row] for row in trace_rows]
            # A rootless trace is a parent_id CYCLE (corrupt/crafted source only). The shared rule
            # declines to name a root there rather than nominating an arbitrary span, and this
            # follows it into the unstamped default below — the same answer the views reach, instead
            # of reading one span's `generation` as if it were the whole trace's.
            if (generation is not None
                    and trace_root_generation(trace_spans, _normalized=True) != generation):
                continue
            root_nid = trace_root_node_id(trace_spans, _normalized=True)
            # A trace is only a candidate because `node_tids` saw the target on SOME row. Shared
            # long-lived traces can also carry newer rows stamped for other nodes. Filter by the
            # shared per-span-first/root-fallback attribution rule BEFORE totals and tail limits;
            # otherwise another node's newest row consumes the window and the requested node looks
            # empty while its total falsely counts foreign rows.
            rows.extend(
                row for row in trace_rows
                if (effective := effective_node_id(self.light[row], root_nid)) is not None
                and str(effective) == target
            )
        return sorted(rows)

    def light_spans_for_node(self, node_id, limit: Optional[int] = None, *,
                             generation: Optional[int] = None) -> list[dict]:
        """The LIGHT spans of the traces attributed to this node — IN-MEMORY, no disk read (unlike
        `full_spans_for_node`, which seeks each span's full I/O). Lets the node-detail timeline build
        O(node) instead of O(whole run): `build_trace_view(light=True)` over just these yields the SAME
        `nodes[nid]`/`rollup` as over ALL spans. Candidate traces come from `node_tids`, then each
        span is filtered by its own node_id or trace-root fallback so shared traces cannot bleed.
        A generation fence is applied BEFORE the row limit, so abandoned attempts cannot consume the
        current attempt's response window."""
        with self._rlock:
            rows = self._rows_for_node(node_id, generation)
            if limit is not None:
                cap = max(0, int(limit))
                rows = rows[-cap:] if cap else []
            return [self.light[row] for row in rows]

    def node_span_count(self, node_id, *, generation: Optional[int] = None) -> int:
        with self._rlock:
            return len(self._rows_for_node(node_id, generation))

    def full_spans_for_node(self, node_id, limit: Optional[int] = None, *,
                            generation: Optional[int] = None) -> list[dict]:
        """Every FULL span in the traces attributed to this node (a node's create_node + evaluate +
        repair traces). Candidate traces are found in-memory, then spans are retained only when the
        shared per-span-first/root-fallback rule attributes them to this node. Filtering (and an
        optional trace-root lifecycle fence) precedes totals and the row limit, so neither another
        node sharing the trace nor an abandoned attempt can consume the conversation window."""
        with self._rlock:                              # snapshot rows — never iterate the live set/lists
            rows = self._rows_for_node(node_id, generation)
            if limit is not None:
                cap = max(0, int(limit))
                rows = rows[-cap:] if cap else []
        return self._read_full(rows)

    # -- persistence ---------------------------------------------------------------------------
    def _persist(self) -> None:
        """Atomically write the light index to `spans.index.jsonl` (header + one line per span with its
        byte offset/length). Throttled GEOMETRICALLY: a live run tops up on every node boundary, and
        re-writing the whole index each time is O(n²) total write volume over the run — and each rewrite
        is a full-object PUT on the S3/geesefs mount the run dir often lives on. So persist only when the
        covered span bytes grew ≥`_PERSIST_GROWTH`× since the last write (plus always the first time):
        a 1 GB run persists ~O(log n) times / ~O(n) total bytes instead of ~covers/8MB full rewrites.
        Trade: the persisted index may lag the in-memory one by up to (1 − 1/g); a fresh process
        cold-loads it then re-parses that bounded tail delta from spans.jsonl — still far cheaper than a
        full rebuild, and the in-memory index (the primary accelerator) is always current."""
        if self.identity is None or not self.light:
            return  # nothing to persist (no identity yet, or an empty/traceless spans.jsonl)
        if self._persisted_covers > 0 and self.covers < self._persisted_covers * _PERSIST_GROWTH:
            return
        # ``get_index`` holds ``span_index_write_guard`` here. Reset publishes its marker while
        # holding that same guard, so this check and the following replace cannot straddle archive.
        # An unreadable marker is also fail-closed for this optional cache write: trace reads may
        # continue from memory/source, but they must not mutate a run whose reset owner is unknown.
        try:
            if (load_run_reset_marker(self.path.parent) is not None
                    or load_run_deletion_fence(self.path.parent) is not None):
                return
        except (RunResetStorageError, RunDeletionStorageError):
            return
        header = {"_idx": _SCHEMA, "covers": self.covers,
                  "dev": self.identity[0], "ino": self.identity[1],
                  "source_size": self.source_size, "mtime_ns": self.mtime_ns,
                  "ctime_ns": self.ctime_ns,
                  "append_journal_dev": (
                      self.append_journal_identity[0]
                      if self.append_journal_identity is not None else None),
                  "append_journal_ino": (
                      self.append_journal_identity[1]
                      if self.append_journal_identity is not None else None),
                  "append_journal_covers": self.append_journal_covers}
        # One growable buffer, not a list of one bytes object per row plus a second full-size join.
        # The old shape temporarily retained ~2x the serialized index on top of its Python dicts.
        payload = bytearray(orjson.dumps(header))
        payload.append(0x0A)
        for light, (off, length) in zip(self.light, self.meta):
            payload.extend(orjson.dumps({**light, "_o": off, "_l": length}))
            payload.append(0x0A)
        try:
            atomic_write_bytes(self.path.with_name(_INDEX_NAME), payload)
            self._persisted_covers = self.covers
        except OSError:
            pass  # a derived cache; a failed persist just means the next cold open rebuilds


def _spotcheck(idx: SpanIndex) -> bool:
    """Cheap O(1) sanity check that the persisted offsets still address spans.jsonl: re-read the LAST
    indexed span at its recorded (offset,length) and confirm the span_id matches. This catches the
    invalidations that actually occur — a truncation/rewrite that changed the tail — but, being a
    single-span check, does NOT detect a mid-file byte shift that left the last span in place. That
    pathological case can't arise here: spans.jsonl is append-only, and the only rewriters (clear_trace,
    reset) go through atomic temp+rename → a NEW inode, which the dev/ino identity guard already rejects
    before we get here. Full integrity is not the goal — the index is a rebuildable accelerator, so any
    missed drift degrades to a wrong-offset read that `_read_full` skips, never wrong data."""
    if not idx.light:
        return True
    last = idx.light[-1]
    got = idx._read_full([len(idx.light) - 1])
    return bool(got) and got[0].get("span_id") == last.get("span_id")


def _load_persisted(spans_path: Path, identity: tuple, size: int,
                    mtime_ns: int, ctime_ns: int, source_handle=None) -> Optional[SpanIndex]:
    """Load `spans.index.jsonl` if it is a valid, current index for this spans.jsonl (fast cold path:
    read ~16 MB instead of re-parsing 1 GB). Returns None on any mismatch → caller rebuilds. Coverage
    is DERIVED from the records actually read (not trusted from the header), so a torn index tail just
    means a smaller covered prefix that the caller tops up from spans.jsonl."""
    ip = spans_path.with_name(_INDEX_NAME)
    idx = SpanIndex(spans_path)
    try:
        # The persisted index is untrusted derived metadata. A run-root symlink/hardlink/FIFO must
        # trigger a source rebuild, never redirect this cold reader or strand its server worker.
        with open_private_trace_file(ip, open_file=open) as f:
            index_size = os.fstat(f.fileno()).st_size
            lines = _iter_bounded_trace_jsonl_lines(
                f, size=index_size, label="persisted span index",
                max_line_bytes=TRACE_JSONL_ROW_MAX_BYTES,
                read_chunk_bytes=_SCAN_CHUNK_BYTES)
            first = next(lines, None)
            if first is None:
                return None
            try:
                header = orjson.loads(first.strip())
            except orjson.JSONDecodeError:
                return None
            if not isinstance(header, dict) or header.get("_idx") != _SCHEMA:
                return None
            if header.get("dev") != identity[0] or header.get("ino") != identity[1]:
                return None  # index was built for a different underlying file (reset/replace)
            observed_size = header.get("source_size")
            observed_mtime = header.get("mtime_ns")
            observed_ctime = header.get("ctime_ns")
            observed_covers = header.get("covers")
            journal_dev = header.get("append_journal_dev")
            journal_ino = header.get("append_journal_ino")
            journal_covers = header.get("append_journal_covers")
            if (not isinstance(observed_size, int) or isinstance(observed_size, bool)
                    or observed_size < 0
                    or not isinstance(observed_mtime, int) or isinstance(observed_mtime, bool)
                    or not isinstance(observed_ctime, int) or isinstance(observed_ctime, bool)
                    or not isinstance(observed_covers, int) or isinstance(observed_covers, bool)
                    or observed_covers < 0 or observed_covers > observed_size
                    or observed_covers > size
                    or not isinstance(journal_covers, int) or isinstance(journal_covers, bool)
                    or journal_covers < 0 or journal_covers > SPAN_APPEND_JOURNAL_MAX_BYTES
                    or ((journal_dev is None) != (journal_ino is None))
                    or (journal_dev is not None and (
                        not isinstance(journal_dev, int) or isinstance(journal_dev, bool)
                        or journal_dev < 0
                        or not isinstance(journal_ino, int) or isinstance(journal_ino, bool)
                        or journal_ino < 0))
                    or (journal_dev is None and journal_covers != 0)):
                return None
            # A larger current source is the normal append-only path and can top up from coverage.
            # A non-growing source whose mtime moved was mutated in-place, even when a corrupt suffix
            # made ``covers < size``. None of its cached light rows/offsets remain authoritative.
            if (size < observed_size
                    or (size == observed_size
                        and (mtime_ns != observed_mtime or ctime_ns != observed_ctime))):
                return None
            last_end = 0
            for raw in lines:
                try:
                    # The persisted index is itself an append-only JSONL file, so it gets the SAME
                    # prefix rule as the log it accelerates — shared rather than re-derived.
                    rec = decode_jsonl_line(raw)
                except JsonlRecordInvalid:
                    break
                if rec is None:
                    continue
                off = rec.pop("_o", None)
                length = rec.pop("_l", None)
                if (not isinstance(off, int) or isinstance(off, bool)
                        or not isinstance(length, int) or isinstance(length, bool)
                        or off < 0 or length < 0
                        or length + 1 > TRACE_JSONL_ROW_MAX_BYTES
                        or off + length + 1 > observed_covers):
                    # A corrupt/out-of-bounds offset from a damaged persisted index must never reach
                    # `f.read(length)` (a negative length reads the whole file into memory). Treat it
                    # like a torn tail: keep the valid prefix, rebuild the rest from spans.jsonl.
                    break
                if not idx._append(rec, off, length):
                    break
                last_end = off + length + 1  # +1 for the newline that follows the line in spans.jsonl
    except OSError:
        return None
    idx.covers = last_end
    if idx.covers > size:
        return None  # spans.jsonl is smaller than the index claims — stale (shrank/rewritten)
    idx.identity = identity
    # Retain the PERSISTED source boundary until `_index_from_handle` validates every receipt from it
    # to the current descriptor. Assigning current size/mtime here would let the first receipt gap
    # disappear before validation.
    idx.source_size = observed_size
    idx.mtime_ns = observed_mtime
    idx.ctime_ns = observed_ctime
    idx.append_journal_identity = (
        (journal_dev, journal_ino) if journal_dev is not None else None)
    idx.append_journal_covers = journal_covers
    idx._persisted_covers = idx.covers   # it IS persisted at this coverage — don't rewrite it unchanged
    if not _spotcheck(idx):
        return None
    return idx


def get_index(spans_path: str | os.PathLike) -> Optional[SpanIndex]:
    """Return the (incrementally-maintained, persisted) light span index for `spans_path`, or None if
    the file does not exist. Cached in-process; tops up from the appended tail on a hit, loads the
    persisted index or rebuilds on a cold miss. Thread-safe."""
    p = Path(_path_key(spans_path))
    key = str(p)
    with span_index_write_guard(p):
        # LSTAT + no-follow OPEN + FSTAT. A run sidecar is a private regular file, never a capability
        # to another path: following `spans.jsonl -> ../other-run/spans.jsonl` would expose that run's
        # prompt/tool diagnostics through every indexed projection. The helper also binds the lstat
        # and descriptor identities, while `_index_from_handle` derives size/mtime from that descriptor
        # alone. Cached indexes are accelerators, not authority once this source becomes unavailable.
        try:
            with open_private_trace_file(p, open_file=open) as handle:
                return _index_from_handle(p, key, handle)
        except FileNotFoundError:
            try:
                os.lstat(p)
            except FileNotFoundError:
                if not _cache_contains(key):
                    return None  # no spans.jsonl (tracing off / pre-tracing run) — caller degrades
                # This run has been OBSERVED to have spans. A source that has since disappeared is an
                # availability failure, not "the run produced no trace" — publishing empty would turn
                # a vanished/renamed file into false evidence. Let it reach the projection boundary,
                # which reports `unavailable`. (Every other OSError propagates unconditionally.)
                raise
            # `open()` reported ENOENT while a following stat still sees the path. This is an
            # availability race/failure, not evidence that the run produced no trace.
            raise


def _index_from_handle(p: Path, key: str, handle) -> SpanIndex:
    """Build/refresh the index for `p` reading exclusively through the caller's open descriptor.

    Split out only so the descriptor's lifetime is a plain `with` in `get_index`; this path's write
    guard is held by that caller for the whole call.
    """
    stt = os.fstat(handle.fileno())
    size, mtime_ns, ctime_ns = stt.st_size, stt.st_mtime_ns, stt.st_ctime_ns
    identity = trace_file_identity(stt)
    idx = _cache_get(key)
    force_rebuild = False
    if idx is not None:
        # Reuse the cached index only when spans.jsonl is the SAME file grown by pure appends —
        # mirrors EventStore.read_all's guard so a network/FUSE mount can't feed the trace view a
        # stale prefix: `replaced` = a new inode (atomic rewrite/reset), `non_growth_rewrite` = an
        # in-place rewrite that did not grow the LAST OBSERVED source (detected by size/mtime even
        # when a corrupt suffix made covers<size), `shrank` = a truncate/compaction. A same-inode
        # mutation forces a source rebuild rather than reloading a persisted index whose dev/ino and
        # last-row spotcheck can still look valid after a mid-file rewrite. Growing files require a
        # contiguous exporter receipt chain before `_topup`: growth+mtime is otherwise ambiguous
        # between a true append and an in-place rewrite followed by append.
        replaced = idx.identity != identity
        prior_size = idx.source_size if idx.source_size is not None else idx.covers
        shrank = size < prior_size
        non_growth_rewrite = (not replaced and size <= prior_size
                              and ((idx.mtime_ns is not None and mtime_ns != idx.mtime_ns)
                                   or (idx.ctime_ns is not None and ctime_ns != idx.ctime_ns)))
        force_rebuild = shrank or non_growth_rewrite
        if not (replaced or force_rebuild):
            transition = None
            if size > prior_size:
                transition = _validated_append_transition(
                    idx, handle, size, mtime_ns, ctime_ns)
                if transition is None:
                    force_rebuild = True   # growth without a complete writer chain is a rewrite
            if not force_rebuild:
                if idx.covers < size:
                    idx._topup(size, handle)
                if transition is not None:
                    idx.append_journal_identity, idx.append_journal_covers = transition
                idx.source_size = size
                idx.mtime_ns = mtime_ns
                idx.ctime_ns = ctime_ns
                # Another run's concurrent insertion may have evicted this entry while its slow
                # top-up ran. Re-publish it; the same-path guard prevents a competing instance.
                _cache_store(key, idx)
                idx._persist()
                return idx
    # Cold miss (not cached, replaced, or shrank): load the persisted index if valid, else rebuild.
    idx = None if force_rebuild else _load_persisted(
        p, identity, size, mtime_ns, ctime_ns, handle)
    if idx is None:
        idx = SpanIndex(p)
        idx.identity = identity
        idx._rebuild(size, handle)
    elif idx.source_size is not None and size > idx.source_size:
        transition = _validated_append_transition(idx, handle, size, mtime_ns, ctime_ns)
        if transition is None:
            idx = SpanIndex(p)
            idx.identity = identity
            idx._rebuild(size, handle)
        else:
            if idx.covers < size:
                idx._topup(size, handle)
            idx.append_journal_identity, idx.append_journal_covers = transition
    elif idx.covers < size:
        # Persisted index tail lag within the SAME size/mtime snapshot (throttled/torn cache), not a
        # source append. Re-parse that source suffix without demanding a receipt that never existed.
        idx._topup(size, handle)
    idx.source_size = size
    idx.mtime_ns = mtime_ns
    idx.ctime_ns = ctime_ns
    _cache_store(key, idx)
    idx._persist()
    return idx


def invalidate(spans_path: str | os.PathLike) -> None:
    """Drop the cached index for a run (after clear_trace/reset rewrites spans.jsonl). The identity/
    size guards in `get_index` already catch a replaced file, so this is belt-and-suspenders."""
    key = _path_key(spans_path)
    # Serialize eviction with a same-path get/reset/delete, but never stall unrelated runs behind it.
    with _path_lock(key):
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
