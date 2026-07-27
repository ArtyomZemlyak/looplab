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
        self._cache: dict[str, tuple] = {}     # run_id -> (sig, RunState, divergence|None)

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
            # Nanosecond mtime + file identity (inode/device), NOT `int(st_mtime)`: truncating mtime to
            # whole seconds made this cache blind to a same-size replacement/rewrite inside one second.
            # That is not only a freshness issue — these folded states feed default-on SiblingRunTools/
            # AllRunsTools, so a stale hit lets an agent keep reasoning from an obsolete cross-run evidence
            # prefix. `st_mtime_ns` catches any in-place rewrite; `st_ino`/`st_dev` catch a same-size,
            # mtime-restored REPLACEMENT (rm+recreate, or a sync tool that preserves mtime).
            return (s.st_size, s.st_mtime_ns, s.st_ino, s.st_dev)
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
        from looplab.events.eventstore import iter_event_jsonl, log_divergence
        from looplab.core.models import Event
        from looplab.events.replay import fold
        try:
            st = fold(Event(**o) for o in iter_event_jsonl(rd / "events.jsonl"))
        except (OSError, ValueError, TypeError):
            return None
        # `iter_event_jsonl` deliberately STOPS at the first corrupt/non-dense record, and the
        # surviving prefix folds into a RunState that looks exactly like a complete one. Reading it
        # without asking whether it was complete is what let these tools tell an in-loop agent that
        # later experiments and evidence "do not exist" when the truth is that the log could not be
        # read that far. Record the divergence alongside the fold so consumers can abstain; the scan
        # only runs on a cache MISS, where the fold is already O(log size).
        try:
            divergence = log_divergence(rd / "events.jsonl")
        except (OSError, ValueError, TypeError):
            divergence = {"unreadable": True}
        self._cache[str(run_id)] = (sig, st, divergence)
        return st

    def partial(self, run_id: Optional[str]) -> Optional[dict]:
        """The divergence receipt for a run whose log could not be read to the end, else None.

        `state()` must be called first (it is, at every consumer: this answers "was what I just read
        the whole run?"). None also covers a run that was never read, which is honest — nothing was
        claimed about it either."""
        hit = self._cache.get(str(run_id))
        return hit[2] if hit and len(hit) > 2 and hit[2] else None

    def source_note(self, run_id: Optional[str]) -> str:
        """A bounded line for tool output when the source log is incomplete, else "".

        Phrased so the reading agent does not convert a truncated read into an absence claim — the
        specific failure this guards is "no later experiment beat this one" derived from a prefix."""
        d = self.partial(run_id)
        if not d:
            return ""
        good = d.get("good_records")
        where = f" after {good} record(s)" if isinstance(good, int) else ""
        return (f"[PARTIAL SOURCE] run {run_id}'s event log could not be read to the end{where}; "
                "anything later is UNKNOWN, not absent — do not conclude a result is best or missing "
                "from this run.")

    def run_ids(self) -> list[str]:
        """Every run id under run_root (a directory carrying an events.jsonl), sorted."""
        try:
            return sorted(p.name for p in self.run_root.iterdir()
                          if p.is_dir() and (p / "events.jsonl").exists())
        except OSError:
            return []
