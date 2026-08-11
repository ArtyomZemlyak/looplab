"""Descriptor-first access to LoopLab's private trace sidecars.

Trace rows can contain prompts, model output, tool arguments and host paths.  A run-local
``spans.jsonl`` (and its derived append journal) must therefore be a private regular file, never a
filesystem capability supplied through a symlink, hard link, FIFO/device, or a pathname swap.

This module contains the filesystem boundary plus the one physical-row allocation boundary shared
by the exporter, readers and destructive filters. JSON decoding/prefix policy remains with callers.
"""
from __future__ import annotations

import builtins
import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from looplab.core.atomicio import same_file_entry


TraceFileIdentity = tuple[int, int]

# Exact physical-envelope contract for spans.jsonl and its derived index. The terminating LF is part
# of the limit; a torn suffix may therefore contain at most ``MAX - 1`` bytes. Current writers bound
# individual diagnostics around 64 KiB, so 8 MiB leaves substantial historical/custom-exporter
# headroom while keeping a corrupt legacy row from becoming a multi-GB Python allocation.
TRACE_JSONL_ROW_MAX_BYTES = 8 * 1024 * 1024
# Shared only by the canonical exporter and destructive trace reset/clear/archive.  Normal span-index
# refresh keeps its independent ``.spans-index.lock``: a cold multi-GB diagnostic read must never
# backpressure the hot exporter queue.  A late async writer still serializes with every source rewrite
# after the engine lifecycle lock changes hands.
TRACE_WRITER_LOCK_NAME = ".spans-writer.lock"
_TRACE_JSONL_READ_CHUNK_BYTES = 64 * 1024


def iter_bounded_trace_jsonl_lines(
        stream, *, size: Optional[int] = None, label: str = "trace source",
        max_line_bytes: Optional[int] = None,
        read_chunk_bytes: Optional[int] = None):
    """Yield complete physical trace lines without accumulating an unbounded row.

    ``bytes`` file iteration and ``readline()`` both materialize through the next newline. This
    scanner instead searches fixed-size chunks and retains at most one physical row under the shared
    limit. A torn EOF or over-limit row stops before that row; the reader decides whether complete
    yielded invalid JSON is a prefix stop or quarantine. When ``size`` is supplied, only that
    descriptor snapshot is inspected and an unexpected early EOF raises. Intentionally stopping at
    an oversized row does not drain its suffix merely to prove an already-known file size.
    """
    ceiling = TRACE_JSONL_ROW_MAX_BYTES if max_line_bytes is None else int(max_line_bytes)
    chunk_size = (_TRACE_JSONL_READ_CHUNK_BYTES
                  if read_chunk_bytes is None else int(read_chunk_bytes))
    if ceiling < 2 or chunk_size < 1:
        raise ValueError("trace JSONL line and read bounds must be positive")
    expected = None if size is None else max(0, int(size))
    remaining = expected
    observed = 0
    pending = bytearray()

    while remaining is None or remaining > 0:
        request = chunk_size if remaining is None else min(chunk_size, remaining)
        chunk = stream.read(request)
        if not chunk:
            if remaining is not None and remaining > 0:
                raise OSError(
                    f"short read of {label}: expected {expected} bytes, got {observed}")
            return
        if len(chunk) > request:
            # Real binary files obey read(n); fail closed for a malformed/custom stream instead of
            # letting it bypass the allocation boundary used by remote filesystem wrappers too.
            raise OSError(f"oversized read from {label}: requested {request}, got {len(chunk)}")
        observed += len(chunk)
        if remaining is not None:
            remaining -= len(chunk)

        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            if newline < 0:
                suffix = chunk[cursor:]
                # Without LF, ``ceiling`` accumulated bytes already imply a future complete row of
                # at least ceiling+1. Refuse before extending pending beyond its ceiling-1 contract.
                if len(pending) + len(suffix) >= ceiling:
                    return
                pending.extend(suffix)
                break

            segment_length = newline - cursor
            if len(pending) + segment_length + 1 > ceiling:
                return
            if pending:
                pending.extend(chunk[cursor:newline + 1])
                raw = bytes(pending)
                pending.clear()
            else:
                raw = chunk[cursor:newline + 1]
            yield raw
            cursor = newline + 1


def trace_file_identity(info: os.stat_result) -> TraceFileIdentity:
    """Return the canonical replacement-only identity shared by ``lstat`` and ``fstat``."""
    return same_file_entry(info)


def windows_file_change_time(fd: int) -> Optional[int]:
    """Return ``FILE_BASIC_INFO.ChangeTime`` for one already-open CRT descriptor.

    CPython exposes Windows creation time as ``st_ctime_ns``.  It therefore cannot prove that a
    same-file rewrite happened when size and last-write time were restored.  ``ChangeTime`` is the
    filesystem-maintained mutation stamp needed by trace cache/CAS callers; unsupported filesystems
    and API failures return ``None`` so those callers can fail closed rather than guess.
    """
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
    except (AttributeError, ImportError):
        return None

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    try:
        raw_handle = msvcrt.get_osfhandle(fd)
        if raw_handle == -1:
            return None
        get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
        get_info.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        get_info.restype = wintypes.BOOL
        info = _FileBasicInfo()
        if not get_info(
                wintypes.HANDLE(raw_handle), 0, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        token = int(info.ChangeTime)
        return token if token > 0 else None
    except (AttributeError, OSError, OverflowError, TypeError, ValueError, ctypes.ArgumentError):
        return None


def trace_file_change_token(
        fd: int, info: Optional[os.stat_result] = None) -> Optional[int]:
    """Return a descriptor-bound trace mutation token, or ``None`` when it is unavailable.

    POSIX ``ctime`` is a change timestamp.  Windows ``ctime`` is creation time, so the native
    descriptor query above is the only safe authority for same-file rewrites there.  Callers that
    use this token for cache reuse or destructive compare-and-swap must treat ``None`` as untrusted.
    """
    if os.name == "nt":
        return windows_file_change_time(fd)
    status = os.fstat(fd) if info is None else info
    token = int(status.st_ctime_ns)
    return token if token > 0 else None


def _is_reparse_point(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return bool(marker and (int(getattr(info, "st_file_attributes", 0) or 0) & marker))


def assert_private_trace_file(info: os.stat_result, path: Path) -> None:
    """Require one unaliased regular trace file (works for lstat and fstat results)."""
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise OSError(errno.ELOOP, "trace path must not be a link or reparse point", path)
    if stat.S_ISDIR(info.st_mode):
        # Preserve the ordinary binary-reader contract used by callers/tests while still rejecting
        # before open. Other special types deliberately use EINVAL below.
        raise IsADirectoryError(errno.EISDIR, "trace path must not be a directory", path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "trace path must be a regular file", path)
    if int(info.st_nlink) != 1:
        raise OSError(errno.EPERM, "trace path must not have hard-link aliases", path)


def _changed(path: Path, message: str) -> OSError:
    return OSError(getattr(errno, "ESTALE", errno.EIO), message, path)


@contextmanager
def open_private_trace_file(
        path: str | os.PathLike,
        mode: str = "rb",
        *,
        expected_identity: Optional[TraceFileIdentity] = None,
        open_file: Optional[Callable] = None,
) -> Iterator:
    """Open and keep one private trace-file descriptor bound to its final pathname component.

    Supported modes are binary read (``rb``) and binary append/update (``a+b``).  The latter may
    create a missing file and is intended for the exporter.  ``O_NOFOLLOW`` closes the native
    regular-to-symlink race; lstat/fstat identity checks are the portable backstop.  ``O_NONBLOCK``
    makes a regular-to-FIFO race reject promptly instead of consuming a server worker forever.

    The final lstat/fstat check runs after a successful body so a caller never publishes bytes from
    an inode whose run-root name was replaced during the read.  Ordinary in-place append is allowed:
    only entry identity, type, and link ownership are rechecked, not mutable size/timestamps.

    ``open_file`` preserves callers' established test/ACL interception seam.  Production callers
    normally omit it; modules that historically resolved a monkeypatchable module-level ``open`` may
    pass that callable explicitly.
    """
    if mode not in {"rb", "a+b"}:
        raise ValueError("trace files support only 'rb' and 'a+b' modes")
    target = Path(path)
    creates = mode == "a+b"
    before = None
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        if not creates:
            raise
    if before is not None:
        assert_private_trace_file(before, target)

    def descriptor_opener(raw_path, flags):
        return os.open(
            raw_path,
            flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
            0o600,
        )

    file_open = builtins.open if open_file is None else open_file
    try:
        stream = file_open(target, mode, opener=descriptor_opener)
    except FileNotFoundError as exc:
        # Initial absence is meaningful to readers.  Once lstat observed an entry, ENOENT is a
        # pathname race/storage failure and must not be laundered into an exact empty trace.
        if before is None:
            raise
        raise _changed(target, "trace path disappeared while it was opened") from exc

    try:
        with stream:
            opened = os.fstat(stream.fileno())
            opened_identity = trace_file_identity(opened)
            if expected_identity is not None and opened_identity != expected_identity:
                raise _changed(target, "trace path no longer matches the expected source")
            try:
                after_open = os.lstat(target)
            except FileNotFoundError as exc:
                raise _changed(target, "trace path disappeared while it was opened") from exc
            if trace_file_identity(after_open) != opened_identity:
                raise _changed(target, "trace path changed while it was opened")
            if before is not None and trace_file_identity(before) != opened_identity:
                raise _changed(target, "trace path changed while it was opened")
            assert_private_trace_file(opened, target)
            assert_private_trace_file(after_open, target)

            try:
                yield stream
            except BaseException:
                raise
            else:
                # Validate the descriptor again as well as the name.  This catches a hard-link alias
                # added while the caller was reading even when the original run-root name stayed put.
                final_descriptor = os.fstat(stream.fileno())
                try:
                    final_entry = os.lstat(target)
                except FileNotFoundError as exc:
                    raise _changed(target, "trace path disappeared while it was read") from exc
                if (trace_file_identity(final_descriptor) != opened_identity
                        or trace_file_identity(final_entry) != opened_identity):
                    raise _changed(target, "trace path changed while it was read")
                assert_private_trace_file(final_descriptor, target)
                assert_private_trace_file(final_entry, target)
    except BaseException:
        # ``with stream`` normally closes it.  If validating ``fileno``/``__enter__`` failed before
        # that context took ownership, make the close best-effort and preserve the original error.
        if not getattr(stream, "closed", True):
            try:
                stream.close()
            except OSError:
                pass
        raise


__all__ = [
    "TRACE_JSONL_ROW_MAX_BYTES", "TRACE_WRITER_LOCK_NAME", "TraceFileIdentity",
    "assert_private_trace_file", "iter_bounded_trace_jsonl_lines",
    "open_private_trace_file", "trace_file_change_token", "trace_file_identity",
    "windows_file_change_time",
]
