"""A sweep over every run must not evict what the turn is actually working with.

`RunStateCache`'s own comment states the policy — "`list_runs` sweeps every run once and must not
evict what the turn is actually working with — 32 covers the working set without pinning a whole
run-root" — and the code did the opposite: a sweep promoted every hit and inserted every miss at the
HOT end, so a walk over a 46-59 run corpus evicted all 32 warm entries and kept the sweep's last 32.
The working set was gone, replaced by runs the turn had not asked about.

`scan=True` is the standard scan-resistant read: no promotion on a hit (a sweep visits each run
once, so promoting can never help the sweep) and a miss lands at the COLD end, so a sweep churns
roughly one slot. The default is unchanged, so every single-run reader behaves exactly as before.

Driven against the real cache over real event logs — the folded state is what the eviction is about,
and a stub would let the LRU bookkeeping pass while the folds it protects were still thrown away.
"""
from __future__ import annotations

import pytest

from looplab.events.eventstore import EventStore
from looplab.tools._runcache import RunStateCache


def _root(tmp_path, count: int):
    root = tmp_path / "runs"
    root.mkdir()
    for i in range(count):
        rd = root / f"run-{i:03d}"
        rd.mkdir()
        EventStore(rd / "events.jsonl").append(
            "run_started", {"run_id": rd.name, "task_id": "t", "goal": "g", "direction": "min"})
    return root


def _cached(cache) -> list[str]:
    return list(cache._cache)


def test_a_sweep_does_not_evict_the_working_set(tmp_path):
    """THE DEFECT, driven. MUTATION: drop `scan` from the sweep -> `working` is gone and the cache
    holds the last 32 runs of a walk nobody asked about."""
    root = _root(tmp_path, 50)
    cache = RunStateCache(root)
    cache._cache_max = 8

    working = ["run-000", "run-001", "run-002"]
    for rid in working:
        assert cache.state(rid) is not None            # ordinary reads: the turn's working set

    for rid in cache.run_ids():                        # the sweep
        cache.state(rid, scan=True)

    for rid in working:
        assert rid in _cached(cache), f"{rid} was evicted by a sweep"


def test_the_same_sweep_WITHOUT_the_flag_destroys_it(tmp_path):
    """The negative control: without this the test above passes on a cache that never evicts, and
    the property would be untested rather than held."""
    root = _root(tmp_path, 50)
    cache = RunStateCache(root)
    cache._cache_max = 8

    working = ["run-000", "run-001", "run-002"]
    for rid in working:
        cache.state(rid)
    for rid in cache.run_ids():
        cache.state(rid)                               # the historical behaviour

    assert not any(rid in _cached(cache) for rid in working), (
        "the unflagged sweep must still evict — otherwise the flag is measuring nothing")


def test_a_sweep_hit_does_not_promote(tmp_path):
    """A sweep visits each run once, so promoting on a hit can never help the sweep — it can only
    demote whatever the turn is using."""
    root = _root(tmp_path, 4)
    cache = RunStateCache(root)

    cache.state("run-000")
    cache.state("run-001")
    before = _cached(cache)

    cache.state("run-000", scan=True)
    assert _cached(cache) == before, "a scan hit reordered the LRU"

    cache.state("run-000")
    assert _cached(cache) == ["run-001", "run-000"], "an ordinary hit must still promote"


def test_a_sweep_still_returns_the_right_state(tmp_path):
    """Scan-resistance is about RETENTION. A read that returned something else, or nothing, would
    be a correctness bug wearing a performance fix's clothes."""
    root = _root(tmp_path, 40)
    cache = RunStateCache(root)
    cache._cache_max = 4
    for rid in cache.run_ids():
        st = cache.state(rid, scan=True)
        assert st is not None and st.run_id == rid


def test_the_bound_still_holds_under_a_sweep(tmp_path):
    """The LRU bound is what stops a long-lived assistant pinning a whole run-root."""
    root = _root(tmp_path, 40)
    cache = RunStateCache(root)
    cache._cache_max = 4
    for rid in cache.run_ids():
        cache.state(rid, scan=True)
    assert len(cache._cache) <= cache._cache_max


def test_the_divergence_receipt_survives_a_sweep(tmp_path):
    """Receipts live outside the LRU for a stated reason — an evicted one would let a truncated log
    read as a whole run. A scan read must record one exactly like any other."""
    root = _root(tmp_path, 12)
    cache = RunStateCache(root)
    cache._cache_max = 2
    for rid in cache.run_ids():
        cache.state(rid, scan=True)
    assert len(cache._partial) == 12


def test_the_default_is_the_historical_behaviour(tmp_path):
    """Every single-run reader is unchanged: this is what makes the flag safe to add."""
    root = _root(tmp_path, 6)
    cache = RunStateCache(root)
    cache._cache_max = 3
    for rid in ["run-000", "run-001", "run-002", "run-003"]:
        cache.state(rid)
    assert _cached(cache) == ["run-001", "run-002", "run-003"], "plain LRU, hot insert, drop oldest"


def test_every_full_population_sweep_passes_the_flag():
    """The rule the cache's own docstring states: a read is a SCAN when its population is
    `run_ids()`. AST over the real loops, so a new listing tool that forgets it goes red.

    MUTATION: drop `scan=True` at any of the three sites -> that tool's walk evicts the working set
    again, silently, and only a timing measurement would ever notice.
    """
    import ast

    from _source_scan import function_tree
    from looplab.tools import machine_runs_tools, run_tools

    sweeps = [
        (run_tools.SiblingRunTools._sibling_ids, "run_ids"),
        (run_tools.AllRunsTools._list_runs, "_all_ids"),
        (machine_runs_tools.MachineRunsTools.summaries, "_run_ids"),
    ]
    for func, population in sweeps:
        tree = function_tree(func)
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "_state"]
        assert calls, f"{func.__qualname__} no longer reads state — re-point this guard"
        for call in calls:
            flags = {kw.arg: getattr(kw.value, "value", None) for kw in call.keywords}
            assert flags.get("scan") is True, (
                f"{func.__qualname__} sweeps {population}() without scan=True, so its folds evict "
                f"the runs the turn is working with")
