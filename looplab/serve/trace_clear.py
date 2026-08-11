"""Durable write-ahead implementation of ``POST /api/runs/{id}/nodes/{nid}/clear_trace`` (doc 25 SR-03).

This is a complete crash-recoverable state machine — pending/succeeded/superseded receipts, a
digest-CAS on ``spans.jsonl``, and recovery ownership after a process death — and it lived as
seventeen closures inside ``build_router``. Its two sibling destructive operations already have
dedicated modules (``reset_route.py::durable_reset_run``, ``deletion_service.py``), so the closure
form was the odd one out: nothing here captures router state, and being closures made every branch
of the receipt machine reachable only by building the whole ASGI app and driving HTTP.

Bodies are verbatim moves from ``serve/routers/control.py``; the only edits are threading ``srv``
(previously captured) as an explicit first argument, ``srv.run_dir`` in place of the captured
``_run_dir``, and taking ``known_engine_liveness`` as a parameter so the fail-closed liveness verdict
stays the router's — its call sites there share one definition and one patch seam.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import (
    file_identity, strict_atomic_write_text, strict_fsync, strict_replace,
    strict_fsync_parent)
from looplab.core.trace_files import (
    TRACE_JSONL_ROW_MAX_BYTES, assert_private_trace_file, open_private_trace_file,
    trace_file_identity)
from looplab.events.eventstore import EventStoreLockError
from looplab.events.span_index import span_destructive_write_guard
from looplab.events.traceview import (
    _normalize_span, effective_node_id, trace_file_revision, trace_root_node_id)
from looplab.serve.appstate import _TRACE_CLEAR_RECEIPT_PREFIX
from looplab.serve.engine_proc import (
    _engine_alive, _engine_liveness, _fresh_resume_launch_pending, engine_write_lock_http,
    run_lifecycle_lock_http)
from looplab.serve.protocol import EXPECTED_RUN_GENERATION_FIELD


_TRACE_CLEAR_OPERATION_RE = re.compile(r"^tc_[0-9a-f]{32}$")
_TRACE_CLEAR_RECEIPT_MAX_BYTES = 64 * 1024


def _trace_clear_receipt_lstat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HTTPException(503, {
            "code": "trace_clear_receipt_unavailable",
            "message": "The durable trace clear receipt path could not be inspected.",
            "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
        }) from exc


def _trace_clear_receipt_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _trace_clear_regular_receipt(path: Path) -> Optional[os.stat_result]:
    info = _trace_clear_receipt_lstat(path)
    if info is None:
        return None
    if (_trace_clear_receipt_reparse(info) or not stat.S_ISREG(info.st_mode)
            or int(getattr(info, "st_nlink", 1)) != 1):
        raise HTTPException(409, {
            "code": "trace_clear_receipt_path_invalid",
            "message": "A trace clear receipt must be a regular run-root file.",
            "remediation": "Remove the conflicting non-receipt entry before retrying.",
        })
    return info


def _trace_clear_receipt_path(srv, rd: Path, operation_id: str) -> Path:
    if _TRACE_CLEAR_OPERATION_RE.fullmatch(operation_id) is None:
        raise HTTPException(400, "operation_id must be tc_ followed by 32 lowercase hex digits")
    sequence_path = srv.commands._sequence_path(rd)
    # Keep durable operation receipts directly in the already-established run root. The command
    # lock directory is intentionally ephemeral and may have been created with ordinary mkdir;
    # putting a write-ahead record inside its first incarnation could therefore lose the whole
    # directory entry after power failure even though the receipt file itself was synced.
    path = sequence_path.parent.parent / (
        f"{_TRACE_CLEAR_RECEIPT_PREFIX}{sequence_path.stem}.{operation_id}.json")
    _trace_clear_regular_receipt(path)
    return path


def _valid_trace_clear_result(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("removed")) is int and value["removed"] >= 0
        and type(value.get("kept")) is int and value["kept"] >= 0
    )


def _load_trace_clear_receipt(path: Path) -> Optional[dict[str, Any]]:
    before = _trace_clear_regular_receipt(path)
    if before is None:
        return None
    try:
        with open_private_trace_file(
                path, "rb", expected_identity=trace_file_identity(before)) as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_size > _TRACE_CLEAR_RECEIPT_MAX_BYTES:
                raise HTTPException(503, {
                    "code": "trace_clear_receipt_unavailable",
                    "message": "The durable trace clear receipt exceeds its server-owned bound.",
                    "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
                })
            raw = stream.read(_TRACE_CLEAR_RECEIPT_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) != opened.st_size
                    or file_identity(after) != file_identity(opened)):
                raise OSError("trace clear receipt changed while it was being read")
        after_entry = _trace_clear_regular_receipt(path)
        if after_entry is None or file_identity(after_entry) != file_identity(before):
            raise OSError("trace clear receipt changed while it was being read")
        value = json.loads(raw.decode("utf-8"))
    except HTTPException:
        raise
    except OSError as exc:
        if exc.errno in {errno.EISDIR, errno.ELOOP, errno.EPERM, errno.EINVAL}:
            raise HTTPException(409, {
                "code": "trace_clear_receipt_path_invalid",
                "message": "A trace clear receipt must be one private regular run-root file.",
                "remediation": "Remove the conflicting non-receipt entry before retrying.",
            }) from exc
        raise HTTPException(503, {
            "code": "trace_clear_receipt_unavailable",
            "message": "The durable trace clear receipt is unreadable.",
            "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
        }) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(503, {
            "code": "trace_clear_receipt_unavailable",
            "message": "The durable trace clear receipt is unreadable.",
            "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
        }) from exc
    if (not isinstance(value, dict)
            or value.get("version") != 2
            or value.get("status") not in {"pending", "succeeded", "superseded"}
            or not isinstance(value.get("id"), str)
            or _TRACE_CLEAR_OPERATION_RE.fullmatch(value["id"]) is None
            or not isinstance(value.get("expected_generation"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["expected_generation"]) is None
            or not isinstance(value.get("expected_trace_revision"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["expected_trace_revision"]) is None
            or type(value.get("node_id")) is not int
            or type(value.get("node_generation")) is not int
            or value["node_generation"] < 0
            or type(value.get("source_exists")) is not bool
            or type(value.get("result_exists")) is not bool
            or not isinstance(value.get("source_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["source_digest"]) is None
            or not isinstance(value.get("result_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["result_digest"]) is None
            or not _valid_trace_clear_result(value.get("result"))):
        raise HTTPException(503, {
            "code": "trace_clear_receipt_unavailable",
            "message": "The durable trace clear receipt is malformed.",
            "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
        })
    return value


def _pending_trace_clear_for_lifecycle(
        srv, rd: Path, *, receipt_path: Path, expected_generation: str,
        expected_trace_revision: str, nid: int,
        node_generation: int) -> Optional[dict[str, Any]]:
    sequence_path = srv.commands._sequence_path(rd)
    pattern = f"{_TRACE_CLEAR_RECEIPT_PREFIX}{sequence_path.stem}.tc_*.json"
    for path in sequence_path.parent.parent.glob(pattern):
        if path == receipt_path:
            continue
        # A pre-upgrade run directory may already occupy the now-reserved namespace; it is not a
        # receipt. Symlinks and other suspicious matching entries still fail closed in the loader.
        info = _trace_clear_receipt_lstat(path)
        if (info is not None and stat.S_ISDIR(info.st_mode)
                and not _trace_clear_receipt_reparse(info)):
            continue
        receipt = _load_trace_clear_receipt(path)
        if (receipt is not None
                and receipt.get("status") == "pending"
                and receipt.get("expected_generation") == expected_generation
                and receipt.get("expected_trace_revision") == expected_trace_revision
                and receipt.get("node_id") == nid
                and receipt.get("node_generation") == node_generation):
            return receipt
    return None


def _save_trace_clear_receipt(path: Path, value: dict[str, Any]) -> None:
    try:
        strict_atomic_write_text(
            path, json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(503, {
            "code": "trace_clear_receipt_unavailable",
            "message": "The trace clear receipt could not be published durably.",
            "remediation": "Inspect trace-clear receipt storage in the run root before retrying.",
        }) from exc


def _trace_clear_pending(operation_id: str, message: str) -> HTTPException:
    return HTTPException(425, {
        "code": "trace_clear_pending",
        "operation_id": operation_id,
        "message": message,
        "remediation": "Verify this same operation again; do not submit a new clear.",
    })


@contextmanager
def _trace_clear_guard(srv, rd: Path, operation_id: str, pending: bool):
    """Preserve an already-pending identity when outer ownership cannot yet be acquired."""
    entered = False
    try:
        with (srv.commands.destructive_guard(rd, "clear node trace") as canonical,
              run_lifecycle_lock_http(canonical)):
            entered = True
            yield canonical
    except HTTPException:
        if pending and not entered:
            raise _trace_clear_pending(
                operation_id,
                "The original clear is waiting for exclusive run lifecycle ownership.")
        raise


def _trace_clear_receipt_result(
        receipt: Optional[dict[str, Any]], *, receipt_path: Path, operation_id: str,
        expected_generation: str, expected_trace_revision: str,
        nid: int, node_generation: int) -> Optional[dict[str, Any]]:
    if receipt is None:
        return None
    identity = (
        receipt.get("id") == operation_id
        and receipt.get("expected_generation") == expected_generation
        and receipt.get("expected_trace_revision") == expected_trace_revision
        and receipt.get("node_id") == nid
        and receipt.get("node_generation") == node_generation
    )
    if not identity:
        raise HTTPException(409, {
            "code": "trace_clear_operation_conflict",
            "message": "This trace clear operation id belongs to a different lifecycle.",
            "remediation": "Keep the original operation identity; do not reuse its id.",
        })
    if receipt.get("status") == "pending":
        # A same-id retry must enter the run sequencer. If the original owner is still alive it
        # waits (or times out ambiguously); after a process crash it becomes the recovery owner.
        return None
    if receipt.get("status") == "superseded":
        # A strict write may fail after its atomic replace became visible but before parent
        # durability was confirmed. Never expose a terminal receipt until this observer has
        # re-published the exact terminal bytes successfully.
        _save_trace_clear_receipt(receipt_path, receipt)
        raise HTTPException(409, {
            "code": "trace_clear_operation_superseded",
            "operation_id": operation_id,
            "message": (
                "The trace changed after an interrupted clear, so its original outcome can no "
                "longer be reconstructed. The operation is closed and will not mutate again."),
            "remediation": "Reload the current trace and form a new confirmation if needed.",
        })
    result = receipt.get("result")
    if not _valid_trace_clear_result(result):
        raise HTTPException(503, {
            "code": "trace_clear_receipt_unavailable",
            "message": "The completed trace clear receipt has no valid result.",
            "remediation": "Inspect the trace-clear sidecar; do not submit a new clear.",
        })
    _save_trace_clear_receipt(receipt_path, receipt)
    return {
        "ok": True,
        "status": "succeeded",
        "operation_id": operation_id,
        "removed": result["removed"],
        "kept": result["kept"],
    }


_TRACE_CLEAR_STREAM_CHUNK = 1024 * 1024
# Root attribution needs one tiny shell per complete JSON-object row. Bound that graph separately
# from payload streaming: a file with millions of one-byte-ish objects must fail before WAL/staging
# rather than turning a destructive request into an unbounded metadata allocation.
_TRACE_CLEAR_METADATA_ROW_MAX = 100_000


@dataclass(frozen=True)
class _TraceDigestSnapshot:
    """Metadata-only receipt for one descriptor-bound trace snapshot."""

    exists: bool
    digest: str
    size: int


@dataclass
class _PreparedTrace:
    """A filtered result staged on disk, never materialized beside its source in memory."""

    source: _TraceDigestSnapshot
    result_exists: bool
    result_digest: str
    result_size: int
    result: dict[str, int]
    temporary: Optional[Path]

    def cleanup(self) -> None:
        if self.temporary is None:
            return
        try:
            self.temporary.unlink(missing_ok=True)
        except OSError:
            pass
        self.temporary = None


def _trace_path_error(exc: OSError) -> HTTPException:
    """Map a private-sidecar policy failure separately from an availability failure."""
    if exc.errno == errno.EFBIG:
        return HTTPException(409, {
            "code": "trace_row_too_large",
            "message": (
                "spans.jsonl contains a physical row above the supported trace envelope limit."),
            "remediation": (
                "Repair or archive the oversized diagnostics row before clearing trace data."),
        })
    if exc.errno == errno.E2BIG:
        return HTTPException(409, {
            "code": "trace_metadata_too_large",
            "message": "spans.jsonl has too many rows for one safe root-aware trace rewrite.",
            "remediation": (
                "Archive or compact trace diagnostics before clearing or purging node trace data."),
        })
    if exc.errno in {errno.EISDIR, errno.ELOOP, errno.EPERM, errno.EINVAL}:
        return HTTPException(409, {
            "code": "trace_path_invalid",
            "message": "spans.jsonl must be one private regular run-owned file.",
            "remediation": "Restore the run-owned trace file before clearing diagnostics.",
        })
    return HTTPException(503, {
        "code": "trace_revision_unavailable",
        "message": "The current trace contents could not be verified.",
        "remediation": "Inspect spans.jsonl storage before clearing diagnostics.",
    })


def _read_exact_chunks(stream, size: int):
    """Yield exactly one snapshotted byte range through a fixed-size buffer."""
    remaining = max(0, int(size))
    while remaining:
        chunk = stream.read(min(_TRACE_CLEAR_STREAM_CHUNK, remaining))
        if not chunk:
            raise OSError("short read while snapshotting spans.jsonl")
        remaining -= len(chunk)
        yield chunk


def _trace_digest_snapshot(path: Path) -> _TraceDigestSnapshot:
    """Hash one exact trace snapshot without ever retaining its payload bytes."""
    try:
        with open_private_trace_file(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            for chunk in _read_exact_chunks(stream, before.st_size):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if file_identity(after) != file_identity(before):
                raise OSError("spans.jsonl changed while its digest was computed")
            return _TraceDigestSnapshot(True, digest.hexdigest(), before.st_size)
    except FileNotFoundError:
        return _TraceDigestSnapshot(False, hashlib.sha256().hexdigest(), 0)
    except OSError as exc:
        raise _trace_path_error(exc) from exc


def _attribution_row(line: bytes) -> tuple[bool, Optional[str], Optional[dict]]:
    """Return validity, explicit node id and the canonical light attribution shell for one row."""
    try:
        candidate = orjson.loads(line)
    except orjson.JSONDecodeError:
        return False, None, None
    if not isinstance(candidate, dict):
        return False, None, None
    attributes = candidate.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    explicit = str(attributes.get("node_id")) if "node_id" in attributes else None
    normalized = _normalize_span({
        "trace_id": candidate.get("trace_id"),
        "span_id": candidate.get("span_id"),
        "parent_id": candidate.get("parent_id"),
        "start": candidate.get("start"),
        "attributes": ({"node_id": attributes.get("node_id")}
                       if "node_id" in attributes else {}),
    })
    if normalized is not None:
        normalized = {
            "trace_id": normalized["trace_id"],
            "span_id": normalized["span_id"],
            "parent_id": normalized.get("parent_id"),
            "start": normalized.get("start", 0.0),
            "attributes": normalized.get("attributes") or {},
        }
    return True, explicit, normalized


def _consume_source_range(source, source_digest, result_digest, output, length: int, *,
                          retain: bool) -> None:
    """Hash one sequential source range and optionally copy it to the staged result."""
    remaining = max(0, length)
    while remaining:
        chunk = source.read(min(_TRACE_CLEAR_STREAM_CHUNK, remaining))
        if not chunk:
            raise OSError("short read while preparing filtered spans.jsonl")
        source_digest.update(chunk)
        if retain:
            output.write(chunk)
            result_digest.update(chunk)
        remaining -= len(chunk)


def _prepare_filtered_trace_snapshot(path: Path, node_ids: set[Any]) -> _PreparedTrace:
    """Stage a root-aware filtered trace using bounded payload memory.

    The first descriptor pass hashes source truth and keeps only byte ranges plus tiny attribution
    shells.  The second pass copies retained ranges to a sibling temporary file through a fixed-size
    buffer while hashing the result. Malformed/blank rows and a bounded torn EOF remain byte-for-byte;
    only complete JSON-object rows contribute to ``removed``/``kept``, matching the historical
    contract. A row over the shared writer/reader ceiling fails closed before any staged mutation.

    ``node_ids`` is intentionally a set: trace-clear passes one id today, while the run-snapshot
    purge can reuse the same hardened/root-aware primitive for a whole removed-node set later.
    """
    targets = {str(value) for value in node_ids}
    try:
        with open_private_trace_file(path, "rb") as source:
            before = os.fstat(source.fileno())
            source_digest = hashlib.sha256()
            records: list[tuple[int, int, Optional[str], Optional[dict]]] = []
            by_trace: dict[str, list[dict]] = {}
            pending = bytearray()
            line_start = absolute = 0
            for chunk in _read_exact_chunks(source, before.st_size):
                source_digest.update(chunk)
                cursor = 0
                while True:
                    newline = chunk.find(b"\n", cursor)
                    if newline < 0:
                        suffix = chunk[cursor:]
                        # The shared limit includes LF, so an unterminated suffix may retain MAX-1
                        # bytes. Refuse before extending the carry beyond that allocation boundary.
                        if len(pending) + len(suffix) >= TRACE_JSONL_ROW_MAX_BYTES:
                            raise OSError(
                                errno.EFBIG, "trace JSONL row exceeds the physical envelope")
                        pending.extend(suffix)
                        break
                    if (len(pending) + (newline - cursor) + 1
                            > TRACE_JSONL_ROW_MAX_BYTES):
                        raise OSError(
                            errno.EFBIG, "trace JSONL row exceeds the physical envelope")
                    if pending:
                        pending.extend(chunk[cursor:newline])
                        line = bytes(pending)
                        pending.clear()
                    else:
                        line = chunk[cursor:newline]
                    end = absolute + newline + 1
                    valid, explicit, normalized = (
                        _attribution_row(line) if line else (False, None, None))
                    if valid:
                        if len(records) >= _TRACE_CLEAR_METADATA_ROW_MAX:
                            raise OSError(
                                errno.E2BIG, "trace metadata row limit exceeded")
                        records.append((line_start, end, explicit, normalized))
                        if normalized is not None:
                            by_trace.setdefault(normalized["trace_id"], []).append(normalized)
                    line_start = end
                    cursor = newline + 1
                absolute += len(chunk)
            after = os.fstat(source.fileno())
            if file_identity(after) != file_identity(before):
                raise OSError("spans.jsonl changed while its filter was prepared")

            root_nodes = {
                trace_id: trace_root_node_id(spans, _normalized=True)
                for trace_id, spans in by_trace.items()
            }
            remove_ranges: list[tuple[int, int]] = []
            for start, end, explicit, normalized in records:
                attributed = (
                    effective_node_id(normalized, root_nodes.get(normalized.get("trace_id")))
                    if normalized is not None else None
                )
                if explicit in targets or (attributed is not None and str(attributed) in targets):
                    remove_ranges.append((start, end))

            snapshot = _TraceDigestSnapshot(
                True, source_digest.hexdigest(), before.st_size)
            removed = len(remove_ranges)
            result = {"removed": removed, "kept": len(records) - removed}
            if not remove_ranges:
                return _PreparedTrace(
                    snapshot, True, snapshot.digest, snapshot.size, result, None)

            fd, raw_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".{path.name}.clear.", suffix=".tmp")
            temporary = Path(raw_name)
            raw_fd: Optional[int] = fd
            try:
                output = os.fdopen(fd, "wb")
                raw_fd = None
                result_digest = hashlib.sha256()
                copied_source_digest = hashlib.sha256()
                with output:
                    source.seek(0)
                    cursor = 0
                    for start, end in remove_ranges:
                        if cursor < start:
                            _consume_source_range(
                                source, copied_source_digest, result_digest, output,
                                start - cursor, retain=True)
                        _consume_source_range(
                            source, copied_source_digest, result_digest, output,
                            end - start, retain=False)
                        cursor = end
                    if cursor < before.st_size:
                        _consume_source_range(
                            source, copied_source_digest, result_digest, output,
                            before.st_size - cursor, retain=True)
                    output.flush()
                    result_size = output.tell()
                copied_after = os.fstat(source.fileno())
                if (file_identity(copied_after) != file_identity(before)
                        or copied_source_digest.digest() != source_digest.digest()):
                    raise OSError("spans.jsonl changed while its filtered copy was prepared")
                return _PreparedTrace(
                    snapshot, True, result_digest.hexdigest(), result_size, result, temporary)
            except BaseException:
                if raw_fd is not None:
                    try:
                        os.close(raw_fd)
                    except OSError:
                        pass
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
    except FileNotFoundError:
        empty = hashlib.sha256().hexdigest()
        return _PreparedTrace(
            _TraceDigestSnapshot(False, empty, 0), False, empty, 0,
            {"removed": 0, "kept": 0}, None)
    except HTTPException:
        raise
    except OSError as exc:
        raise _trace_path_error(exc) from exc


def _strict_replace_prepared_trace(prepared: _PreparedTrace, destination: Path) -> None:
    """Durably publish one already-streamed sibling temporary file."""
    temporary = prepared.temporary
    if temporary is None:
        raise OSError("filtered trace has no staged replacement")
    before = os.lstat(temporary)
    assert_private_trace_file(before, temporary)

    def nofollow_opener(raw_path, flags):
        return os.open(
            raw_path,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
        )

    with open(temporary, "rb", opener=nofollow_opener) as stream:
        opened = os.fstat(stream.fileno())
        assert_private_trace_file(opened, temporary)
        if trace_file_identity(opened) != trace_file_identity(before):
            raise OSError("filtered trace temporary changed before publication")
        strict_fsync(stream.fileno())
    after = os.lstat(temporary)
    assert_private_trace_file(after, temporary)
    expected = trace_file_identity(before)
    if trace_file_identity(after) != expected:
        raise OSError("filtered trace temporary changed before publication")
    strict_replace(temporary, destination)
    prepared.temporary = None
    published = os.lstat(destination)
    assert_private_trace_file(published, destination)
    if trace_file_identity(published) != expected:
        raise OSError("filtered trace publication replaced the wrong source")
    strict_fsync_parent(destination)


def _invalidate_trace_clear(srv, rd: Path, sp: Path) -> None:
    # The persisted index stores byte offsets into spans.jsonl. Invalidate it on every recovered
    # success as well: a crash may have committed the trace replacement before this cleanup.
    from looplab.events.span_index import invalidate
    invalidate(sp)
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME
    try:
        # Both files are derived from the pre-rewrite byte layout.  Retire the append receipts too:
        # leaving their old contiguous chain beside a replacement cannot change trace truth, but it
        # needlessly forces the next index reader through a fail-closed rebuild.
        (rd / "spans.index.jsonl").unlink(missing_ok=True)
        (rd / SPAN_APPEND_JOURNAL_NAME).unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(503, {
            "code": "trace_clear_projection_unavailable",
            "message": "The trace changed, but its stale derived projections could not be retired.",
            "remediation": "Repair the run directory, then verify this same operation again.",
        }) from exc
    srv.invalidate_trace_view(rd)


def _complete_trace_clear(
        srv, rd: Path, sp: Path, receipt_path: Path,
        receipt: dict[str, Any]) -> dict[str, Any]:
    if receipt["result"]["removed"] > 0 or not receipt["source_exists"]:
        _invalidate_trace_clear(srv, rd, sp)
    completed = {
        **receipt,
        "status": "succeeded",
        "updated_at": time.time(),
    }
    # Publish success only after the trace postcondition was durably confirmed and every
    # process-local/disk projection was invalidated.
    _save_trace_clear_receipt(receipt_path, completed)
    result = completed["result"]
    return {
        "ok": True,
        "status": "succeeded",
        "operation_id": completed["id"],
        "removed": result["removed"],
        "kept": result["kept"],
    }


def _supersede_trace_clear(
        receipt_path: Path, receipt: dict[str, Any], reason: str) -> None:
    _save_trace_clear_receipt(receipt_path, {
        **receipt,
        "status": "superseded",
        "superseded_reason": reason,
        "updated_at": time.time(),
    })
    raise HTTPException(409, {
        "code": "trace_clear_operation_superseded",
        "operation_id": receipt["id"],
        "message": (
            "The trace lifecycle changed after an interrupted clear. Its old operation has "
            "been closed without another deletion."),
        "remediation": "Reload the current trace and form a new confirmation if needed.",
    })


def _apply_prepared_trace_clear(
        srv, rd: Path, sp: Path, receipt_path: Path, receipt: dict[str, Any],
        *, current: Optional[_TraceDigestSnapshot] = None,
        prepared: Optional[_PreparedTrace] = None) -> dict[str, Any]:
    """Recover or apply one durable write-ahead trace-clear receipt."""
    current_snapshot = current or _trace_digest_snapshot(sp)
    source_matches = (
        current_snapshot.exists == receipt["source_exists"]
        and current_snapshot.digest == receipt["source_digest"]
    )
    result_matches = (
        current_snapshot.exists == receipt["result_exists"]
        and current_snapshot.digest == receipt["result_digest"]
    )
    if result_matches and not source_matches:
        return _complete_trace_clear(srv, rd, sp, receipt_path, receipt)
    if not source_matches:
        _supersede_trace_clear(receipt_path, receipt, "trace_changed_after_pending")

    staged = prepared or _prepare_filtered_trace_snapshot(sp, {receipt["node_id"]})
    try:
        prepared_matches = (
            staged.source.exists == receipt["source_exists"]
            and staged.source.digest == receipt["source_digest"]
            and staged.result_exists == receipt["result_exists"]
            and staged.result_digest == receipt["result_digest"]
            and staged.result == receipt["result"]
        )
        if not prepared_matches:
            _supersede_trace_clear(receipt_path, receipt, "prepared_postcondition_changed")
        if result_matches:
            return _complete_trace_clear(srv, rd, sp, receipt_path, receipt)

        # The pending receipt is now durable, but a non-cooperating filesystem writer could still
        # change the pathname after preparation. Reconfirm the digest-CAS immediately before replace;
        # never overwrite a third state with the prepared result.
        before_replace = _trace_digest_snapshot(sp)
        if (before_replace.exists != receipt["source_exists"]
                or before_replace.digest != receipt["source_digest"]):
            _supersede_trace_clear(
                receipt_path, receipt, "trace_changed_after_pending")
        try:
            # A durable success receipt must never outrun the destructive replacement. The payload
            # already lives in a sibling 0600 temporary; sync it, atomically publish it, then sync the
            # directory. Failure remains indeterminate exactly like strict_atomic_write_bytes.
            _strict_replace_prepared_trace(staged, sp)
        except Exception as exc:  # noqa: BLE001 - normalize every durability/storage failure
            raise HTTPException(503, {
                "code": "trace_clear_outcome_unknown",
                "operation_id": receipt["id"],
                "message": "The trace replacement did not return a durable completion receipt.",
                "remediation": "Verify this same operation again; do not submit a new clear.",
            }) from exc

        try:
            post = _trace_digest_snapshot(sp)
        except HTTPException as exc:
            # Every read-back failure is ambiguous after atomic replacement, including a path that
            # became a link/hard-link.  A definitive 409 here would falsely imply no deletion and
            # could invite a second operation over a replacement that actually landed.
            raise HTTPException(503, {
                "code": "trace_clear_outcome_unknown",
                "operation_id": receipt["id"],
                "message": "The trace replacement could not be read back for confirmation.",
                "remediation": "Verify this same operation again; do not submit a new clear.",
            }) from exc
        if (post.exists != receipt["result_exists"]
                or post.digest != receipt["result_digest"]):
            raise HTTPException(503, {
                "code": "trace_clear_outcome_unknown",
                "operation_id": receipt["id"],
                "message": "The trace replacement postcondition could not be confirmed.",
                "remediation": "Verify this same operation again; do not submit a new clear.",
            })
        return _complete_trace_clear(srv, rd, sp, receipt_path, receipt)
    finally:
        # The caller that supplied a staged object also transfers its one-shot ownership here. Once
        # apply returns/raises, no recovery path needs that process-local temporary; WAL has digests.
        staged.cleanup()


def durable_clear_node_trace(
        srv, run_id: str, nid: int, body: Optional[dict[str, Any]], *,
        known_engine_liveness: Callable[[Path, str], bool]) -> dict[str, Any]:
    """Erase one node's spans from ``spans.jsonl`` under a durable write-ahead receipt.

    The route docstring in ``routers/control.py`` states the user-facing contract; this is the
    implementation. ``known_engine_liveness`` is the router's fail-closed liveness verdict — it
    stays there because its other three call sites must share one definition and one patch seam.
    """
    payload = body or {}
    expected_generation = payload.get(EXPECTED_RUN_GENERATION_FIELD)
    expected_trace_revision = payload.get("expected_trace_revision")
    node_generation = payload.get("node_generation")
    operation_id = payload.get("operation_id")
    if expected_generation is not None and (
            not isinstance(expected_generation, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_generation) is None):
        raise HTTPException(400, "expected_generation must be a lowercase SHA-256 token")
    if node_generation is not None and (
            type(node_generation) is not int or node_generation < 0):
        raise HTTPException(400, "node_generation must be a non-negative integer")
    if expected_trace_revision is not None and (
            not isinstance(expected_trace_revision, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_trace_revision) is None):
        raise HTTPException(400, "expected_trace_revision must be a lowercase SHA-256 token")
    if operation_id is not None and (
            not isinstance(operation_id, str)
            or _TRACE_CLEAR_OPERATION_RE.fullmatch(operation_id) is None):
        raise HTTPException(
            400, "operation_id must be tc_ followed by 32 lowercase hex digits")
    if (expected_generation is None or expected_trace_revision is None
            or node_generation is None or operation_id is None):
        raise HTTPException(428, {
            "code": "trace_clear_identity_required",
            "message": "Trace clear requires an operation id and the exact run and node lifecycle.",
            "remediation": "Reload the node and submit all identities from the rendered trace.",
        })

    rd = srv.run_dir(run_id)
    receipt_path = _trace_clear_receipt_path(srv, rd, operation_id)
    receipt = _load_trace_clear_receipt(receipt_path)
    completed = _trace_clear_receipt_result(
        receipt,
        receipt_path=receipt_path,
        operation_id=operation_id,
        expected_generation=expected_generation,
        expected_trace_revision=expected_trace_revision,
        nid=nid,
        node_generation=node_generation,
    )
    if completed is not None:
        return completed
    # Take command -> lifecycle -> engine-writer locks in that order because this is a destructive
    # whole-file rewrite. The command sequencer alone is not enough: the resume reconciler spawns
    # engines under the lifecycle lock, while direct CLI writers own only engine.lock.
    with _trace_clear_guard(srv, rd, operation_id, receipt is not None) as rd:
        # A retry may have waited behind the original handler in the command sequencer. Re-read
        # inside ownership: a completed operation returns idempotently; a pending same-id
        # operation becomes recoverable after an earlier process crash.
        receipt = _load_trace_clear_receipt(receipt_path)
        completed = _trace_clear_receipt_result(
            receipt,
            receipt_path=receipt_path,
            operation_id=operation_id,
            expected_generation=expected_generation,
            expected_trace_revision=expected_trace_revision,
            nid=nid,
            node_generation=node_generation,
        )
        if completed is not None:
            return completed
        recovering = receipt is not None  # succeeded/superseded already returned or raised
        if recovering:
            # Re-confirm the write-ahead record before any recovery decision. This closes the
            # strict-write ambiguity where `pending` became visible but its parent fsync failed:
            # no trace mutation or terminal response may rely on that unconfirmed directory entry.
            _save_trace_clear_receipt(receipt_path, receipt)
        if not recovering:
            pending = _pending_trace_clear_for_lifecycle(
                srv, rd,
                receipt_path=receipt_path,
                expected_generation=expected_generation,
                expected_trace_revision=expected_trace_revision,
                nid=nid,
                node_generation=node_generation,
            )
            if pending is not None:
                raise _trace_clear_pending(
                    pending["id"],
                    "Another request is already resolving this exact trace clear.")

        current_generation = srv.commands.run_generation(rd)
        if expected_generation != current_generation:
            if recovering:
                _supersede_trace_clear(
                    receipt_path, receipt, "run_generation_changed_after_pending")
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": current_generation or None,
                "message": "The run was reset or replaced before the trace clear was submitted.",
                "remediation": "Reload the run before clearing trace diagnostics.",
            })

        # A new operation can fail definitively before mutation. A pending operation cannot:
        # while any writer may be alive its historical outcome remains unknown, so retain its
        # operation identity and ask the client to verify again after ownership is stopped.
        if recovering:
            liveness = _engine_liveness(rd)
            if (liveness is not False or _engine_alive(rd)
                    or _fresh_resume_launch_pending(rd)):
                raise _trace_clear_pending(
                    operation_id,
                    "The original clear cannot be reconciled while trace write ownership is busy.")
        else:
            known_alive = known_engine_liveness(rd, "clear the node trace")
            if known_alive or _engine_alive(rd) or _fresh_resume_launch_pending(rd):
                raise HTTPException(
                    409, "run is live — stop it first (the engine is writing spans.jsonl)")

        # Own the same writer lock as every direct CLI for the entire rewrite. Track whether the
        # context entered so a recovery-time acquisition failure remains 425/ambiguous, while an
        # application error raised inside ownership keeps its precise status.
        writer_lock_entered = False
        try:
            with (engine_write_lock_http(rd),
                  span_destructive_write_guard(rd / "spans.jsonl", required=True)):
                writer_lock_entered = True
                current_generation = srv.commands.run_generation(rd)
                if expected_generation != current_generation:
                    if recovering:
                        _supersede_trace_clear(
                            receipt_path, receipt,
                            "run_generation_changed_after_writer_lock")
                    raise HTTPException(409, {
                        "code": "run_generation_changed",
                        "expected_generation": expected_generation,
                        "current_generation": current_generation or None,
                        "message": "The run changed before exclusive trace access was acquired.",
                        "remediation": "Reload the run before clearing trace diagnostics.",
                    })
                current_state = srv.state(rd)
                if current_state.resume_pending():
                    if recovering:
                        raise _trace_clear_pending(
                            operation_id,
                            "The original clear cannot be reconciled while a resume is pending.")
                    raise HTTPException(409, "run has an unserved resume — stop it first "
                                             "(an engine is about to write spans.jsonl)")
                current_node = current_state.nodes.get(nid)
                current_node_generation = getattr(current_node, "attempt", None)
                node_changed = (
                    current_node is None or current_node.tombstoned
                    or nid in current_state.aborted_nodes
                    or type(current_node_generation) is not int
                    or current_node_generation < 0
                    or node_generation != current_node_generation
                )
                if node_changed:
                    if recovering:
                        _supersede_trace_clear(
                            receipt_path, receipt, "node_generation_changed_after_pending")
                    raise HTTPException(409, {
                        "code": "node_generation_changed",
                        "node_id": nid,
                        "expected_node_generation": node_generation,
                        "current_node_generation": (
                            current_node_generation
                            if type(current_node_generation) is int
                            and current_node_generation >= 0 else None),
                        "message": (
                            "The node no longer has the confirmed trace lifecycle."
                            if current_node is None
                            else "The node was rebuilt before the trace clear was submitted."),
                        "remediation": "Reload the node before clearing trace diagnostics.",
                    })

                sp = rd / "spans.jsonl"
                if recovering:
                    try:
                        return _apply_prepared_trace_clear(
                            srv, rd, sp, receipt_path, receipt)
                    except HTTPException as exc:
                        code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
                        if code == "trace_path_invalid":
                            _supersede_trace_clear(
                                receipt_path, receipt, "trace_path_changed_after_pending")
                        raise

                current_trace_revision = trace_file_revision(sp)
                if current_trace_revision is None:
                    raise HTTPException(503, {
                        "code": "trace_revision_unavailable",
                        "message": "The current trace file identity could not be verified.",
                        "remediation": "Inspect spans.jsonl storage before clearing diagnostics.",
                    })
                if expected_trace_revision != current_trace_revision:
                    raise HTTPException(409, {
                        "code": "trace_revision_changed",
                        "expected_trace_revision": expected_trace_revision,
                        "current_trace_revision": current_trace_revision,
                        "message": "Trace diagnostics changed after the confirmation was rendered.",
                        "remediation": "Refresh the node and confirm against the current trace.",
                    })
                prepared = _prepare_filtered_trace_snapshot(sp, {nid})
                try:
                    verified_trace_revision = trace_file_revision(sp)
                    if (verified_trace_revision is None
                            or verified_trace_revision != current_trace_revision):
                        raise HTTPException(409, {
                            "code": "trace_revision_changed",
                            "expected_trace_revision": expected_trace_revision,
                            "current_trace_revision": verified_trace_revision,
                            "message": "Trace diagnostics changed while the clear snapshot was read.",
                            "remediation": "Refresh the node and confirm against the current trace.",
                        })
                    receipt = {
                        "version": 2,
                        "id": operation_id,
                        "status": "pending",
                        "expected_generation": expected_generation,
                        "expected_trace_revision": expected_trace_revision,
                        "node_id": nid,
                        "node_generation": node_generation,
                        "source_exists": prepared.source.exists,
                        "source_digest": prepared.source.digest,
                        "result_exists": prepared.result_exists,
                        "result_digest": prepared.result_digest,
                        "result": prepared.result,
                        "created_at": time.time(),
                    }
                    # Write-ahead hashes distinguish "not applied" from "already applied" after a
                    # crash. The same operation may recover; a changed third state is closed without
                    # another deletion. The staged result contains no authority of its own and may be
                    # discarded after any failure; recovery reconstructs it from source + receipt.
                    _save_trace_clear_receipt(receipt_path, receipt)
                    return _apply_prepared_trace_clear(
                        srv, rd, sp, receipt_path, receipt,
                        current=prepared.source, prepared=prepared)
                finally:
                    prepared.cleanup()
        except EventStoreLockError as exc:
            if recovering:
                raise _trace_clear_pending(
                    operation_id,
                    "The original clear is waiting for exclusive trace projection ownership.")
            raise HTTPException(503, {
                "code": "trace_clear_projection_lock_unavailable",
                "operation_id": operation_id,
                "message": "Trace clear could not obtain exclusive span-index ownership.",
                "remediation": "Retry this same operation after trace storage is available.",
            }) from exc
        except HTTPException:
            if recovering and not writer_lock_entered:
                raise _trace_clear_pending(
                    operation_id,
                    "The original clear is waiting for exclusive trace write ownership.")
            raise
