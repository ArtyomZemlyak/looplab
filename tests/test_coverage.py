"""Coverage read-model (narrowing signal) — pure signal + event/fold + replay-safe cadence.

The read-model is CONTEXT for the Strategist, never a decision; these tests lock in that it is
deterministic over RunState, folds as an audit-only breadth curve, and is emitted idempotently at
the strategist cadence (so a resume never re-records or duplicates a snapshot)."""
from __future__ import annotations

from pathlib import Path

import anyio

from looplab.agents.strategist import StrategyContext
from looplab.adapters.toytask import ToyTask
from looplab.core.models import RunState
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.coverage import coverage_signal, normalized_entropy
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def _store(tmp_path, nodes) -> EventStore:
    """Build a run log from `nodes` = [(theme, params, metric), ...]; metric=None => a failed node."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "min"})
    for i, (theme, params, metric) in enumerate(nodes):
        op = "draft" if i < 3 else "improve"
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": op,
                                  "idea": {"operator": op, "params": params, "theme": theme,
                                           "rationale": ""}})
        if metric is None:
            s.append("node_failed", {"node_id": i, "error": "boom", "reason": "crash"})
        else:
            s.append("node_evaluated", {"node_id": i, "metric": metric})
    return s


# --------------------------------------------------------------------------- #
# Pure signal
# --------------------------------------------------------------------------- #

def test_empty_run_is_all_zeros():
    sig = coverage_signal(RunState())
    assert sig["nodes"] == 0 and sig["themes"] == 0 and sig["niches"] == 0
    assert sig["theme_entropy"] == 0.0 and sig["dominant_theme_frac"] == 0.0
    assert sig["top_themes"] == []


def test_single_theme_reads_as_narrow(tmp_path):
    # everything on one theme -> entropy 0, dominant fraction 1.0 (maximally narrow)
    st = fold(_store(tmp_path, [("lr", {"x": float(i)}, float(i)) for i in range(5)]).read_all())
    sig = coverage_signal(st)
    assert sig["themes"] == 1
    assert sig["theme_entropy"] == 0.0
    assert sig["dominant_theme_frac"] == 1.0
    assert sig["top_themes"][0][0] == "lr"


def test_spread_themes_read_as_broad(tmp_path):
    # four distinct themes, evenly -> entropy 1.0, dominant fraction 0.25 (maximally broad)
    st = fold(_store(tmp_path, [(t, {"x": float(i)}, float(i))
                                for i, t in enumerate(["lr", "arch", "loss", "data"])]).read_all())
    sig = coverage_signal(st)
    assert sig["themes"] == 4
    assert sig["theme_entropy"] == 1.0
    assert sig["dominant_theme_frac"] == 0.25


def test_recent_window_flags_narrowing_now(tmp_path):
    # broad early, then the last 4 nodes all collapse onto one theme
    nodes = [("a", {"x": 0.0}, 0.0), ("b", {"x": 1.0}, 1.0), ("c", {"x": 2.0}, 2.0)]
    nodes += [("z", {"x": float(k)}, float(k)) for k in range(3, 7)]   # 4 recent, all theme "z"
    sig = coverage_signal(fold(_store(tmp_path, nodes).read_all()), recent=4)
    assert sig["recent_dominant_frac"] == 1.0   # the run is narrowing NOW
    assert sig["dominant_theme_frac"] < 1.0      # ... even though overall it was broader


def test_untitled_ideas_dilute_dominant_fraction(tmp_path):
    # 6 untitled ideas + 2 on theme 'a': the dominant fraction is over ALL idea-nodes (2/8=0.25),
    # NOT over themed nodes (which would be a misleading 2/2=1.0). Untitled effort is not narrowing.
    nodes = [(None, {"x": float(i)}, float(i)) for i in range(6)]
    nodes += [("a", {"x": 6.0}, 6.0), ("a", {"x": 7.0}, 7.0)]
    sig = coverage_signal(fold(_store(tmp_path, nodes).read_all()))
    assert sig["themes"] == 1
    assert sig["dominant_theme_frac"] == 0.25   # 2/8 — untitled dilute, not 1.0


def test_recent_window_is_time_based_not_theme_filtered(tmp_path):
    # old themed 'a','a' then the last 4 nodes are all untitled (fresh breadth): the recency window
    # is the last `recent` NODES, so it reads 0.0 — the old dominant theme must not reach forward.
    nodes = [("a", {"x": 0.0}, 0.0), ("a", {"x": 1.0}, 1.0)]
    nodes += [(None, {"x": float(k)}, float(k)) for k in range(2, 6)]
    sig = coverage_signal(fold(_store(tmp_path, nodes).read_all()), recent=4)
    assert sig["recent_dominant_frac"] == 0.0   # last 4 untitled -> not narrowing NOW


def test_failed_nodes_count_for_themes_but_not_niches(tmp_path):
    # a failed node has an idea (theme/params) but no metric -> it is a theme, not a param-niche elite
    st = fold(_store(tmp_path, [("lr", {"x": 1.0}, None), ("arch", {"y": 2.0}, 3.0)]).read_all())
    sig = coverage_signal(st)
    assert sig["nodes"] == 2 and sig["themes"] == 2
    assert sig["niches"] == 1   # only the one evaluated node forms a niche


def test_signal_is_deterministic(tmp_path):
    st = fold(_store(tmp_path, [("a", {"x": 1.0}, 1.0), ("b", {"x": 2.0}, 2.0)]).read_all())
    assert coverage_signal(st) == coverage_signal(st)


def test_normalized_entropy_bounds():
    assert normalized_entropy([]) == 0.0
    assert normalized_entropy([5]) == 0.0          # single category
    assert normalized_entropy([3, 3]) == 1.0       # even -> max
    assert 0.0 < normalized_entropy([9, 1]) < 1.0  # skewed -> between


# --------------------------------------------------------------------------- #
# Event / fold
# --------------------------------------------------------------------------- #

def test_coverage_snapshot_folds_as_audit_only(tmp_path):
    s = _store(tmp_path, [("a", {"x": 1.0}, 1.0)])
    s.append("coverage_snapshot", {"at_node": 1, "themes": 1, "theme_entropy": 0.0})
    st = fold(s.read_all())
    assert len(st.coverage_snapshots) == 1 and st.coverage_snapshots[0]["at_node"] == 1
    assert st.best_node_id == 0   # audit-only: selection is unchanged by the snapshot


def test_already_covered_at_gate():
    st = RunState()
    assert not Engine._already_covered_at(st, 3)
    st.coverage_snapshots.append({"at_node": 3})
    assert Engine._already_covered_at(st, 3)
    assert not Engine._already_covered_at(st, 6)


# --------------------------------------------------------------------------- #
# End-to-end cadence (offline toy run)
# --------------------------------------------------------------------------- #

def _engine(run_dir, **kw):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=3, max_nodes=8),
                  n_seeds=3, max_nodes=8, strategist_every=3, **kw)


def test_toy_run_emits_coverage_at_cadence_no_dupes(tmp_path):
    anyio.run(_engine(tmp_path / "cov").run)
    evs = EventStore(tmp_path / "cov" / "events.jsonl").read_all()
    snaps = [e for e in evs if e.type == "coverage_snapshot"]
    assert snaps, "expected coverage snapshots at the strategist cadence"
    at_nodes = [e.data["at_node"] for e in snaps]
    assert at_nodes == sorted(set(at_nodes))          # one per node-count, strictly increasing
    assert all(k in snaps[0].data for k in ("themes", "theme_entropy", "dominant_theme_frac"))


def test_coverage_survives_replay(tmp_path):
    state = anyio.run(_engine(tmp_path / "cov").run)
    replayed = fold(EventStore(tmp_path / "cov" / "events.jsonl").read_all())
    assert replayed.coverage_snapshots == state.coverage_snapshots
    assert replayed.coverage_snapshots   # non-empty


def test_coverage_context_off_emits_nothing(tmp_path):
    anyio.run(_engine(tmp_path / "off", coverage_context=False).run)
    evs = EventStore(tmp_path / "off" / "events.jsonl").read_all()
    assert not any(e.type == "coverage_snapshot" for e in evs)


def test_strategy_ctx_carries_coverage():
    # the field exists and defaults empty; the engine fills it from coverage_signal when on
    assert StrategyContext().coverage == {}
