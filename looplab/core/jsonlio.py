"""Generic JSONL store I/O — the mutable-store readers/writers and the append-only line classifier.

Moved verbatim out of `events/eventstore.py` (doc 25 EV-12). Nothing here is event-related: these are
consumed by lessons, memory, the knowledge/memory tools, trust/harden, the spans reader and the CLI,
and they only ever knew about JSON lines and atomic rewrites. `eventstore` kept them because it was
the module that happened to need them first, which put generic file plumbing inside the package whose
job is the append-only event log — and made `events` look like a dependency of subsystems that only
wanted to read a JSONL file.

The distinction these functions draw is the load-bearing part and it survives the move intact:
`iter_jsonl` STOPS at the first bad line, because in an append-only log a bad line is a torn tail and
everything after it is unproven; `read_jsonl_lenient` SKIPS bad lines and continues, because a store
that is rewritten in place must not let one damaged line hide everything after it. Picking the wrong
one is a durable correctness bug, not a style choice.

`events/eventstore.py` re-exports every name, so both spellings resolve to the SAME objects and
existing imports and monkeypatch seams are unaffected.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

import orjson


class JsonlRecordInvalid(ValueError):
    """This physical line ENDS the recoverable prefix of an append-only JSONL log.

    Not "skip me": every append-only reader below stops here, because in a log that is only ever
    appended to, a damaged line means the bytes after it were written behind damage the reader
    cannot interpret — accepting them would silently reorder or resurrect records. Mutable stores
    step over the same condition on purpose via `read_jsonl_lenient`; that policy split is the whole
    reason both readers exist, and it is why this is a distinct exception rather than a bare False.
    """


def decode_jsonl_line(raw: bytes) -> Optional[dict]:
    """Classify ONE newline-stripped physical JSONL line under the append-only prefix rule.

    Three outcomes: the record for a valid line, ``None`` for a blank one (skip it — whitespace is
    not damage, and stopping there would truncate a log at a stray newline), and
    ``JsonlRecordInvalid`` for anything else.

    Five readers used to re-derive these outcomes inline (doc 25 EV-05) — `iter_jsonl`,
    `_parse_jsonl_region`, `log_divergence`, `span_index`'s two scans and the span-tail fallback in
    `serve/routers/runs.py` — and their agreement was maintained by comments reading "matches
    iter_jsonl" rather than by shared code. It is not a cosmetic agreement: each reader's accepted
    byte count is a durable watermark, so a reader that accepts one line more than `read_all` claims
    coverage of bytes the event log treats as damage.

    Both rejections are load-bearing. `not isinstance(obj, dict)` is a STOP rather than a skip: a
    valid-JSON non-object line in an append-only log is corruption, not an unknown record type. And
    the parser must be the one the writer used — stdlib `json` accepts the NaN/Infinity literals
    orjson rejects, so swapping it here would classify writer-valid lines as damage.
    """
    line = raw.strip()
    if not line:
        return None
    try:
        obj = orjson.loads(line)
    except orjson.JSONDecodeError as exc:
        raise JsonlRecordInvalid("line is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise JsonlRecordInvalid("valid JSON, but not an object")
    return obj


def scan_jsonl_region(buf: bytes) -> tuple[list[tuple[dict, int, int]], int]:
    """Parse complete records from a byte buffer under the same append-only prefix rule.

    Returns ``(records, consumed)``. Each record is ``(obj, start, end)``: ``start`` is the offset of
    the line's first byte within ``buf`` and ``end`` the offset of its terminating newline, so
    ``buf[start:end]`` is the line verbatim and ``end + 1`` is the boundary after it.

    ``consumed`` always lands ON a newline boundary and NEVER covers a rejected line — a torn final
    line (no newline yet) and a corrupt line are both left unconsumed, so a caller that tops the
    buffer up later re-examines exactly those bytes and a completed torn line is picked up then.
    Blank lines ARE consumed: they are part of the valid prefix, they just produce no record.
    """
    records: list[tuple[dict, int, int]] = []
    consumed = 0
    i, n = 0, len(buf)
    while i < n:
        nl = buf.find(b"\n", i)
        if nl == -1:
            break                       # torn final write — leave it for a later top-up
        try:
            obj = decode_jsonl_line(buf[i:nl])
        except JsonlRecordInvalid:
            break                       # corrupt tail — stop cleanly, don't advance past it
        if obj is not None:
            records.append((obj, i, nl))
        i = nl + 1
        consumed = i
    return records, consumed


def iter_jsonl(path: str | os.PathLike) -> Iterator[dict]:
    """Yield dict records from an append-only JSONL file, tolerating a torn/partial final line
    (a crash mid-append): stop at the first line without a trailing newline or that fails to
    parse. This helper is deliberately FORMAT-AGNOSTIC: chat, assistant-message and span stores also use
    it, and a foreign row whose ordinary ``type`` happens to equal an internal event type must round-trip
    unchanged. Event-log consumers use :func:`iter_event_jsonl` below.

    Streams line by line rather than going through `scan_jsonl_region`: this reader is used on
    multi-hundred-MB span logs, and holding the whole file as one buffer to reuse the walk would
    trade the duplication for the memory. The line RULE is shared, which is the part that drifted."""
    p = Path(path)
    if not p.exists():
        return
    with open(p, "rb") as f:
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # torn final write — ignore the partial record
            try:
                obj = decode_jsonl_line(raw)
            except JsonlRecordInvalid:
                break  # corrupt tail (unparseable, or valid JSON that is not an object)
            if obj is None:
                continue  # blank line
            yield obj

def read_jsonl_lenient_with_health(path: str | os.PathLike, *, loads=orjson.loads,
                                   keep_bad: bool = False, dicts_only: bool = True,
                                   errors: str = "strict") -> tuple[list, dict]:
    """Read a mutable JSONL store and return its additive quarantine receipt.

    The ordinary lenient reader intentionally skips damaged rows so one bad mutable-store record cannot
    hide every later record.  Consumers that make completeness or absence claims also need to know that a
    row was skipped.  This companion keeps the existing returned rows byte-for-byte compatible while
    reporting non-blank source lines that were not accepted by the selected parser/shape contract.
    """
    p = Path(path)
    out: list = []
    source_lines = malformed_lines = invalid_shape_lines = 0
    try:
        # one binary snapshot both preserves invalid UTF-8 as a quarantinable row and keeps
        # non-absence OSErrors visible to the caller. A preflight exists()/second read would introduce a
        # TOCTOU window and could launder an unreadable store into an exact empty source.
        raw_file = p.read_bytes()
    except FileNotFoundError:
        return out, {
            "read_complete": True,
            "source_lines": 0,
            "accepted_rows": 0,
            "invalid_lines": 0,
            "malformed_lines": 0,
            "invalid_shape_lines": 0,
        }
    # Split only on the JSONL record delimiter. ``bytes.splitlines`` would also split bare CR, form-feed,
    # vertical-tab and other bytes that are part of one poisoned record. Drop only the synthetic element
    # after a terminal LF so ``keep_bad`` remains identical to ``str.splitlines`` for blank records.
    raw_lines = raw_file.split(b"\n") if raw_file else []
    if raw_file.endswith(b"\n"):
        raw_lines.pop()
    for raw in raw_lines:
        rec = None
        try:
            line = raw.decode("utf-8", errors=errors)
            # A CR in CRLF is the line terminator, while a bare/mid-record CR above deliberately remains
            # data. JSON parsers accept trailing whitespace, but removing exactly this one byte also keeps
            # blank-line/keep_bad behavior byte-compatible with the former ``read_text().splitlines()``.
            if line.endswith("\r"):
                line = line[:-1]
        except UnicodeDecodeError:
            line = None
        nonblank = bool(line.strip()) if line is not None else bool(raw.strip())
        if nonblank:
            source_lines += 1
            if line is None:
                malformed_lines += 1
            else:
                try:
                    v = loads(line)
                    if not dicts_only or isinstance(v, dict):
                        rec = v
                    else:
                        invalid_shape_lines += 1
                except Exception:  # noqa: BLE001 — any unparseable line is damage to step over
                    malformed_lines += 1
        if rec is not None:
            out.append(rec)
        else:
            if keep_bad:
                out.append(None)
    invalid_lines = malformed_lines + invalid_shape_lines
    return out, {
        "read_complete": invalid_lines == 0,
        "source_lines": source_lines,
        "accepted_rows": source_lines - invalid_lines,
        "invalid_lines": invalid_lines,
        "malformed_lines": malformed_lines,
        "invalid_shape_lines": invalid_shape_lines,
    }


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
    rows, _health = read_jsonl_lenient_with_health(
        path, loads=loads, keep_bad=keep_bad, dicts_only=dicts_only, errors=errors)
    return rows


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


def replace_jsonl_rows_atomic_preserving_quarantine(
        path: str | os.PathLike, rows, *, replace_if, loads=orjson.loads,
        dumps=orjson.dumps) -> None:
    """Atomically replace selected decoded rows without erasing unreadable/future records.

    Mutable-store upserts historically went through ``read_jsonl_lenient`` followed by a whole-file
    rewrite.  That silently deleted every quarantined raw line and made the next health receipt look
    complete.  Preserve every non-blank raw line byte-for-byte unless it decodes to an object explicitly
    superseded by ``replace_if``; append the replacement rows in the store's normal encoding.  An explicit
    repair/migration, rather than an unrelated write, remains the only operation allowed to discard damage.

    Callers must hold their store's interprocess lock around this helper.
    """
    from looplab.core.atomicio import atomic_write_bytes

    p = Path(path)
    retained: list[bytes] = []
    if p.exists():
        # Split only on the JSONL delimiter. ``bytes.splitlines`` also treats form-feed / vertical-tab as
        # boundaries and would violate the byte-preservation promise for a poisoned raw record.
        for raw in p.read_bytes().split(b"\n"):
            if not raw.strip():
                continue
            try:
                # parse with the same UTF-8 text contract as read_jsonl_lenient. Python's
                # json.loads(bytes) accepts a BOM that json.loads(str) rejects; using bytes here could
                # therefore classify a reader-quarantined row as understood and erase it during upsert.
                decoded = loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001 — this is the raw quarantine we must retain
                retained.append(raw)
                continue
            if isinstance(decoded, dict) and replace_if(decoded):
                continue
            retained.append(raw)

    def _line(value) -> bytes:
        encoded = dumps(value)
        return encoded if isinstance(encoded, bytes) else encoded.encode("utf-8")

    retained.extend(_line(row) for row in rows)
    atomic_write_bytes(p, b"".join(raw + b"\n" for raw in retained))
