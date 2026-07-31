"""Shared fold-on-demand run-state cache (BACKLOG §4 "RunStateCache").

`SiblingRunTools` (run_tools.py) and `MachineRunsTools` (machine_runs_tools.py) both read OTHER runs' event logs
off disk: resolve <run_root>/<run_id> with a path-traversal guard, fold the log into a `RunState`,
and cache the fold by the log's (size, mtime) fingerprint so repeated turns don't re-fold unchanged
runs. That plumbing was duplicated verbatim in both providers; it lives here once and they delegate.
Every reader soft-fails (returns None / []) — a junk run_id or a torn log must never crash the loop.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from looplab.core.models import RunState


class RunStateCache:
    """Fold-on-demand `RunState`s for the run directories under one run-root."""

    def __init__(self, run_root):
        self.run_root = Path(run_root)
        # LRU-BOUNDED. A folded `RunState` holds every node's code, logs and trials, and
        # `AllRunsTools.list_runs` folds EVERY run under the root — so an unbounded map pinned all
        # of them in a long-lived assistant/server process for its whole lifetime. The bound is on
        # entry COUNT because the expensive thing here is the number of retained run states; a miss
        # only costs the re-fold this cache was already willing to pay on a signature change.
        self._cache: "OrderedDict[str, tuple]" = OrderedDict()  # run_id -> (sig, RunState)
        # Small on purpose: cross-run tools reason over a handful of runs per turn (a sibling, the
        # best few), while `list_runs` sweeps every run once and must not evict what the turn is
        # actually working with — 32 covers the working set without pinning a whole run-root.
        self._cache_max = 32
        # Divergence receipts live OUTSIDE the LRU, deliberately. `_list_runs` folds every run under
        # the root and only then asks each one whether its log was complete, so an evicted receipt
        # would silently drop the PARTIAL SOURCE marker and let a truncated log read as a whole run —
        # the exact claim `source_note` exists to prevent. A receipt is a handful of counters, so
        # keeping one per run seen costs nothing next to the RunStates the bound is actually for.
        self._partial: dict[str, dict] = {}

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
            return (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino, s.st_dev)
        except OSError:
            return (0, 0)

    def state(self, run_id: Optional[str]) -> Optional[RunState]:
        rd = self.safe_dir(run_id)
        if rd is None:
            return None
        sig = self.sig(rd)
        hit = self._cache.get(str(run_id))
        if hit and hit[0] == sig:
            self._cache.move_to_end(str(run_id))     # recency: survive the next eviction
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
        self._partial[str(run_id)] = divergence
        self._cache[str(run_id)] = (sig, st)
        self._cache.move_to_end(str(run_id))
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)          # drop the least recently used run state
        return st

    def partial(self, run_id: Optional[str]) -> Optional[dict]:
        """The divergence receipt for a run whose log could not be read to the end, else None.

        `state()` must be called first (it is, at every consumer: this answers "was what I just read
        the whole run?"). None also covers a run that was never read, which is honest — nothing was
        claimed about it either. Survives eviction of the run's folded state: the receipt describes
        the READ that happened, and forgetting it would turn a partial read into a silent whole."""
        d = self._partial.get(str(run_id))
        return d if d else None

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
