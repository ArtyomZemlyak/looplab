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
from pathlib import Path


def best_effort_fsync(fileno: int) -> None:
    """fsync that DEGRADES where it isn't supported. On an object-store FUSE mount (geesefs/s3fs/
    goofys — common on JupyterHub data volumes) fsync can raise OSError (EINVAL/ENOTSUP/EIO) or block
    on an S3 round-trip; a raised fsync must NOT kill the engine mid-append (durability already
    degrades gracefully on read — `iter_jsonl` tolerates a torn final line). Flush already reached the
    OS buffer; this just asks for a sync and swallows an unsupported-FS error."""
    try:
        os.fsync(fileno)
    except OSError:
        pass   # fsync unsupported/failed on this filesystem — the bytes still reached the OS buffer


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
