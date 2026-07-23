"""Action-space lock-in detector (PART IV D7, §21.8 — Phase 1a).

Locks in that the detector reads the 0a concept graph to find the longest run of CONSECUTIVE
experiments confined to one axis-region (the "same-lever streak" the flat coverage signal was blind
to), fires a ≥N-consecutive alarm, and is pure/deterministic. It never writes events or touches
selection."""
from __future__ import annotations

from looplab.core.models import RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import dense_retrieval_skeleton
from looplab.search.lock_in import lock_in_report, lock_in_signal


def _store(tmp_path, nodes) -> EventStore:
    """nodes = [(theme, rationale), ...] in id order (all evaluated)."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    for i, (theme, rationale) in enumerate(nodes):
        op = "draft" if i < 3 else "improve"
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": op,
                                  "idea": {"operator": op, "params": {"seed": float(i)},
                                           "theme": theme, "rationale": rationale}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.8 + i * 0.001})
    return s


def test_empty_run_does_not_fire():
    sig = lock_in_signal(RunState(), dense_retrieval_skeleton())
    assert sig["experiments"] == 0 and sig["fired"] is False and sig["streak"] == 0


def test_long_single_axis_streak_fires(tmp_path):
    # 3 mixed seeds then 8 consecutive loss-axis (DCL + r-drop) experiments
    nodes = [("data-aug", "data augmentation"), ("mnr", "mnr multiple-negatives loss run"),
             ("distill", "teacher distillation from cross-encoder")]
    nodes += [(f"dcl-{i}", "decoupled contrastive with r-drop") for i in range(8)]
    st = fold(_store(tmp_path, nodes).read_all())
    sig = lock_in_signal(st, dense_retrieval_skeleton())
    assert sig["fired"] is True
    assert sig["locked_axis"] == "loss"
    assert sig["streak"] == 8
    assert sig["streak_start_node"] == 3       # the run begins at the 4th node (id 3)
    assert sig["current_streak"] == 8          # and it is still going at the end


def test_mixed_run_does_not_fire(tmp_path):
    # alternating axes -> no long single-axis run
    nodes = [("a", "data augmentation"), ("b", "mnr loss"), ("c", "teacher distillation"),
             ("d", "external hard negative mining"), ("e", "eval ndcg tuning"),
             ("f", "data augmentation"), ("g", "mnr loss")]
    st = fold(_store(tmp_path, nodes).read_all())
    sig = lock_in_signal(st, dense_retrieval_skeleton(), streak_threshold=5)
    assert sig["fired"] is False
    assert sig["streak"] < 5


def test_untagged_node_breaks_the_streak(tmp_path):
    # a run of loss nodes with an untagged node in the middle -> the streak resets, longest is 3 not 7
    nodes = [("x0", "decoupled contrastive r-drop")] * 3
    nodes = [(f"loss-{i}", "decoupled contrastive r-drop") for i in range(3)]
    nodes += [("mystery", "an approach with no matching alias at all")]      # untagged -> breaks
    nodes += [(f"loss2-{i}", "decoupled contrastive r-drop") for i in range(3)]
    st = fold(_store(tmp_path, nodes).read_all())
    sig = lock_in_signal(st, dense_retrieval_skeleton(), streak_threshold=5)
    assert sig["streak"] == 3               # the untagged node split the two 3-runs
    assert sig["fired"] is False


def test_recent_window_concentration(tmp_path):
    nodes = [("a", "data augmentation"), ("b", "external hard negative mining")]
    nodes += [(f"loss-{i}", "decoupled contrastive r-drop") for i in range(6)]   # last 6 on loss
    st = fold(_store(tmp_path, nodes).read_all())
    sig = lock_in_signal(st, dense_retrieval_skeleton(), recent=6)
    assert sig["recent_axis"] == "loss"
    assert sig["recent_frac"] == 1.0


def test_lock_in_excludes_aborted_and_tombstoned_history(tmp_path):
    nodes = [(f"loss-{i}", "decoupled contrastive r-drop") for i in range(6)]
    nodes.append(("data", "data augmentation"))
    st = fold(_store(tmp_path, nodes).read_all())
    st.aborted_nodes = list(range(5))
    st.nodes[5].tombstoned = True

    sig = lock_in_signal(st, dense_retrieval_skeleton(), streak_threshold=5)

    assert sig["experiments"] == sig["tagged"] == 1
    assert sig["locked_axis"] == "data" and sig["streak"] == 1
    assert sig["fired"] is False


def test_signal_pins_tie_broken_fields(tmp_path):
    # each node touches loss AND regularization (tie); the tie-break must PIN loss (min axis name) — a
    # pinned value catches iteration-order leakage a same-process f(x)==f(x) cannot.
    nodes = [(f"loss-{i}", "decoupled contrastive r-drop") for i in range(6)]
    st = fold(_store(tmp_path, nodes).read_all())
    g = dense_retrieval_skeleton()
    sig = lock_in_signal(st, g)
    assert sig["locked_axis"] == "loss" and sig["streak"] == 6 and sig["recent_axis"] == "loss"


def test_report_renders_alarm(tmp_path):
    nodes = [(f"loss-{i}", "decoupled contrastive r-drop") for i in range(6)]
    st = fold(_store(tmp_path, nodes).read_all())
    rep = lock_in_report(st, dense_retrieval_skeleton())
    assert "LOCK-IN ALARM" in rep and "loss" in rep


def test_pure_analytics_are_hash_seed_stable():
    """The order-stability invariant (no set/frozenset/dict iteration order leaking into a returned
    value) can only be caught ACROSS hash seeds — a same-process f(x)==f(x) iterates every set the SAME
    way both times and is trivially satisfied. Run the pure Phase-1 analytics (lock-in, board dedup,
    research targets) under two PYTHONHASHSEEDs in subprocesses and assert byte-identical JSON."""
    import os
    import subprocess
    import sys
    import textwrap
    script = textwrap.dedent("""
        import json
        from looplab.core.models import Card, Idea, Node, NodeStatus, RunState
        from looplab.search.concept_graph import skeleton_for
        from looplab.search.lock_in import lock_in_signal
        from looplab.search.taxonomy_dedup import dedup_analysis
        from looplab.search.research_targeting import research_targets
        st = RunState(direction="max", task_id="dr")
        for i in range(6):
            st.nodes[i] = Node(id=i, operator="improve", metric=0.8, status=NodeStatus.evaluated,
                               idea=Idea(operator="improve", params={"seed": float(i)},
                                         rationale="decoupled contrastive r-drop ema temperature"))
        for i in range(6, 10):
            # 1 card = 1 hypothesis: the board dedup reads research_cards(); populate the Card board.
            s = "decoupled contrastive r-drop temperature variant %d" % i
            st.cards["h%d" % i] = Card(id="h%d" % i, seed_statement=s, statement=s,
                                       verdict="open", status="proposed")
        g = skeleton_for("dense-retrieval")
        print(json.dumps({"lock": lock_in_signal(st, g), "dedup": dedup_analysis(st, g),
                          "targets": research_targets(st, g)}, sort_keys=True))
    """)

    def run(seed: int) -> str:
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=env, check=True).stdout

    out0, out1, out2 = run(0), run(1), run(7)
    assert out0 and out0 == out1 == out2
