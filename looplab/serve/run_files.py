"""Shared serialization for mutable per-run snapshot files."""
from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from looplab.core.run_reset import assert_run_reset_write_allowed
from looplab.events.eventstore import _interprocess_lock


_RUN_CONFIG_LOCK_STRIPES = tuple(threading.Lock() for _ in range(64))


def run_config_thread_lock(snapshot_path: Path) -> threading.Lock:
    """Bound same-process serialization without retaining a lock for every historical run."""
    identity = os.path.normcase(os.path.abspath(snapshot_path)).encode(
        "utf-8", errors="surrogatepass")
    stripe = int.from_bytes(hashlib.sha256(identity).digest()[:2], "big")
    return _RUN_CONFIG_LOCK_STRIPES[stripe % len(_RUN_CONFIG_LOCK_STRIPES)]


@contextmanager
def run_config_write_lock(
        snapshot_path: Path, *, operation_id: Optional[str] = None) -> Iterator[None]:
    """Own the complete config read/compare/write transaction and enforce a pending reset fence."""
    with (run_config_thread_lock(snapshot_path),
          _interprocess_lock(Path(str(snapshot_path) + ".lock"), required=True)):
        assert_run_reset_write_allowed(snapshot_path.parent, operation_id)
        yield


__all__ = ["run_config_thread_lock", "run_config_write_lock"]
