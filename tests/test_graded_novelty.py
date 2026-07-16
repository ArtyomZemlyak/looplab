"""Graded novelty + failed-direction re-examination (PART IV D3, §21.4/§21.10/§21.12 — Phase 1c).

Locks in the §21.4 levels: the classifier uses the concept graph to tell 'this DCL tweak' (near-dup /
same-impl) from 'the whole DCL branch' (same-direction-new-impl -> ALLOW), recognizes a proposal that
RE-OPENS a wrongly-abandoned failed direction (-> reexamine, not reject), and the grounded+repeated
verifier decides implementation-bound vs direction-bound. Advisory — does not touch the live gate."""
from __future__ import annotations

from looplab.core.models import Idea, RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import skeleton_for
from looplab.search.graded_novelty import (failed_directions, grade_novelty,
                                           reexamine_failed_direction, tag_idea)


def _run(tmp_path) -> "RunState":
    """node 0: a DCL loss win; node 1: a FAILED loss-side false-negative direction."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"temperature": 0.05},
                                       "theme": "dcl", "rationale": "decoupled contrastive loss with r-drop"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.85})
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"seed": 1.0}, "theme": "fn",
                                       "rationale": "loss-side false negative filtering that broke training"}})
    s.append("node_failed", {"node_id": 1, "error": "nan", "reason": "crash"})
    return fold(s.read_all())


def test_identical_params_reject(tmp_path):
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"temperature": 0.05},
                               rationale="decoupled contrastive r-drop"), skeleton_for("dense-retrieval"))
    assert g.level == 1 and g.name == "identical" and g.recommendation == "reject" and g.near_node == 0


def test_near_duplicate_in_run_reproposes(tmp_path):
    # same FULL concept set as node 0 (DCL) AND params within 15% -> near-duplicate -> repropose (level 2)
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"temperature": 0.055},
                               rationale="decoupled contrastive loss with r-drop"),
                      skeleton_for("dense-retrieval"))
    assert g.level == 2 and g.name == "near_duplicate_in_run" and g.recommendation == "repropose"
    assert g.near_node == 0


def test_same_direction_new_impl_allows(tmp_path):
    # same DCL/loss branch as node 0, but a materially different implementation -> ALLOW (level 4)
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"temperature": 0.5},
                               rationale="decoupled contrastive with a listwise KL term"),
                      skeleton_for("dense-retrieval"))
    assert g.level == 4 and g.name == "same_direction_new_impl" and g.recommendation == "allow"


def test_reopen_wrongly_abandoned_direction(tmp_path):
    # a data-side false-negative filter re-opens the direction node 1 killed with a loss-side hack
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"seed": 9.0},
                               rationale="data-side false negative filtering (nv-0.95)"),
                      skeleton_for("dense-retrieval"))
    assert g.level == 5 and g.name == "wrongly_abandoned" and g.recommendation == "reexamine"
    assert "negatives/false-neg-handling" in g.shared_concepts


def test_partial_overlap_close_params_is_not_novel(tmp_path):
    # a proposal sharing SOME concept with a tried node (DCL) but adding a NEW concept, with close params,
    # must be 'same_direction_new_impl' (it shares the DCL branch) — NOT mislabeled 'novel'
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"temperature": 0.055},
                               rationale="decoupled contrastive with data augmentation"),
                      skeleton_for("dense-retrieval"))
    assert g.name != "novel"
    assert g.level == 4 and "loss/decoupled-contrastive" in g.shared_concepts


def test_novel_region_allows(tmp_path):
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"seed": 3.0},
                               rationale="synthetic query generation via doc2query"),
                      skeleton_for("dense-retrieval"))
    assert g.name == "novel" and g.recommendation == "allow"


def test_tried_across_runs_surfaces(tmp_path):
    st = _run(tmp_path)
    g = grade_novelty(st, Idea(operator="improve", params={"seed": 7.0},
                               rationale="cross-encoder mined hard negatives"),
                      skeleton_for("dense-retrieval"),
                      prior_concepts={"negatives/external-mining"})
    assert g.level == 3 and g.name == "tried_across_runs" and g.recommendation == "surface_prior"


def test_failed_directions_identifies_the_failed_concept(tmp_path):
    st = _run(tmp_path)
    fds = failed_directions(st, skeleton_for("dense-retrieval"))
    concepts = {fd.concept for fd in fds}
    assert "negatives/false-neg-handling" in concepts
    # the DCL/loss direction WON (node 0), so it is not a failed direction
    assert "loss/decoupled-contrastive" not in concepts


class _Stub:
    def complete_tool(self, m, s):
        return {"verdicts": ["strong_yes", "yes"], "rationales": ["impl bug", "sound"]}

    def complete_text(self, m):
        return "x"


def test_reexamine_is_grounded_and_repeated(tmp_path):
    st = _run(tmp_path)
    res = reexamine_failed_direction(st, 1, skeleton_for("dense-retrieval"), client=_Stub(),
                                     asset_brief="hard-neg + NV-0.95 gave +0.04 here", samples=3)
    assert res["available"] is True
    assert res["implementation_bound"] == 1.0 and res["reexamine"] == 0.75
    assert res["recommendation"] == "reexamine"
    assert "negatives/false-neg-handling" in res["concepts"]


def test_reexamine_degrades_without_client(tmp_path):
    st = _run(tmp_path)
    res = reexamine_failed_direction(st, 1, skeleton_for("dense-retrieval"), client=None)
    assert res["available"] is False and res["recommendation"] == "unavailable"


def test_tag_idea_pins_the_concept_set(tmp_path):
    # tag_idea returns a frozenset (membership, not order), so pin the exact set contents
    g = skeleton_for("dense-retrieval")
    idea = Idea(operator="improve", params={}, rationale="decoupled contrastive with r-drop")
    assert tag_idea(idea, g) == frozenset({"loss/decoupled-contrastive", "loss/contrastive",
                                           "regularization/r-drop"})


# --------------------------------------------------------------------------- #
# F2 (§21.4): agentic graded-novelty — reuse cached LLM node tags + LLM-tag the idea
# --------------------------------------------------------------------------- #

def test_graph_from_node_concepts_rebuilds_deterministically():
    """The cached LLM tags reconstruct into a graph + tags with NO LLM (Feature-1 reuse)."""
    from looplab.search.concept_graph import graph_from_node_concepts
    g, tags = graph_from_node_concepts({0: ["loss/decoupled-contrastive"], 1: ["negatives/external-mining"]})
    # `ensure()` materializes the full ANCESTOR CHAIN (arbitrary-depth concepts, 3ca45bf), so each leaf
    # id also registers its intermediate id-prefix roots (`loss`, `negatives`) as first-class concepts.
    assert sorted(c.id for c in g.concepts()) == [
        "loss", "loss/decoupled-contrastive", "negatives", "negatives/external-mining"]
    # The per-node TAG assignment stays the exact named leaf — intermediates are graph structure, not tags.
    assert tags == {0: frozenset({"loss/decoupled-contrastive"}), 1: frozenset({"negatives/external-mining"})}
    # a grown id's axis comes from its prefix; top-level ids are valid roots and only empty ids are dropped
    g2, t2 = graph_from_node_concepts({5: ["", "badnoslash", "axis/ok"]})
    assert t2 == {5: frozenset({"badnoslash", "axis/ok"})}
    assert "badnoslash" in g2 and "axis/ok" in g2


class _IdeaTagClient:
    """Fake LLM idea-tagger returning a fixed id set (tool_call)."""
    def __init__(self, ids):
        self.ids = ids
        self.calls = 0
    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"concept_ids": self.ids}
    def complete_text(self, messages): return "x"


def test_tag_idea_llm_pins_to_known_ids_and_never_grows():
    from looplab.search.concept_graph import skeleton_for
    from looplab.search.graded_novelty import tag_idea_llm
    g = skeleton_for("dense-retrieval")
    idea = Idea(operator="improve", params={}, rationale="some hard negative mining")
    # the LLM names a KNOWN id + an UNKNOWN one -> only the known survives, graph is NOT grown
    c = _IdeaTagClient(["negatives/external-mining", "totally/new-unknown"])
    tags = tag_idea_llm(idea, g, c)
    assert c.calls == 1
    assert tags == frozenset({"negatives/external-mining"})
    assert "totally/new-unknown" not in g            # a proposal must not mint vocabulary


def test_tag_idea_llm_falls_back_to_heuristic():
    from looplab.search.concept_graph import skeleton_for
    from looplab.search.graded_novelty import tag_idea, tag_idea_llm
    g = skeleton_for("dense-retrieval")
    idea = Idea(operator="improve", params={}, rationale="decoupled contrastive with r-drop")
    # no client -> heuristic tag_idea
    assert tag_idea_llm(idea, g, None) == tag_idea(idea, g)
    # LLM NAMED ids but none are known -> fall back to the alias tagger rather than empty
    assert tag_idea_llm(idea, g, _IdeaTagClient(["nope/unknown"])) == tag_idea(idea, g)


def test_tag_idea_llm_respects_an_empty_novel_verdict():
    """When the LLM names NOTHING (concept_ids=[]) it is deliberately saying 'fits no known concept ->
    novel'; respect the empty set (NOT the alias heuristic, which could fire a spurious partial-word match
    and wrongly force an overlap)."""
    from looplab.search.concept_graph import skeleton_for
    from looplab.search.graded_novelty import tag_idea, tag_idea_llm
    g = skeleton_for("dense-retrieval")
    # a rationale whose text WOULD trip a heuristic alias, but the LLM (correctly) returns [] -> stay empty
    idea = Idea(operator="improve", params={}, rationale="decoupled contrastive with r-drop")
    assert tag_idea(idea, g)                                    # heuristic WOULD tag it
    assert tag_idea_llm(idea, g, _IdeaTagClient([])) == frozenset()   # empty verdict respected
