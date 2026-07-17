"""Phase 1: `concept_metrics` — the per-concept outcome/Δ rollup (View 1's table). Pure over folded
state; joins each concept's touching experiments to their robust_metric; a multi-membership node's
metric is NOT divided across its concepts (full metric counts in each). Δ is signed vs the run's
median baseline so positive = better for the run's direction."""
from looplab.core.models import RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import concept_metrics, graph_from_node_concepts, _median


def _run(tmp_path, rows, direction="max"):
    """rows: list of (concepts_or_None, metric_or_None). metric=None -> created but not evaluated."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": direction})
    for i, (concepts, metric) in enumerate(rows):
        idea = {"operator": "draft", "params": {"seed": float(i)}, "rationale": "r"}
        if concepts is not None:
            idea["concepts"] = concepts
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": "draft", "idea": idea})
        if metric is not None:
            s.append("node_evaluated", {"node_id": i, "metric": metric})
    st = fold(s.read_all())
    graph, tags = graph_from_node_concepts(st.node_concepts)
    return st, concept_metrics(st, graph, tags)


def test_bare_single_segment_concepts_produce_metrics_rows(tmp_path):
    # A single-segment authored concept ("agents") is a first-class top-level concept: it is kept in the
    # graph/tags (not dropped as a non-path id), so a node tagged ONLY with a bare id is not falsely
    # untagged and concept_metrics produces its row alongside the deeper concepts.
    st, res = _run(tmp_path, [(["agents", "loss/dcl"], 0.9), (["agents"], 0.5)])
    graph, tags = graph_from_node_concepts(st.node_concepts)
    assert tags[0] == frozenset({"agents", "loss/dcl"})
    assert tags[1] == frozenset({"agents"})                   # not falsely untagged
    assert res["rows"]["agents"]["touched"] == 2 and res["rows"]["agents"]["best"] == 0.9


def test_subtree_rollup_aggregates_descendants_keeping_rows_leaf(tmp_path):
    # The frame parity invariant needs `rows` leaf/direct, so the axis aggregate lives in a SEPARATE
    # `rollup` map: for each concept, every experiment at or below it on the id-path, UNION per node.
    st, res = _run(tmp_path, [
        (["loss/dcl", "loss/triplet"], 0.9),     # one node, two loss leaves -> loss counted once
        (["loss/dcl"], 0.5),
        (["hyperparameter/temperature"], 0.7),
    ])
    assert "loss" not in res["rows"]              # rows stay leaf/direct (parity preserved)
    roll = res["rollup"]
    assert roll["loss"]["touched"] == 2          # union: node0 counts once for the axis
    assert roll["loss/dcl"]["touched"] == 2      # a leaf's rollup equals its own direct row
    assert roll["loss"]["best"] == 0.9           # best over descendants
    assert roll["hyperparameter"]["touched"] == 1
    assert res["baseline"] == 0.7                 # median(0.9,0.5,0.7)
    assert roll["loss"]["delta_best"] == 0.2      # 0.9 - 0.7, signed for direction=max


def test_median_helper():
    assert _median([]) is None
    assert _median([3.0, 1.0, 2.0]) == 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_per_concept_best_mean_delta_and_first_touch(tmp_path):
    st, res = _run(tmp_path, [
        (["loss/dcl", "architecture/moe"], 0.9),   # node 0 — best, multi-membership
        (["loss/dcl"], 0.7),                        # node 1
        (["data/synth"], 0.5),                      # node 2
        (["loss/dcl"], None),                       # node 3 — touched but not evaluated
    ])
    assert res["baseline"] == 0.7                    # median of [0.9, 0.7, 0.5]
    assert res["direction"] == "max"
    rows = res["rows"]
    dcl = rows["loss/dcl"]
    assert dcl["touched"] == 3 and dcl["evaluated"] == 2 and dcl["first_touch"] == 0
    assert dcl["best"] == 0.9 and dcl["mean"] == 0.8
    assert dcl["delta_best"] == 0.2 and dcl["delta_mean"] == 0.1
    data = rows["data/synth"]
    assert data["best"] == 0.5 and data["delta_best"] == -0.2 and data["first_touch"] == 2


def test_multi_membership_metric_is_not_divided(tmp_path):
    # node 0's 0.9 must count FULLY in BOTH of its concepts — never split.
    _, res = _run(tmp_path, [(["loss/dcl", "architecture/moe"], 0.9)])
    assert res["rows"]["loss/dcl"]["best"] == 0.9
    assert res["rows"]["architecture/moe"]["best"] == 0.9   # full, not 0.45


def test_min_direction_flips_best_and_delta_sign(tmp_path):
    _, res = _run(tmp_path, [(["loss/x"], 0.2), (["loss/x"], 0.5), (["a/b"], 0.8)], direction="min")
    assert res["baseline"] == 0.5
    lx = res["rows"]["loss/x"]
    assert lx["best"] == 0.2 and lx["worst"] == 0.5           # min: lower is better
    assert lx["delta_best"] == 0.3                            # sign flipped: 0.2 is 0.3 BETTER than 0.5


def test_empty_and_untagged(tmp_path):
    _, res = _run(tmp_path, [(None, 0.5)])                    # a node with no concepts
    assert res["rows"] == {} and res["baseline"] == 0.5


def test_replay_stable(tmp_path):
    st, res = _run(tmp_path, [(["b/x"], 0.4), (["a/y"], 0.6)])
    graph, tags = graph_from_node_concepts(st.node_concepts)
    assert concept_metrics(st, graph, tags) == res           # deterministic recompute
