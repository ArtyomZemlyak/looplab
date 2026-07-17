"""Atomic write helper (I1, ADR-17): unique temp file -> best-effort fsync -> os.replace.

Used for derived/snapshot files (config snapshot, HTML, projects/ui_settings; SQLite is its own
thing). The event log itself uses append+fsync (see eventstore) — torn final lines are tolerated on
read, which is the durability contract we actually test.

FUSE/S3-aware (JupyterHub geesefs/s3fs/goofys home mounts): fsync may raise/throttle and a fixed
`<name>.tmp` lets two concurrent writers (the engine subprocess + the UI server on the same run dir)
truncate each other — so we fsync best-effort and give every write its OWN temp via mkstemp.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

# fsync timeout (seconds) before we give up on it for the rest of the process. Env-overridable.
# Parsed defensively: this module is imported transitively everywhere, so a garbage override
# (LOOPLAB_FSYNC_TIMEOUT=abc) must degrade to the default, not crash `import looplab` at load.
def _fsync_timeout() -> float:
    try:
        return float(os.environ.get("LOOPLAB_FSYNC_TIMEOUT", "5") or 5)
    except (TypeError, ValueError):
        return 5.0


_FSYNC_TIMEOUT = _fsync_timeout()
_FSYNC_DISABLED = False   # flips permanently once fsync is seen to BLOCK — a stalled FUSE mount


def strict_fsync(fileno: int) -> None:
    """Confirm a sync or fail within the configured deadline.

    Paid-work claims cannot use the normal best-effort durability contract: starting a provider
    call after an unconfirmed claim could make a retry bill twice. Duplicate the descriptor so a
    timed-out sync thread can finish safely after the caller closes its own file handle.
    """
    duplicate = os.dup(fileno)
    done = threading.Event()
    failure: list[BaseException] = []

    def _sync() -> None:
        try:
            os.fsync(duplicate)
        except BaseException as exc:  # noqa: BLE001 - propagate the exact durability failure
            failure.append(exc)
        finally:
            try:
                os.close(duplicate)
            finally:
                done.set()

    worker = threading.Thread(target=_sync, daemon=True)
    try:
        worker.start()
    except BaseException:
        os.close(duplicate)
        raise
    if not done.wait(_FSYNC_TIMEOUT):
        raise TimeoutError("durable fsync timed out")
    if failure:
        raise OSError("durable fsync failed") from failure[0]


def strict_fsync_parent(path: str | os.PathLike) -> None:
    """Durably publish a newly-created file/directory entry on POSIX.

    ``fsync(file)`` confirms file contents but not necessarily the parent directory entry that makes
    a first-created paid-work claim discoverable after power loss. Windows publication is performed
    by the write-through move itself (see :func:`_windows_move_write_through`), so there is no second
    portable directory-handle operation to perform there.
    """
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(Path(path).parent, flags)
    try:
        strict_fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_move_write_through(
        source: str | os.PathLike, destination: str | os.PathLike, *, replace: bool) -> None:
    """Move one Windows path atomically and wait for the rename metadata to reach storage.

    ``os.replace`` is atomic on Windows but exposes no write-through flag. Paid-work claims need the
    stronger Win32 ``MOVEFILE_WRITE_THROUGH`` contract: returning before the destination name is
    durable can lose an acknowledged claim after power failure and authorize a duplicate paid call.
    """
    import ctypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    move_file_ex.restype = ctypes.c_int
    flags = 0x00000008  # MOVEFILE_WRITE_THROUGH
    if replace:
        flags |= 0x00000001  # MOVEFILE_REPLACE_EXISTING
    src = os.path.abspath(os.fspath(source))
    dst = os.path.abspath(os.fspath(destination))
    if not move_file_ex(src, dst, flags):
        code = ctypes.get_last_error()
        raise OSError(code, "durable Windows rename failed", dst)


def _strict_replace(source: str | os.PathLike, destination: str | os.PathLike) -> None:
    if os.name == "nt":
        _windows_move_write_through(source, destination, replace=True)
    else:
        os.replace(source, destination)


def _strict_publish_directory(directory: Path) -> None:
    """Create one missing directory with a durably published name."""
    if os.name != "nt":
        directory.mkdir(exist_ok=True)
        strict_fsync_parent(directory)
        return

    # Windows has no portable Python directory-fsync primitive. Create a unique sibling directory,
    # then publish the requested name with MOVEFILE_WRITE_THROUGH. Do not accept an unexpected
    # concurrent destination: its creator may have used a weaker durability policy.
    temporary = Path(tempfile.mkdtemp(
        dir=str(directory.parent), prefix=f".{directory.name}.", suffix=".tmp"))
    try:
        _windows_move_write_through(temporary, directory, replace=False)
    except BaseException:
        try:
            os.rmdir(temporary)
        except OSError:
            pass
        raise


def best_effort_fsync(fileno: int) -> None:
    """fsync that DEGRADES where it isn't supported OR where it BLOCKS. On an object-store FUSE mount
    (geesefs/s3fs/goofys — common on JupyterHub data volumes) fsync can raise OSError (EINVAL/ENOTSUP/
    EIO), OR — the trap — BLOCK indefinitely on a stalled S3 round-trip. A blocking fsync is
    uninterruptible in the kernel, so catching OSError can't save us: it would wedge the whole engine
    mid-append (observed: `events.jsonl` on a geesefs run dir hanging the append, and the test suite
    hanging in exactly this call). So we run fsync on a daemon thread and abandon it after
    `_FSYNC_TIMEOUT`; the bytes already reached the OS buffer via flush() and durability degrades
    gracefully on read (`iter_jsonl` tolerates a torn final line), so a stuck sync must never block the
    writer. The FIRST timeout latches fsync OFF process-wide — a stalled mount won't recover mid-run,
    and this stops us leaking one blocked thread per subsequent append."""
    global _FSYNC_DISABLED
    if _FSYNC_DISABLED:
        return
    done = threading.Event()

    def _sync() -> None:
        try:
            os.fsync(fileno)
        except OSError:
            pass   # unsupported/failed on this FS — bytes still reached the OS buffer
        finally:
            done.set()

    threading.Thread(target=_sync, daemon=True).start()
    if not done.wait(_FSYNC_TIMEOUT):
        _FSYNC_DISABLED = True   # fsync is BLOCKING (stalled FUSE/S3) — stop trying for this process


def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # UNIQUE temp name (not a fixed `<name>.tmp`): the engine subprocess and the UI server both write
    # into the same run dir, so a shared temp path lets one writer truncate another's in-flight temp —
    # a window that's tiny on local disk but wide on a slow-rename S3/FUSE mount. mkstemp in the
    # destination dir gives each writer its own temp (and 0600) and keeps the rename on one filesystem.
    fd, tmpname = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            best_effort_fsync(f.fileno())
        os.replace(tmpname, p)  # atomic on Win + POSIX local FS; best-effort on FUSE
    except BaseException:
        try:
            os.unlink(tmpname)   # don't leave a stray temp behind on failure
        except OSError:
            pass
        raise


def atomic_write_text(path: str | os.PathLike, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def _ensure_strict_parent(parent: Path) -> None:
    """Create and durably publish every missing directory in *parent*'s path."""
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        ancestor = cursor.parent
        if ancestor == cursor:
            break
        cursor = ancestor

    # Publish top-down: a child is only created after the directory containing its name has received
    # a strict durability receipt. On Windows an unexpected concurrent creator fails closed because
    # its weaker publication policy cannot be inferred after the fact.
    for directory in reversed(missing):
        _strict_publish_directory(directory)


def strict_atomic_write_bytes(path: str | os.PathLike, data: bytes) -> None:
    """Atomically replace *path* only after confirming durable publication.

    Unlike :func:`atomic_write_bytes`, this helper is deliberately fail-closed.  It is intended for
    paid-work claims and other records whose visibility must survive a crash before an external side
    effect starts.  Both the temporary file contents and, after ``os.replace``, the destination's
    parent directory entry must receive a successful strict sync receipt.  Any newly-created parent
    directories are also durably published before the temporary file is opened.
    """
    p = Path(path)
    _ensure_strict_parent(p.parent)
    fd, tmpname = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    raw_fd: int | None = fd
    try:
        stream = os.fdopen(fd, "wb")
        raw_fd = None  # stream owns the descriptor now; never close a possibly-reused fd number.
        with stream as f:
            f.write(data)
            f.flush()
            strict_fsync(f.fileno())
        _strict_replace(tmpname, p)
        strict_fsync_parent(p)
    except BaseException:
        # Once fdopen succeeds it owns the descriptor.  Only close the raw descriptor when fdopen
        # itself failed; closing the old integer later could race with another thread reusing it.
        if raw_fd is not None:
            try:
                os.close(raw_fd)
            except OSError:
                pass
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def strict_atomic_write_text(path: str | os.PathLike, text: str) -> None:
    """UTF-8 text variant of :func:`strict_atomic_write_bytes`."""
    strict_atomic_write_bytes(path, text.encode("utf-8"))


def append_jsonl_bytes_locked(path: str | os.PathLike, payload: bytes) -> None:
    """Durably append one or more already-encoded JSONL records.

    The caller must hold the file's interprocess lock for the complete operation.  Keeping
    locking outside this helper lets a read/modify/rewrite transaction (for example lesson-store
    hygiene) share the same critical section.  If a previous process left a torn final record, a
    separator is inserted before the new payload so the new records remain independently readable;
    lenient readers can then ignore only the torn record instead of losing every later append.
    """
    if not payload:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    normalized = payload.rstrip(b"\r\n") + b"\n"
    with open(p, "a+b") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        needs_separator = False
        if size:
            f.seek(-1, os.SEEK_END)
            needs_separator = f.read(1) not in {b"\n", b"\r"}
        f.seek(0, os.SEEK_END)
        if needs_separator:
            f.write(b"\n")
        f.write(normalized)
        f.flush()
        best_effort_fsync(f.fileno())
