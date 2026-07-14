"""Taxonomy-aware board dedup analysis (PART IV D4, §21.5/§21.12 — Phase 1d).

Locks in that the analysis tags the hypothesis board, surfaces the dominant within-concept cluster (the
redundancy to merge aggressively), and flags cross-branch look-alike pairs a blind merge would wrongly
collapse (the protective value taxonomy-awareness buys). Pure/deterministic; merges nothing."""
from __future__ import annotations

from looplab.core.models import RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import skeleton_for
from looplab.search.taxonomy_dedup import dedup_analysis, dedup_report


def _board(tmp_path, statements) -> RunState:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    for i, stmt in enumerate(statements):
        s.append("hypothesis_added", {"id": f"h{i}", "statement": stmt, "source": "researcher"})
    return fold(s.read_all())


_DCL_BOARD = [
    "decoupled contrastive with r-drop",
    "decoupled contrastive tuning temperature",
    "decoupled contrastive with ema",
    "decoupled contrastive longer training",
    "decoupled contrastive with gradient cache",
    "cross-encoder mined hard negatives",
    "cross-encoder distill from teacher",
]


def test_empty_board_is_zeros():
    a = dedup_analysis(RunState(), skeleton_for("dense-retrieval"))
    assert a["n_hypotheses"] == 0 and a["top_cluster"] is None and a["false_merge_count"] == 0


def test_dcl_cluster_is_the_dominant_redundancy(tmp_path):
    st = _board(tmp_path, _DCL_BOARD)
    a = dedup_analysis(st, skeleton_for("dense-retrieval"))
    assert a["n_hypotheses"] == 7
    # the two DCL family concepts each cluster all 5 variants; deterministic tie-break picks the
    # lexicographically-smallest concept id as the top cluster.
    clusters = {c["concept"]: c["count"] for c in a["concept_clusters"]}
    assert clusters["loss/decoupled-contrastive"] == 5
    assert clusters["loss/contrastive"] == 5
    assert a["top_cluster"]["concept"] == "loss/contrastive"   # min-id tie-break, pinned
    assert a["top_cluster"]["count"] == 5
    assert a["redundancy_frac"] >= 0.7      # 5 of 7 tagged hypotheses touch the DCL family
    # the winning-region hypotheses are their own (small) clusters, not merged into DCL
    concepts = {c["concept"] for c in a["concept_clusters"]}
    assert "negatives/external-mining" in concepts
    assert "distillation/teacher-distill" in concepts


def test_untagged_hypotheses_are_tracked(tmp_path):
    st = _board(tmp_path, ["decoupled contrastive r-drop", "something with no matching alias whatsoever"])
    a = dedup_analysis(st, skeleton_for("dense-retrieval"))
    assert a["tagged"] == 1 and a["untagged"] == 1


def test_cross_branch_lookalikes_flagged_as_false_merge_risk(tmp_path):
    # two statements sharing most tokens but tagging DISJOINT concepts (temperature vs augmentation) —
    # a blind lexical/vector merge would collapse them; a taxonomy-aware merge must keep them distinct
    st = _board(tmp_path, ["tune the temperature parameter carefully for the run",
                           "tune the augmentation parameter carefully for the run"])
    a = dedup_analysis(st, skeleton_for("dense-retrieval"))
    assert a["false_merge_count"] >= 1
    risk = a["false_merge_risks"][0]
    assert "temperature" in risk["a"] + risk["b"] and "augmentation" in risk["a"] + risk["b"]


def test_no_false_merge_when_similar_items_share_a_concept(tmp_path):
    # same concept + similar text -> a legitimate merge candidate, NOT a false-merge risk
    st = _board(tmp_path, ["raise the contrastive temperature a bit",
                           "raise the contrastive temperature more"])
    a = dedup_analysis(st, skeleton_for("dense-retrieval"))
    assert a["false_merge_count"] == 0     # they share hyperparameter/temperature -> keep-together


def test_analysis_pins_cluster_order(tmp_path):
    # the concept_clusters list order is order-sensitive output; pin its head (an iteration-order leak
    # into the list would change this; the cross-seed subprocess guard lives in test_lock_in.py).
    st = _board(tmp_path, _DCL_BOARD)
    g = skeleton_for("dense-retrieval")
    a = dedup_analysis(st, g)
    assert [c["concept"] for c in a["concept_clusters"][:2]] == \
        ["loss/contrastive", "loss/decoupled-contrastive"]


def test_report_renders(tmp_path):
    st = _board(tmp_path, _DCL_BOARD)
    rep = dedup_report(st, skeleton_for("dense-retrieval"))
    assert "most-redundant concept" in rep and "loss/" in rep
