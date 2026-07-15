"""Axis-structured deep-research targeting (PART IV D2, §21.3/§21.12 — Phase 1e).

Locks in that targeting turns the coverage map into a RANKED set of axis-scoped research queries —
uncovered axes (with key regions) first, failed directions re-framed as 'research a DIFFERENT
implementation' (so the loop stops re-proposing the failed variant), then under-covered axes — grounded
optionally in the D1 brief. Pure/deterministic; runs no research itself."""
from __future__ import annotations

from looplab.core.models import RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import skeleton_for
from looplab.search.research_targeting import research_targets, targeting_report


def _dcl_run(tmp_path) -> RunState:
    """A DCL-only run (loss covered), a lightly-touched negatives node, and a FAILED false-neg node."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    for i, (th, rat) in enumerate([("dcl-0", "decoupled contrastive r-drop"),
                                   ("dcl-1", "decoupled contrastive temperature"),
                                   ("dcl-2", "decoupled contrastive ema")]):
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(i)},
                                           "theme": th, "rationale": rat}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.8 + i * 0.01})
    s.append("node_created", {"node_id": 3, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"seed": 9.0}, "theme": "fn",
                                       "rationale": "loss-side false negative filtering that broke training"}})
    s.append("node_failed", {"node_id": 3, "error": "nan", "reason": "crash"})
    return fold(s.read_all())


def test_empty_run_has_no_targets():
    r = research_targets(RunState(), skeleton_for("dense-retrieval"))
    # an empty run has no experiments, but every skeleton axis is uncovered -> targets for each
    assert isinstance(r["targets"], list)
    assert all(t["kind"] == "uncovered" for t in r["targets"])


def test_uncovered_axes_are_top_priority(tmp_path):
    st = _dcl_run(tmp_path)
    r = research_targets(st, skeleton_for("dense-retrieval"))
    assert "data" in r["uncovered"] and "distillation" in r["uncovered"]
    # the very first targets are uncovered axes carrying KEY winning-region concepts (priority 0)
    assert r["targets"][0]["kind"] == "uncovered"
    assert r["targets"][0]["priority"] == 0
    top_axes = {t["axis"] for t in r["targets"] if t["priority"] == 0}
    assert {"data", "distillation"} <= top_axes


def test_failed_direction_is_reframed_not_reproposed(tmp_path):
    st = _dcl_run(tmp_path)
    r = research_targets(st, skeleton_for("dense-retrieval"))
    fd_targets = [t for t in r["targets"] if t["kind"] == "failed-direction"]
    assert fd_targets
    assert "negatives/false-neg-handling" in fd_targets[0]["concepts"]
    assert "DIFFERENT implementation" in fd_targets[0]["query"]
    assert "negatives/false-neg-handling" in r["failed"]


def test_uncovered_ranks_before_failed_before_undercovered(tmp_path):
    st = _dcl_run(tmp_path)
    r = research_targets(st, skeleton_for("dense-retrieval"), max_targets=20)
    kinds = [t["kind"] for t in r["targets"]]
    # priority order: all uncovered, then failed-direction, then under-covered
    order = {"uncovered": 0, "failed-direction": 1, "under-covered": 2}
    assert kinds == sorted(kinds, key=lambda k: order[k])


def test_asset_brief_grounds_the_query(tmp_path):
    st = _dcl_run(tmp_path)
    r = research_targets(st, skeleton_for("dense-retrieval"),
                         asset_brief="hard-neg + NV-0.95 gave +0.04 here")
    assert any("NV-0.95" in t["query"] for t in r["targets"])


def test_targets_pin_axis_order(tmp_path):
    # the ranked target order is order-sensitive output; pin the head axes (uncovered key-region axes
    # first, alphabetical within a priority). The cross-seed subprocess guard lives in test_lock_in.py.
    st = _dcl_run(tmp_path)
    g = skeleton_for("dense-retrieval")
    r = research_targets(st, g)
    axes = [t["axis"] for t in r["targets"]]
    assert axes[:2] == ["data", "distillation"]      # the two uncovered key-region axes, priority 0


def test_report_renders(tmp_path):
    st = _dcl_run(tmp_path)
    rep = targeting_report(st, skeleton_for("dense-retrieval"))
    assert "research targets" in rep.lower() and "uncovered" in rep
