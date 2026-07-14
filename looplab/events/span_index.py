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
failure (identity/size mismatch, corruption, offset drift, missing file) falls back to a full rebuild
from `spans.jsonl`, so the result is always byte-identical to reading `spans.jsonl` directly — worst
case is "as slow as before", never wrong. `spans.jsonl` remains the sole source of truth.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Optional

import orjson

from looplab.core.atomicio import atomic_write_bytes
from looplab.events.traceview import _strip_span_io

# Bump when the persisted record shape changes so an old `spans.index.jsonl` is ignored (rebuilt),
# never mis-read. The index is a cache — a version skew simply triggers one rebuild.
_SCHEMA = 3
_INDEX_NAME = "spans.index.jsonl"
# Geometric re-persist factor (see `_persist`): re-write the persisted index only when the indexed
# span bytes have grown by this factor since the last write. Bounds a live run's total index-write
# volume to ~O(n) (a handful of full-object PUTs on S3/geesefs) instead of ~O(n²) full rewrites every
# few MB. The first index (from covers 0) always persists.
_PERSIST_GROWTH = 1.5
# Bound the in-process cache: a 1 GB run's light spans are ~220 MB of Python dicts, so hold only a few
# (the user views one run at a time; an evicted index just reloads its persisted form, not a rescan).
_CACHE_MAX = 3

_CACHE: "OrderedDict[str, SpanIndex]" = OrderedDict()
_LOCK = threading.Lock()


def _scan_light(buf: bytes, base: int) -> tuple[list[tuple[dict, int, int]], int]:
    """Parse complete JSONL lines from `buf` (a slice of spans.jsonl starting at file offset `base`),
    applying `iter_jsonl`'s durability rules (stop at the first torn/corrupt line). Yields
    `(light_span, off, length)` where `off` is the line-start offset IN THE FILE and `length` is the
    line length WITHOUT the trailing newline — so a reader can `seek(off); read(length)` to recover
    the FULL span line verbatim. Returns `(records, consumed)`; `consumed` lands on a newline boundary
    (the exact prefix `iter_jsonl` would have accepted), so it is the index's coverage watermark."""
    records: list[tuple[dict, int, int]] = []
    n = len(buf)
    i = 0
    consumed = 0
    while i < n:
        nl = buf.find(b"\n", i)
        if nl == -1:
            break  # torn final line (no newline yet) — leave for a later top-up
        raw = buf[i:nl]
        line = raw.strip()
        if line:
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                break  # corrupt tail — stop cleanly (matches iter_jsonl)
            if not isinstance(obj, dict):
                break  # valid JSON but non-object => corruption, not a span
            records.append((_strip_span_io(obj), base + i, nl - i))
        i = nl + 1
        consumed = i
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
    def _append(self, light: dict, off: int, length: int) -> None:
        row = len(self.light)
        self.light.append(light)
        self.meta.append((off, length))
        sid = light.get("span_id")
        if sid is not None:
            self.by_sid[sid] = row
        tid = light.get("trace_id")
        if tid is not None:
            self.by_tid[tid].append(row)
            nid = (light.get("attributes") or {}).get("node_id")
            if nid is not None:
                self.node_tids[str(nid)].add(tid)

    def _extend(self, records: list[tuple[dict, int, int]]) -> None:
        for light, off, length in records:
            self._append(light, off, length)

    def _rebuild(self, size: int) -> None:
        try:
            with open(self.path, "rb") as f:
                buf = f.read(size)
        except OSError:
            buf = b""
        records, consumed = _scan_light(buf, 0)      # parse OUTSIDE the lock (the slow part)
        with self._rlock:                            # publish the new maps atomically vs a lock-free read
            self.light.clear()
            self.meta.clear()
            self.by_sid.clear()
            self.by_tid.clear()
            self.node_tids.clear()
            self._extend(records)
            self.covers = consumed

    def _topup(self, size: int) -> None:
        """Parse only the bytes appended since `self.covers` (spans.jsonl is append-only)."""
        if size <= self.covers:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.covers)
                buf = f.read(size - self.covers)
        except OSError:
            return
        records, consumed = _scan_light(buf, self.covers)   # parse OUTSIDE the lock
        with self._rlock:                                   # append is atomic vs a lock-free read
            self._extend(records)
            self.covers = consumed

    # -- reads (snapshot the in-memory maps under `_rlock`, then do disk I/O outside it) ---------
    def light_spans(self) -> list[dict]:
        """A SNAPSHOT of the light span list (for `build_trace_view(light=True)`). A copy, not a live
        reference: build_trace_view iterates it while a concurrent `_topup` may append to `self.light`,
        and a plain-reference iteration would silently pick up half-appended tail spans. The dicts
        inside are shared (never mutated after creation), so the copy is shallow and cheap (~ms)."""
        with self._rlock:
            return list(self.light)

    def _read_full(self, rows: list[int]) -> list[dict]:
        """Read the FULL (uncapped) span lines for the given rows by seeking to their byte offsets —
        so a per-node/-trace/-span detail view touches only those bytes, not the whole file. `rows`
        is a snapshot taken under `_rlock` by the caller; `self.meta` is append-only, so reading
        `meta[r]` here (outside the lock) is safe — a concurrent append never moves an existing row."""
        out: list[dict] = []
        try:
            with open(self.path, "rb") as f:
                for r in sorted(rows):                 # sorted → mostly-sequential reads
                    off, length = self.meta[r]
                    f.seek(off)
                    data = f.read(length)
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
                    expected = self.light[r].get("span_id")
                    got_id = obj.get("span_id")
                    if expected is not None and got_id is not None and got_id != expected:
                        continue
                    out.append(obj)
        except OSError:
            return out
        return out

    def full_span(self, sid: str) -> Optional[dict]:
        with self._rlock:
            row = self.by_sid.get(sid)
        if row is None:
            return None
        got = self._read_full([row])
        return got[0] if got else None

    def full_spans_for_trace(self, tid: str) -> list[dict]:
        with self._rlock:
            rows = list(self.by_tid.get(tid, ()))
        return self._read_full(rows)

    def light_spans_for_node(self, node_id) -> list[dict]:
        """The LIGHT spans of the traces attributed to this node — IN-MEMORY, no disk read (unlike
        `full_spans_for_node`, which seeks each span's full I/O). Lets the node-detail timeline build
        O(node) instead of O(whole run): `build_trace_view(light=True)` over just these yields the SAME
        `nodes[nid]`/`rollup` as over ALL spans, because a span's effective node (its own node_id, else
        its trace root's) is N iff it lives in one of N's traces — exactly what `node_tids` collects."""
        with self._rlock:
            return [self.light[r] for tid in self.node_tids.get(str(node_id), ())
                    for r in self.by_tid.get(tid, ())]

    def full_spans_for_node(self, node_id) -> list[dict]:
        """Every FULL span in the traces attributed to this node (a node's create_node + evaluate +
        repair traces). Matches `build_conversation`'s grouping: it reads spans by TRACE and shows a
        trace whose root carries this node_id — so we collect all traces that carry the node_id on any
        span and read every span in them (a harmless superset; build_conversation re-filters by the
        trace root's node_id, so an extra trace is dropped there, and a node-idless child of a matching
        trace is still included because we read by trace)."""
        with self._rlock:                              # snapshot rows — never iterate the live set/lists
            rows = [r for tid in self.node_tids.get(str(node_id), ()) for r in self.by_tid.get(tid, ())]
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
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError:
                    break
                if not isinstance(rec, dict):
                    break
                off = rec.pop("_o", None)
                length = rec.pop("_l", None)
                if (not isinstance(off, int) or isinstance(off, bool)
                        or not isinstance(length, int) or isinstance(length, bool)
                        or off < 0 or length < 0 or off + length > size):
                    # A corrupt/out-of-bounds offset from a damaged persisted index must never reach
                    # `f.read(length)` (a negative length reads the whole file into memory). Treat it
                    # like a torn tail: keep the valid prefix, rebuild the rest from spans.jsonl.
                    break
                idx._append(rec, off, length)
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
    with _LOCK:
        try:
            stt = p.stat()
        except OSError:
            return None  # no spans.jsonl (tracing off / pre-tracing run) — caller degrades
        size, mtime_ns = stt.st_size, stt.st_mtime_ns
        identity = (stt.st_dev, stt.st_ino)
        key = str(p)
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
                    idx._topup(size)            # parse only the appended tail
                idx.mtime_ns = mtime_ns
                _CACHE.move_to_end(key)
                idx._persist()
                return idx
        # Cold miss (not cached, replaced, or shrank): load the persisted index if valid, else rebuild.
        idx = _load_persisted(p, identity, size)
        if idx is None:
            idx = SpanIndex(p)
            idx.identity = identity
            idx._rebuild(size)
        elif idx.covers < size:
            idx._topup(size)
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
