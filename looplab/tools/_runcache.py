"""Shared fold-on-demand run-state cache (BACKLOG §4 "RunStateCache").

`SiblingRunTools` (run_tools.py) and `MachineRunsTools` (machine_runs_tools.py) both read OTHER runs' event logs
off disk: resolve <run_root>/<run_id> with a path-traversal guard, fold the log into a `RunState`,
and cache the fold by the log's (size, mtime) fingerprint so repeated turns don't re-fold unchanged
runs. That plumbing was duplicated verbatim in both providers; it lives here once and they delegate.
Beside the bounded state cache sits `summary()`, the per-run LISTING ROW (`run_summary`): a sweep is
wider than the LRU by construction, so the row is what survives it and what a second sweep answers
from.
Every reader soft-fails (returns None / []) — a junk run_id or a torn log must never crash the loop.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import file_identity
from looplab.core.models import RunState
from looplab.events import digest


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
        # WHAT WAS FIXED, and why it needed no measurement: this comment ALREADY stated the policy
        # ("`list_runs` sweeps every run once and must not evict what the turn is actually working
        # with") and the code did the opposite. A sweep promoted every hit and inserted every miss
        # at the HOT end, so a 46-run walk evicted all 32 warm entries and kept the sweep's last 32
        # — the working set gone, replaced by runs the turn was not asking about. `state(scan=True)`
        # is the standard scan-resistant read: no promotion on a hit (a sweep visits each run once,
        # so promoting can never help the sweep and demotes the working set), and a miss lands at
        # the COLD end, so a sweep churns roughly one slot instead of the whole cache.
        self._cache_max = 32
        # Divergence receipts live OUTSIDE the LRU, deliberately. `_list_runs` folds every run under
        # the root and only then asks each one whether its log was complete, so an evicted receipt
        # would silently drop the PARTIAL SOURCE marker and let a truncated log read as a whole run —
        # the exact claim `source_note` exists to prevent. A receipt is a handful of counters, so
        # keeping one per run seen costs nothing next to the RunStates the bound is actually for.
        self._partial: dict[str, dict] = {}
        # THE SWEEP'S ANSWER, kept when its `RunState` is not. A sweep over `run_ids()` renders one
        # ROW per run — the handful of scalars `run_summary` below projects — and then drops the
        # folded state it read them off. The LRU above deliberately does not retain that state (a
        # sweep is wider than the bound by construction), so a SECOND sweep in the same turn re-
        # folded every run, and the listing tools sweep more than once by construction:
        # `SiblingRunTools._sibling_ids` walks every run to answer "which are mine", and the render
        # pass then reads the survivors again.
        #
        # THIS NEEDED NO CORPUS MEASUREMENT, which is exactly why it is here and a bigger
        # `_cache_max` is not. Widening the bound is a POLICY sized in RunStates — every node's
        # code, logs and trials — and sizing that without knowing the corpus is the unmeasured
        # policy this repo refuses elsewhere. A projection is not a policy: one row per run seen is
        # a handful of scalars, the same shape and the same argument as `_partial` above and
        # `run_tools.py::ForeignRunReader._contracts` one layer up, both already unbounded in run
        # COUNT for this reason. Keyed by the same `file_identity` signature as the fold it came
        # from, so an appended log misses here too — a stale listing row is the one thing these
        # tools must never publish, and it is unrepresentable rather than merely unlikely.
        self._summaries: dict[str, tuple] = {}   # run_id -> (sig, row)

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
            # The canonical full-strength identity, not `int(st_mtime)`: these folded states feed
            # default-on SiblingRunTools/AllRunsTools, so a stale hit lets an agent reason from an
            # obsolete cross-run evidence prefix. Besides nanosecond timestamps and dev/inode, the
            # shared tuple includes Windows' reparse-point attributes; omitting that last field let
            # a path switch underneath the cache while still comparing equal.
            return file_identity(s)
        except OSError:
            return (0, 0)

    def state(self, run_id: Optional[str], *, scan: bool = False) -> Optional[RunState]:
        """The folded `RunState` for one run, or None.

        `scan=True` marks a read that is part of a SWEEP over every run id — what every listing
        tool does. Such a read is deliberately not recency-bearing: see `_cache_max` above for what
        the promotion cost. The default is byte-identical to the historical behaviour, so every
        single-run reader is unchanged.

        THE RULE FOR A CALLER, stated once because "is this a sweep?" is otherwise a judgement each
        site makes differently: a read is a SCAN when its population is `run_ids()`, i.e. every run
        under the root. A loop over an already-scoped subset (`RunTools._sibling_ids`' task-filtered
        result) is an ordinary read — those runs ARE what the turn is reasoning about, and demoting
        them is the eviction this flag exists to prevent, pointed the wrong way.
        """
        rd = self.safe_dir(run_id)
        if rd is None:
            return None
        sig = self.sig(rd)
        hit = self._cache.get(str(run_id))
        if hit and hit[0] == sig:
            if not scan:
                self._cache.move_to_end(str(run_id))  # recency: survive the next eviction
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
        # RECORDED OFF THIS FOLD, not off a later re-read: deriving the listing row anywhere else
        # would make it a second reading of the log, free to disagree with the state just returned.
        self._summaries[str(run_id)] = (sig, run_summary(st))
        # EVICT BEFORE INSERTING, and that order is the whole of it. Inserting first and then
        # `move_to_end(last=False)` put the new entry at the FRONT — which is the end
        # `popitem(last=False)` pops — so at capacity a sweep evicted the entry it had just made and
        # cached NOTHING, not the "one slot" the rule below describes. On a 46-run root with 32
        # slots that meant every scanned run was folded and thrown away, and the render pass that
        # follows re-folded all of them.
        while len(self._cache) >= self._cache_max:
            self._cache.popitem(last=False)          # drop the least recently used run state
        self._cache[str(run_id)] = (sig, st)
        # A SWEEP'S MISS LANDS COLD. `move_to_end(last=False)` makes it the next thing evicted, so a
        # 46-run walk over 32 slots churns one slot instead of replacing the whole working set with
        # runs the turn never asked about. A single-run read still lands hot, unchanged.
        #
        # THE RESIDUE IS DELIBERATE and is the trade this rule already made: consecutive sweep
        # misses still evict each other, so a sweep wider than the cache leaves one entry behind and
        # a render pass over the same runs re-folds the rest. Caching the sweep instead would
        # replace the working set with runs the turn never asked about, which is what the cold
        # landing exists to prevent. Widening `_cache_max` is the lever, not this order.
        self._cache.move_to_end(str(run_id), last=not scan)
        return st

    def summary(self, run_id: Optional[str]) -> Optional[dict]:
        """The LISTING projection of one run — the row a sweep renders — without retaining its
        `RunState`. None for a run that cannot be read, exactly like `state()`.

        THE READ THIS EXISTS FOR is a sweep over `run_ids()`, and a sweep is wider than the LRU by
        construction, so its folds are gone before the next sweep asks for them. The fold behind a
        MISS here is the same one `state(scan=True)` would have paid — nothing is read twice and
        nothing is read differently — but the ANSWER outlives the state, so the second sweep of a
        turn costs zero folds at any corpus size, which is the property the bound cannot give.

        A caller needing anything the row does not carry still calls `state()`. This is a
        projection, deliberately not a substitute, and widening it is how it would become one.
        """
        rd = self.safe_dir(run_id)
        if rd is None:
            return None
        sig = self.sig(rd)
        hit = self._summaries.get(str(run_id))
        if hit and hit[0] == sig:
            return hit[1]
        # A miss folds through `state()` — one code path for reading a log, one place the divergence
        # receipt is recorded — and as a SCAN, because every caller of this walks the whole
        # population (`state`'s docstring states that rule for both).
        st = self.state(run_id, scan=True)
        if st is None:
            return None
        hit = self._summaries.get(str(run_id))
        if hit and hit[0] == sig:
            return hit[1]
        # TOTAL rather than trusting the write above to have happened under THIS signature:
        # `state()` may have answered from its own cache, or stat'd a log that grew between the two
        # reads. A row is cheap enough that re-deriving it beats an assumption about a sibling.
        row = run_summary(st)
        self._summaries[str(run_id)] = (sig, row)
        return row

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


def run_summary(state: RunState) -> dict:
    """The handful of scalars a LISTING row is made of, projected off one folded run.

    Pure and total. Every listing tool under `tools/` renders its rows out of exactly these fields,
    which is what makes the row the thing a sweep may keep once the `RunState` it came from is gone.
    Both metrics are carried because the two listings have always published different ones —
    `machine_runs_tools.py::MachineRunsTools.summaries` publishes the ranking/display metric
    (`events/digest.py::node_metric`, the robust confirmed mean when there is one) and the string
    listings publish the raw — and a row that dropped either would quietly change a tool's output
    while claiming to be a performance fix.

    NOT the place for anything a reader must DECIDE on: the divergence receipt (`partial`) and the
    evaluation-contract notice stay their own reads, because a row asserting them would be asserting
    them for a run whose log it is no longer holding.
    """
    best = state.best()
    return {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "goal": state.goal,
        "direction": state.direction,
        "finished": bool(state.finished),
        "nodes": len(state.nodes),
        "best_node_id": (best.id if best is not None else None),
        "best_metric": (best.metric if best is not None else None),
        "best_display_metric": (digest.node_metric(best) if best is not None else None),
    }
