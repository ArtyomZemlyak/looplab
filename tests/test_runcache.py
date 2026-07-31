"""RunStateCache fingerprint freshness (shared cross-run fold cache).

The cache keys a folded RunState by the event log's fingerprint. Truncating mtime to whole seconds
(`int(st_mtime)`) made a same-size rewrite WITHIN one second invisible, so SiblingRunTools/
MachineRunsTools could keep serving an obsolete cross-run evidence prefix. Offline."""
from __future__ import annotations

import os

from looplab.tools._runcache import RunStateCache


def test_sig_detects_a_same_size_subsecond_rewrite(tmp_path):
    rd = tmp_path / "runX"
    rd.mkdir()
    path = rd / "events.jsonl"
    path.write_text('{"seq": 0}\n', encoding="utf-8")     # size is identical across the two utimes

    base = (path.stat().st_mtime_ns // 1_000_000_000) * 1_000_000_000   # floor to a whole second
    early, late = base + 100, base + 800_000_000                        # SAME whole second, diff ns
    assert early // 1_000_000_000 == late // 1_000_000_000              # int(st_mtime) would collide

    os.utime(path, ns=(early, early))
    sig1 = RunStateCache.sig(rd)
    os.utime(path, ns=(late, late))                        # a same-size rewrite within one second
    sig2 = RunStateCache.sig(rd)
    assert sig1 != sig2, "fingerprint must be sub-second aware (nanosecond mtime), not int(st_mtime)"


def test_corrupt_complete_tail_is_marked_partial_not_silently_complete(tmp_path):
    from looplab.events.eventstore import EventStore

    rd = tmp_path / "runX"
    rd.mkdir()
    path = rd / "events.jsonl"
    EventStore(path).append("run_started", {
        "run_id": "runX", "task_id": "task", "goal": "goal", "direction": "max",
    })
    cache = RunStateCache(tmp_path)
    assert cache.state("runX") is not None

    with path.open("ab") as stream:
        stream.write(b"{complete corrupt record}\n")

    assert cache.state("runX") is not None
    assert cache.partial("runX") is not None
    assert "PARTIAL SOURCE" in cache.source_note("runX")


def _seed_run(root, name: str, *, corrupt: bool = False):
    from looplab.events.eventstore import EventStore

    rd = root / name
    rd.mkdir()
    path = rd / "events.jsonl"
    EventStore(path).append("run_started", {
        "run_id": name, "task_id": "task", "goal": "goal", "direction": "max",
    })
    if corrupt:
        with path.open("ab") as stream:
            stream.write(b"{complete corrupt record}\n")
    return rd


def test_the_fold_cache_is_lru_bounded_so_listing_a_run_root_cannot_pin_every_state(tmp_path):
    """A folded RunState carries every node's code, logs and trials, and `list_runs` folds EVERY run
    under the root — an unbounded map kept all of them alive for a server's whole lifetime."""
    cache = RunStateCache(tmp_path)
    cache._cache_max = 4
    for i in range(10):
        _seed_run(tmp_path, f"run{i}")
        assert cache.state(f"run{i}") is not None
    assert len(cache._cache) == 4, len(cache._cache)
    assert set(cache._cache) == {f"run{i}" for i in range(6, 10)}     # the LEAST recent were dropped


def test_eviction_never_forgets_that_a_run_was_read_only_in_part(tmp_path):
    """`_list_runs` folds every run first and asks about completeness afterwards, so a receipt tied
    to the LRU entry would let a truncated log read as a whole run — the one claim source_note
    exists to prevent."""
    cache = RunStateCache(tmp_path)
    cache._cache_max = 2
    _seed_run(tmp_path, "torn", corrupt=True)
    assert cache.state("torn") is not None
    assert "PARTIAL SOURCE" in cache.source_note("torn")

    for i in range(5):                                    # push "torn" out of the LRU entirely
        _seed_run(tmp_path, f"other{i}")
        cache.state(f"other{i}")
    assert "torn" not in cache._cache

    assert cache.partial("torn") is not None
    assert "PARTIAL SOURCE" in cache.source_note("torn"), (
        "the completeness receipt was evicted with the folded state — a truncated log now reads "
        "as a complete run")
