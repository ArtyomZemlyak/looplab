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

import os
import threading
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import orjson

from looplab.core.atomicio import atomic_write_bytes
from looplab.core.run_deletion import RunDeletionStorageError, load_run_deletion_fence
from looplab.core.run_reset import RunResetStorageError, load_run_reset_marker
from looplab.events.eventstore import (
    JsonlRecordInvalid, _interprocess_lock, decode_jsonl_line, scan_jsonl_region)
from looplab.events.traceview import _normalize_span, _strip_span_io, trace_root_span

# Bump when the persisted record shape changes so an old `spans.index.jsonl` is ignored (rebuilt),
# never mis-read. The index is a cache — a version skew simply triggers one rebuild.
_SCHEMA = 6
_INDEX_NAME = "spans.index.jsonl"
_INDEX_LOCK_NAME = ".spans-index.lock"
# Geometric re-persist factor (see `_persist`): re-write the persisted index only when the indexed
# span bytes have grown by this factor since the last write. Bounds a live run's total index-write
# volume to ~O(n) (a handful of full-object PUTs on S3/geesefs) instead of ~O(n²) full rewrites every
# few MB. The first index (from covers 0) always persists.
_PERSIST_GROWTH = 1.5
# Bound the in-process cache: a 1 GB run's light spans are ~220 MB of Python dicts, so hold only a few
# (the user views one run at a time; an evicted index just reloads its persisted form, not a rescan).
_CACHE_MAX = 3
# Bytes read per scan step. The OS readahead does the sequential-throughput work, so this only needs
# to be large enough to amortize the per-chunk scan; keeping it small is what bounds peak RSS to the
# derived index instead of the source file. One span line larger than this is still held whole (it
# has to be, to parse it) — the carry below is at most one line.
_SCAN_CHUNK_BYTES = 1024 * 1024

_CACHE: "OrderedDict[str, SpanIndex]" = OrderedDict()
_LOCK = threading.RLock()


@contextmanager
def span_index_write_guard(
        spans_path: str | os.PathLike, *, required: bool = False):
    """Serialize persisted span-index writers with whole-run archive/reset.

    The process lock protects the in-memory cache and the file lock covers another UI server process.
    Reset takes this same guard before publishing its writer marker and keeps it through artifact
    archive, so a cold trace GET cannot recreate generation-A derived data behind the archive.
    """
    path = Path(spans_path)
    with _LOCK:
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
    """`_scan_light` over `size` bytes of `stream`, holding only a bounded window in memory.

    A cold rebuild used to read the WHOLE spans.jsonl into one bytes object before parsing a line of
    it, so a multi-GB trace could exhaust host memory to produce an index the module docstring
    measures at ~30x smaller. Nothing needs the full file at once: the scanner already works on a
    slice, so read a fixed chunk, scan the complete lines in it, and carry only the trailing partial
    line into the next chunk.

    The carry also encodes WHY the scanner stopped, which the chunked form must not confuse:

    * carry with no newline = the last line is torn by the chunk boundary (or by the snapshot's end)
      — read on; the next chunk completes it;
    * carry containing a newline = `_scan_light` refused a COMPLETE line as corrupt. That is
      `iter_jsonl`'s stop-at-first-corruption rule, so the scan ends here and the caller's coverage
      watermark is the last good boundary — exactly what the unchunked version did.

    A short read before `size` is an I/O failure, same as `_read_exact`: the snapshot the caller
    validated is gone, and publishing a truncated index would claim the run had fewer spans.
    """
    records: list[tuple[dict, int, int]] = []
    consumed = base
    pending: list[bytes] = []        # the partial line carried across chunk boundaries, unjoined
    remaining = max(0, int(size))
    while remaining:
        chunk = stream.read(min(_SCAN_CHUNK_BYTES, remaining))
        if not chunk:
            got = size - remaining
            raise OSError(f"short read of {label}: expected {size} bytes, got {got}")
        remaining -= len(chunk)
        # A chunk with no newline cannot complete a line, so BUFFER it and read on. Joining and
        # re-scanning at every chunk boundary is what made a single over-chunk span line quadratic:
        # each iteration copied the growing carry AND re-scanned it from offset 0 for a newline it
        # had already failed to find — ~O(L^2 / chunk) for an L-byte line, and multi-MB generation
        # spans are the norm in exactly the files this module targets. Deferring costs one join and
        # one scan per LINE instead of per chunk, which is linear.
        if b"\n" not in chunk:
            pending.append(chunk)
            continue
        # Concatenate only when a partial line is actually pending: the common case is a chunk that
        # ends on a newline, and joining would otherwise copy every byte of the file twice.
        window = chunk if not pending else b"".join((*pending, chunk))
        pending = []
        found, end = _scan_light(window, consumed)
        records.extend(found)
        carry = window[end - consumed:]
        consumed = end
        if b"\n" in carry:
            break                    # corrupt line, not a chunk boundary — stop like iter_jsonl does
        if carry:
            pending.append(carry)
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


@contextmanager
def _reading(path: Path, handle, offset: int):
    """Read from `handle` when the caller owns one, else open `path` for this read alone.

    `get_index` opens spans.jsonl ONCE and derives identity/size from `fstat` on that descriptor, so
    passing it down keeps the validation and the bytes on the same inode — a rewrite mid-scan cannot
    splice two generations of the file into one index. The `handle is None` branch preserves the old
    self-contained behaviour for any caller that has no descriptor to lend.
    """
    if handle is None:
        with open(path, "rb") as opened:
            opened.seek(offset)
            yield opened
        return
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
        self.mtime_ns: Optional[int] = None
        self._persisted_covers: int = -1          # covers at last persist (throttle re-writes)
        # Guards the mutable in-memory maps against a concurrent read. `get_index` runs a `_topup`
        # (append) under the module `_LOCK`, but the READ methods below are called lock-free by the
        # serve threadpool AFTER get_index returns — so a read that iterates `node_tids`/`by_tid` (or
        # copies `light`) would otherwise race a concurrent topup's `.add()`/`.append()` ("set/dict
        # changed size during iteration"). Reads take a cheap in-memory SNAPSHOT under this lock, then
        # do the (slow) disk seeks OUTSIDE it. Per-INDEX (not the module lock) so a read of run A never
        # waits on a slow rebuild of run B. Held strictly inside the module lock's scope in get_index
        # (order: _LOCK → _rlock), never the reverse, so the two can't deadlock.
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
        with _reading(self.path, handle, 0) as f:
            # Streamed in bounded chunks: the index is ~30x smaller than its source, so a multi-GB
            # trace must never be materialized whole just to derive it. Parsed OUTSIDE the lock (the
            # slow part) and published below only after the pass completes.
            records, consumed = _scan_light_stream(f, 0, size, label="trace source")
        with self._rlock:                            # publish the new maps atomically vs a lock-free read
            self.light.clear()
            self.meta.clear()
            self.by_sid.clear()
            self.by_tid.clear()
            self.node_tids.clear()
            self._extend(records)
            self.covers = consumed

    def _topup(self, size: int, handle=None) -> None:
        """Parse only the bytes appended since `self.covers` (spans.jsonl is append-only). `handle`
        is `get_index`'s already-validated descriptor — see `_rebuild`."""
        if size <= self.covers:
            return
        with _reading(self.path, handle, self.covers) as f:
            # Same bounded stream as `_rebuild`: an appended tail is usually small, but the first
            # top-up after a long detach can be arbitrarily large.
            records, consumed = _scan_light_stream(
                f, self.covers, size - self.covers, label="trace tail")   # parsed OUTSIDE the lock
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
        with open(self.path, "rb") as f:
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
        tids = self.node_tids.get(str(node_id), ())
        if generation is None:
            return sorted(r for tid in tids for r in self.by_tid.get(tid, ()))
        rows: list[int] = []
        for tid in tids:
            trace_rows = list(self.by_tid.get(tid, ()))
            if not trace_rows:
                continue
            root = trace_root_span([self.light[row] for row in trace_rows], _normalized=True)
            # A rootless trace is a parent_id CYCLE (corrupt/crafted source only). The shared rule
            # declines to name a root there rather than nominating an arbitrary span, and this
            # follows it into the unstamped default below — the same answer the views reach, instead
            # of reading one span's `generation` as if it were the whole trace's.
            attributes = root.get("attributes") if root is not None else None
            raw_generation = (
                attributes.get("generation") if isinstance(attributes, dict) else None
            )
            trace_generation = (
                raw_generation
                if type(raw_generation) is int and raw_generation >= 0
                else 0
            )
            if trace_generation == generation:
                rows.extend(trace_rows)
        return sorted(rows)

    def light_spans_for_node(self, node_id, limit: Optional[int] = None, *,
                             generation: Optional[int] = None) -> list[dict]:
        """The LIGHT spans of the traces attributed to this node — IN-MEMORY, no disk read (unlike
        `full_spans_for_node`, which seeks each span's full I/O). Lets the node-detail timeline build
        O(node) instead of O(whole run): `build_trace_view(light=True)` over just these yields the SAME
        `nodes[nid]`/`rollup` as over ALL spans, because a span's effective node (its own node_id, else
        its trace root's) is N iff it lives in one of N's traces — exactly what `node_tids` collects.
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

    def full_spans_for_node(self, node_id, limit: Optional[int] = None) -> list[dict]:
        """Every FULL span in the traces attributed to this node (a node's create_node + evaluate +
        repair traces). Matches `build_conversation`'s grouping: it reads spans by TRACE and shows a
        trace whose root carries this node_id — so we collect all traces that carry the node_id on any
        span and read every span in them (a harmless superset; build_conversation re-filters by the
        trace root's node_id, so an extra trace is dropped there, and a node-idless child of a matching
        trace is still included because we read by trace)."""
        with self._rlock:                              # snapshot rows — never iterate the live set/lists
            rows = sorted(r for tid in self.node_tids.get(str(node_id), ())
                          for r in self.by_tid.get(tid, ()))
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
                  "dev": self.identity[0], "ino": self.identity[1]}
        parts = [orjson.dumps(header)]
        for light, (off, length) in zip(self.light, self.meta):
            parts.append(orjson.dumps({**light, "_o": off, "_l": length}))
        try:
            atomic_write_bytes(self.path.with_name(_INDEX_NAME), b"\n".join(parts) + b"\n")
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


def _load_persisted(spans_path: Path, identity: tuple, size: int) -> Optional[SpanIndex]:
    """Load `spans.index.jsonl` if it is a valid, current index for this spans.jsonl (fast cold path:
    read ~16 MB instead of re-parsing 1 GB). Returns None on any mismatch → caller rebuilds. Coverage
    is DERIVED from the records actually read (not trusted from the header), so a torn index tail just
    means a smaller covered prefix that the caller tops up from spans.jsonl."""
    ip = spans_path.with_name(_INDEX_NAME)
    idx = SpanIndex(spans_path)
    try:
        with open(ip, "rb") as f:
            first = f.readline()
            if not first.endswith(b"\n"):
                return None
            try:
                header = orjson.loads(first.strip())
            except orjson.JSONDecodeError:
                return None
            if not isinstance(header, dict) or header.get("_idx") != _SCHEMA:
                return None
            if header.get("dev") != identity[0] or header.get("ino") != identity[1]:
                return None  # index was built for a different underlying file (reset/replace)
            last_end = 0
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # torn index tail
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
                        or off < 0 or length < 0 or off + length > size):
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
    idx._persisted_covers = idx.covers   # it IS persisted at this coverage — don't rewrite it unchanged
    if not _spotcheck(idx):
        return None
    return idx


def get_index(spans_path: str | os.PathLike) -> Optional[SpanIndex]:
    """Return the (incrementally-maintained, persisted) light span index for `spans_path`, or None if
    the file does not exist. Cached in-process; tops up from the appended tail on a hit, loads the
    persisted index or rebuilds on a cold miss. Thread-safe."""
    p = Path(spans_path)
    key = str(p)
    with span_index_write_guard(p):
        # OPEN FIRST, then `fstat` that same descriptor. `stat(path)` followed by `open(path)`
        # resolves the pathname twice: a rewrite in between would cache-key and validate inode A
        # while the probe — and every later read — observed inode B, i.e. a trace view stitched from
        # two generations of the file. One descriptor makes the identity, the size and the readability
        # proof all describe the same inode, and it folds the separate readability probe into the same
        # syscall: cached indexes are accelerators, not authority once the source is unreadable, so a
        # permission loss must never turn a previously indexed source into exact zero.
        try:
            handle = open(p, "rb")
        except FileNotFoundError:
            try:
                p.stat()
            except FileNotFoundError:
                if key not in _CACHE:
                    return None  # no spans.jsonl (tracing off / pre-tracing run) — caller degrades
                # This run has been OBSERVED to have spans. A source that has since disappeared is an
                # availability failure, not "the run produced no trace" — publishing empty would turn
                # a vanished/renamed file into false evidence. Let it reach the projection boundary,
                # which reports `unavailable`. (Every other OSError propagates unconditionally.)
                raise
            # `open()` reported ENOENT while a following stat still sees the path. This is an
            # availability race/failure, not evidence that the run produced no trace.
            raise
        with handle:
            return _index_from_handle(p, key, handle)


def _index_from_handle(p: Path, key: str, handle) -> SpanIndex:
    """Build/refresh the index for `p` reading exclusively through the caller's open descriptor.

    Split out only so the descriptor's lifetime is a plain `with` in `get_index`; the module `_LOCK`
    is held by that caller for the whole call.
    """
    stt = os.fstat(handle.fileno())
    size, mtime_ns = stt.st_size, stt.st_mtime_ns
    identity = (stt.st_dev, stt.st_ino)
    idx = _CACHE.get(key)
    if idx is not None:
        # Reuse the cached index only when spans.jsonl is the SAME file grown by pure appends —
        # mirrors EventStore.read_all's guard so a network/FUSE mount can't feed the trace view a
        # stale prefix: `replaced` = a new inode (atomic rewrite/reset), `same_size_rewrite` = an
        # in-place rewrite that kept the byte count (detected by a moved mtime), `shrank` = a
        # truncate/compaction. Any of these invalidates the byte offsets → fall through to reload.
        replaced = idx.identity != identity
        same_size_rewrite = (size == idx.covers and idx.mtime_ns is not None
                             and mtime_ns != idx.mtime_ns)
        shrank = size < idx.covers
        if not (replaced or same_size_rewrite or shrank):
            if idx.covers < size:
                idx._topup(size, handle)    # parse only the appended tail
            idx.mtime_ns = mtime_ns
            _CACHE.move_to_end(key)
            idx._persist()
            return idx
    # Cold miss (not cached, replaced, or shrank): load the persisted index if valid, else rebuild.
    idx = _load_persisted(p, identity, size)
    if idx is None:
        idx = SpanIndex(p)
        idx.identity = identity
        idx._rebuild(size, handle)
    elif idx.covers < size:
        idx._topup(size, handle)
    idx.mtime_ns = mtime_ns
    _CACHE[key] = idx
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    idx._persist()
    return idx


def invalidate(spans_path: str | os.PathLike) -> None:
    """Drop the cached index for a run (after clear_trace/reset rewrites spans.jsonl). The identity/
    size guards in `get_index` already catch a replaced file, so this is belt-and-suspenders."""
    with _LOCK:
        _CACHE.pop(str(Path(spans_path)), None)
