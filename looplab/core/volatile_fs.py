"""Is a path in MEMORY? tmpfs/ramfs detection for the run root (doc 82 П6).

WHY. A run root on tmpfs (`/dev/shm/looplab-runs` on the MiniOneRec box) is fast and keeps the
engine's appends off a slow network mount, and a pod restart wipes it — four times on 2026-10-07/08.
`looplab run-mirror` copies it to durable storage, but nothing told an operator who had not started
one that the runs lived in memory. `looplab ui` / `looplab run` now say so, and the UI shows a banner.

HOW. `/proc/mounts` (Linux): the mount whose mount point is the LONGEST prefix of the resolved
path decides, and its type is `tmpfs` or `ramfs`. Anything else — no `/proc/mounts`, an unreadable
file, a non-Linux host — answers None ("unknown"), never a guess. `os.statfs` is not used: Python's
`os` has no `statfs`, and `statvfs` carries no filesystem type.

Imports nothing above the standard library.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

VOLATILE_TYPES = frozenset({"tmpfs", "ramfs"})


def _unescape(field: str) -> str:
    # /proc/mounts octal-escapes space, tab, newline and backslash in mount points.
    return (field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n")
            .replace("\\134", "\\"))


def mount_type(path, *, mounts_file: str = "/proc/mounts") -> Optional[str]:
    """The filesystem type of the mount holding `path`, or None when it cannot be told."""
    try:
        target = os.path.realpath(os.fspath(path))
        with open(mounts_file, encoding="utf-8", errors="replace") as fh:
            rows = [line.split() for line in fh]
    except (OSError, TypeError, ValueError):
        return None
    best, kind = -1, None
    for row in rows:
        if len(row) < 3:
            continue
        point = _unescape(row[1])
        inside = target == point or target.startswith(point.rstrip("/") + "/") or point == "/"
        if inside and len(point) > best:
            best, kind = len(point), row[2]
    return kind


def is_volatile(path, *, mounts_file: str = "/proc/mounts") -> Optional[bool]:
    """True on tmpfs/ramfs, False on any other known filesystem, None when unknown."""
    kind = mount_type(path, mounts_file=mounts_file)
    return None if kind is None else kind in VOLATILE_TYPES


MIRROR_DST_ENV = "LOOPLAB_RUN_MIRROR_DST"


def volatile_warning(run_root) -> Optional[str]:
    """The one sentence `looplab ui` / `looplab run` print for an unprotected in-memory run root,
    or None (not volatile, unknown, or a mirror destination is configured)."""
    if os.environ.get(MIRROR_DST_ENV) or is_volatile(run_root) is not True:
        return None
    return (f"warning: the run root {os.fspath(run_root)} is in memory (tmpfs): a restart wipes "
            f"its runs. Set {MIRROR_DST_ENV} to a durable folder and `looplab ui` mirrors it, "
            "or run `looplab run-mirror --loop --dst <folder>` yourself.")
