"""Is a cross-process lock free RIGHT NOW? — asked through the product's own primitive.

Three tests asked it with a raw `fcntl.flock` on a second `open()`, which made each of them a
POSIX-only test of a rule that must hold everywhere: on the Windows CI leg (GitHub Actions run
35785582444, review 2026-09-22) `import fcntl` raised inside the probe, the product contained the
exception, and the assertion read an empty observation list. `events/eventstore.py::
interprocess_lock` is what every appender takes — `flock` on POSIX, a `msvcrt` byte lock on Windows
— so the probe contends through it, non-blocking, and releases at once.

Why a second open in THIS process is a faithful stand-in for another run: an flock belongs to the
open file description and a msvcrt byte lock to the handle, so on both platforms a lock held through
one open refuses a non-blocking attempt through another, whoever owns them.
"""
from __future__ import annotations

from pathlib import Path

from looplab.events.eventstore import InterprocessLockContended, interprocess_lock


def lock_is_free(lock_path) -> bool:
    try:
        with interprocess_lock(Path(lock_path), required=True, blocking=False):
            return True
    except InterprocessLockContended:
        return False
