"""Shared fold-on-demand run-state cache (BACKLOG §4 "RunStateCache").

`SiblingRunTools` (run_tools.py) and `MachineRunsTools` (machine_runs_tools.py) both read OTHER runs' event logs
off disk: resolve <run_root>/<run_id> with a path-traversal guard, fold the log into a `RunState`,
and cache the fold by the log's (size, mtime) fingerprint so repeated turns don't re-fold unchanged
runs. That plumbing was duplicated verbatim in both providers; it lives here once and they delegate.
Every reader soft-fails (returns None / []) — a junk run_id or a torn log must never crash the loop.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.core.models import RunState


class RunStateCache:
    """Fold-on-demand `RunState`s for the run directories under one run-root."""

    def __init__(self, run_root):
        self.run_root = Path(run_root)
        self._cache: dict[str, tuple] = {}     # run_id -> (sig, RunState)

    def safe_dir(self, run_id: Optional[str]) -> Optional[Path]:
        """Resolve <run_root>/<run_id>, with the same path-traversal guard as server._run_dir: the
        directory must sit directly under run_root and carry an events.jsonl. Returns None otherwise."""
        if not run_id:
            return None
        rd = (self.run_root / str(run_id)).resolve()
        root = self.run_root.resolve()
        if rd.parent != root:
            return None
        if not (rd / "events.jsonl").exists():
            return None
        return rd

    @staticmethod
    def sig(rd: Path):
        try:
            s = (rd / "events.jsonl").stat()
            return (s.st_size, int(s.st_mtime))
        except OSError:
            return (0, 0)

    def state(self, run_id: Optional[str]) -> Optional[RunState]:
        rd = self.safe_dir(run_id)
        if rd is None:
            return None
        sig = self.sig(rd)
        hit = self._cache.get(str(run_id))
        if hit and hit[0] == sig:
            return hit[1]
        from looplab.events.eventstore import iter_event_jsonl
        from looplab.core.models import Event
        from looplab.events.replay import fold
        try:
            st = fold(Event(**o) for o in iter_event_jsonl(rd / "events.jsonl"))
        except (OSError, ValueError, TypeError):
            return None
        self._cache[str(run_id)] = (sig, st)
        return st

    def run_ids(self) -> list[str]:
        """Every run id under run_root (a directory carrying an events.jsonl), sorted."""
        try:
            return sorted(p.name for p in self.run_root.iterdir()
                          if p.is_dir() and (p / "events.jsonl").exists())
        except OSError:
            return []
