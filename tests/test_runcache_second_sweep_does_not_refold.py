"""A second sweep over the corpus in one turn must fold nothing.

`scan=True` made a sweep stop EVICTING the working set; it deliberately did not make the sweep's own
folds survive, because a sweep is wider than the LRU by construction. So the listing tools re-folded
every run every time they walked — and they walk more than once per turn by construction
(`SiblingRunTools._sibling_ids` walks every run to answer "which are mine", then the render pass
reads the survivors; the assistant's `summaries()` is called by the tool AND by @run-mention
expansion).

The close is a per-run PROJECTION (`_runcache.run_summary`) kept outside the LRU and keyed by the
same file-identity signature as the fold it came from — one row per run seen, the same shape as the
divergence receipt beside it, so nothing here is sized by a policy nobody has measured.

Driven with an ACCOUNTANT over `fold` itself: the expensive work is the fold, and counting it is the
only way to tell a cache that works from a cache that is merely present.
"""
from __future__ import annotations

import looplab.events.replay as replay_module
from looplab.events.eventstore import EventStore
from looplab.tools._runcache import RunStateCache, run_summary
from looplab.tools.run_tools import AllRunsTools


def _root(tmp_path, count: int, *, task: str = "t"):
    root = tmp_path / "runs"
    root.mkdir()
    for i in range(count):
        rd = root / f"run-{i:03d}"
        rd.mkdir()
        store = EventStore(rd / "events.jsonl")
        store.append("run_started", {"run_id": rd.name, "task_id": task, "goal": "g",
                                     "direction": "min"})
    return root


class _FoldAccountant:
    """Counts the folds the corpus actually pays for. `RunStateCache.state` imports `fold` from
    `looplab.events.replay` inside the method, so patching the module attribute reaches every read."""

    def __init__(self, monkeypatch):
        self.calls = 0
        inner = replay_module.fold

        def _counted(events):
            self.calls += 1
            return inner(events)

        monkeypatch.setattr(replay_module, "fold", _counted)


def test_a_second_sweep_over_the_corpus_folds_NOTHING(tmp_path, monkeypatch):
    """THE DEFECT, driven. MUTATION: drop `_summaries` and read `state(scan=True)` again -> the
    second sweep folds all 40 runs, and so does the third."""
    root = _root(tmp_path, 40)
    cache = RunStateCache(root)
    cache._cache_max = 8                                # the corpus is wider than the bound, as on a real box
    meter = _FoldAccountant(monkeypatch)

    first = [cache.summary(rid) for rid in cache.run_ids()]
    assert meter.calls == 40, "premise: the first sweep pays one fold per run"

    second = [cache.summary(rid) for rid in cache.run_ids()]
    assert meter.calls == 40, (
        f"the second sweep folded {meter.calls - 40} run(s) again — 8 LRU slots cannot hold a "
        "40-run corpus, which is the whole reason the ROW is kept when the state is not")
    assert second == first, "and it must answer identically, not merely cheaply"


def test_the_row_is_the_fold_and_a_CHANGED_log_is_refolded(tmp_path, monkeypatch):
    """A cache that can serve a stale row is worse than the cost it saves. The signature is the same
    `file_identity` the state cache keys on, so an appended log misses here too."""
    root = _root(tmp_path, 3)
    cache = RunStateCache(root)
    meter = _FoldAccountant(monkeypatch)

    before = cache.summary("run-001")
    assert before["nodes"] == 0 and meter.calls == 1

    EventStore(root / "run-001" / "events.jsonl").append(
        "node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                         "idea": {"operator": "draft", "params": {}}})
    after = cache.summary("run-001")

    assert meter.calls == 2, "an appended log must miss — the row is keyed on the log's identity"
    assert after["nodes"] == 1, f"the row went stale: {before} -> {after}"
    # And the projection is the fold's own answer, not a second reading of the log.
    assert after == run_summary(cache.state("run-001"))


def test_the_listing_TOOL_re_lists_without_re_folding(tmp_path, monkeypatch):
    """Driven through the real provider, because the property is about what a TURN costs: the
    output must be byte-identical and the second call must buy nothing."""
    root = _root(tmp_path, 20)
    tools = AllRunsTools(root, self_run_id="none")
    tools._runs._cache_max = 4
    meter = _FoldAccountant(monkeypatch)

    first = tools.execute("list_all_runs", {})
    folds_after_first = meter.calls
    assert folds_after_first == 20, f"premise: one fold per run on the cold pass ({folds_after_first})"

    second = tools.execute("list_all_runs", {})
    assert meter.calls == folds_after_first, (
        f"re-listing folded {meter.calls - folds_after_first} run(s) again")
    assert second == first
    assert "20 run(s)" in first and "run-019" in first, "and the listing still says what it said"


def test_the_row_carries_BOTH_metrics_the_two_listings_publish(tmp_path):
    """The projection replaced a `RunState` at two call sites that publish DIFFERENT metrics (the
    raw one in the string listings, `digest.node_metric` in the machine summaries). A row carrying
    one of them would change a tool's output while claiming to be a performance fix."""
    root = _root(tmp_path, 1)
    store = EventStore(root / "run-000" / "events.jsonl")
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}}})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.5, "feasible": True})

    row = RunStateCache(root).summary("run-000")
    assert row["nodes"] == 1 and row["task_id"] == "t" and row["direction"] == "min"
    assert row["best_node_id"] == 1
    assert row["best_metric"] == 0.5 and row["best_display_metric"] == 0.5


def test_a_run_that_cannot_be_read_gets_NO_row(tmp_path):
    """Same answer as `state()`: None, and nothing cached under it. A row invented for an
    unreadable run is a listing line asserting a run that was never read."""
    root = _root(tmp_path, 1)
    cache = RunStateCache(root)
    assert cache.summary("../escape") is None
    assert cache.summary("run-404") is None
    assert cache.summary(None) is None
    assert cache._summaries == {}
