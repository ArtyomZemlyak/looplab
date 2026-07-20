"""Concept-graph diagnostic (PART IV D5 keystone, §21.11) — the offline coverage / uncovered-region
signal over a multi-label concept DAG.

These lock in the three validated behaviours (§21.10/§21.11): the heuristic tagger keys on primary-lever
LINEAGE (all `dcl-*` variants -> one family, so concentration reads the branch not the leaf); the pure
analytics are deterministic over (RunState, graph, tags); and the *uncovered winning-region* alarm fires
on the exact regions the `rubertlite` run never entered, from the first node — the decisive PART IV
signal. The analytics never write events or touch selection (Phase 0 = offline diagnostic)."""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.core.models import RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import (Concept, ConceptGraph, concept_coverage, concept_report,
                                          dense_retrieval_skeleton, skeleton_for, tag_nodes_heuristic,
                                          tag_nodes_llm, uncovered_regions)


def _store(tmp_path, nodes, direction="max") -> EventStore:
    """Build a run log from `nodes` = [(theme, rationale, metric), ...]; metric=None => failed node."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": direction})
    for i, (theme, rationale, metric) in enumerate(nodes):
        op = "draft" if i < 3 else "improve"
        # neutral `seed` param (matches no concept alias) so a node is tagged only by its theme/rationale
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": op,
                                  "idea": {"operator": op, "params": {"seed": float(i)},
                                           "theme": theme, "rationale": rationale}})
        if metric is None:
            s.append("node_failed", {"node_id": i, "error": "boom", "reason": "crash"})
        else:
            s.append("node_evaluated", {"node_id": i, "metric": metric})
    return s


# `rubertlite`-shaped run: every node is a DCL + R-Drop loss/regularization tweak; the winning region
# (external hard-neg mining, false-neg filtering, teacher distillation, synthetic data) is never entered.
_DCL_RUN = [
    ("dcl-rdrop-ema", "decoupled contrastive loss with r-drop and ema weight averaging", 0.80),
    ("dcl-rdrop-gc", "dcl with r-drop and gradient cache in-batch negatives", 0.81),
    ("dcl-temperature", "tune the contrastive temperature / logit scale for dcl", 0.82),
    ("dcl-rdrop-swa", "decoupled contrastive + r-drop + swa averaging", 0.83),
    ("dcl-listwise", "decoupled contrastive with a listwise kl ranking term", 0.835),
]

# A run that DOES reach the winning region (external mining + teacher distillation).
_BROAD_RUN = [
    ("dcl-baseline", "decoupled contrastive baseline", 0.80),
    ("hard-neg-mining", "offline hard negative mining with a cross-encoder to mine negatives", 0.86),
    ("teacher-distill", "distill from the cross-encoder teacher checkpoint (margin-mse)", 0.88),
    ("false-neg-filter", "apply nv-style false-negative filtering / denoise negatives", 0.87),
]


# --------------------------------------------------------------------------- #
# Graph model
# --------------------------------------------------------------------------- #

def test_skeleton_is_a_multiparent_dag():
    g = dense_retrieval_skeleton()
    # ema sits under BOTH regularization and training-schedule — the DAG expresses multi-membership a
    # single-parent tree cannot (the §21.11 upgrade).
    assert set(g.axes_of("regularization/ema")) == {"regularization", "training-schedule"}
    # every seeded axis is present even before any concept under it is touched
    for ax in ("data", "negatives", "loss", "distillation"):
        assert ax in g.axes()
    # the winning region is declared as key concepts
    assert "negatives/external-mining" in g.key_concepts()
    assert "distillation/teacher-distill" in g.key_concepts()


def test_skeleton_for_unknown_task_type_is_generic_and_empty():
    g = skeleton_for("some-new-task")
    assert g.concepts() == [] and g.axes() == [] and g.key_concepts() == []


def test_ensure_grows_without_clobbering_key():
    g = dense_retrieval_skeleton()
    before = g.get("negatives/external-mining")
    # a dynamically-grown duplicate must not downgrade the curated key concept
    g.ensure("negatives/external-mining")
    assert g.get("negatives/external-mining") is before and before.key
    # a genuinely new concept is added under its id-prefix axis
    g.ensure("loss/brand-new")
    assert "loss/brand-new" in g and g.axes_of("loss/brand-new") == ("loss",)


# --------------------------------------------------------------------------- #
# Heuristic tagging (lineage, not surface token)
# --------------------------------------------------------------------------- #

def test_heuristic_tagger_keys_on_lineage(tmp_path):
    st = fold(_store(tmp_path, _DCL_RUN).read_all())
    g = dense_retrieval_skeleton()
    tags = tag_nodes_heuristic(st, g)
    # all five dcl-* variants collapse onto the ONE decoupled-contrastive family (not five leaves)
    for nid in range(5):
        assert "loss/decoupled-contrastive" in tags[nid]
    assert {"loss/decoupled-contrastive"} <= set().union(*tags.values())


def test_heuristic_tagger_is_a_deterministic_bounded_display_projection(tmp_path):
    st = fold(_store(tmp_path, [("shared", "shared method", 0.5)]).read_all())
    graph = ConceptGraph([
        Concept(f"axis/c{i:03d}", aliases=("shared",), axes=("axis",)) for i in range(70)
    ])

    tags = tag_nodes_heuristic(st, graph)

    assert tags[0] == frozenset(f"axis/c{i:03d}" for i in range(64))


def test_untagged_nodes_are_tracked(tmp_path):
    st = fold(_store(tmp_path, [("mystery", "an approach with no matching alias here", 0.5)]).read_all())
    g = dense_retrieval_skeleton()
    cov = concept_coverage(st, g)
    assert cov["experiments"] == 1 and cov["untagged"] == 1 and cov["tagged"] == 0


def test_failed_nodes_still_count_as_experiments(tmp_path):
    nodes = [("dcl-rdrop", "decoupled contrastive with r-drop", None),   # failed
             ("dcl-temp", "dcl temperature", 0.8)]
    st = fold(_store(tmp_path, nodes).read_all())
    cov = concept_coverage(st, dense_retrieval_skeleton())
    assert cov["experiments"] == 2   # a failed experiment is still effort spent in the region


# --------------------------------------------------------------------------- #
# Coverage analytics + the uncovered-region alarm
# --------------------------------------------------------------------------- #

def test_empty_run_is_all_zeros_and_fully_uncovered():
    g = dense_retrieval_skeleton()
    cov = concept_coverage(RunState(), g)
    assert cov["experiments"] == 0 and cov["top_concept"] is None
    assert cov["dominant_clique"] is None
    # nothing touched -> every skeleton axis and every key region is uncovered
    assert set(cov["uncovered_axes"]) == set(g.axes())
    assert set(cov["uncovered_key"]) == set(g.key_concepts())
    alarm = uncovered_regions(RunState(), g)
    assert alarm["fired"] is True


def test_dcl_run_fires_the_winning_region_alarm(tmp_path):
    st = fold(_store(tmp_path, _DCL_RUN).read_all())
    g = dense_retrieval_skeleton()
    alarm = uncovered_regions(st, g)
    assert alarm["fired"] is True
    # the alarm names the EXACT regions the run never entered (§21.11 decisive signal)
    for cid in ("negatives/external-mining", "negatives/false-neg-handling",
                "distillation/teacher-distill"):
        assert cid in alarm["uncovered_key"]
    assert "0 coverage in {" in alarm["directive"]
    # ... and the concentration is legible: DCL is the dominant lineage, loss is the busy axis
    cov = concept_coverage(st, g)
    assert cov["top_concept"]["frac"] >= 0.5
    assert "loss" in cov["dominant_clique"]["axes"]
    # loss is the busy axis; the winning-region negatives concept was never entered (only the weak
    # in-batch variant may be — §21.11 notes those weak in-batch attempts, so we assert on the KEY concept)
    assert cov["axis_touch"]["loss"] == 5
    assert "negatives/external-mining" not in cov["first_touch"]


def test_reached_regions_drop_out_of_the_alarm(tmp_path):
    st = fold(_store(tmp_path, _BROAD_RUN).read_all())
    g = dense_retrieval_skeleton()
    cov = concept_coverage(st, g)
    # the key regions are now touched
    assert "negatives/external-mining" in cov["first_touch"]
    assert "distillation/teacher-distill" in cov["first_touch"]
    assert "negatives/external-mining" not in cov["uncovered_key"]
    alarm = uncovered_regions(st, g)
    # not every key region is covered (synthetic-queries still isn't), so with a curated key set the
    # alarm still fires — but it no longer names the mining/distill/false-neg regions the run reached.
    assert "negatives/external-mining" not in alarm["uncovered_key"]
    assert "distillation/teacher-distill" not in alarm["uncovered_key"]


def test_generic_skeleton_alarm_fires_on_untouched_axes(tmp_path):
    # the universality path: a graph with NO curated key concepts (a custom task type) still alarms —
    # it fires on entirely-untouched AXES rather than key concepts (the has_key=False branch).
    from looplab.search.concept_graph import Concept, ConceptGraph
    g = ConceptGraph([Concept("loss/x", axes=("loss",), aliases=("widget-loss",)),
                      Concept("data/y", axes=("data",), aliases=("gizmo-aug",))], task_type="custom")
    assert g.key_concepts() == []                       # no curated winning region
    st = fold(_store(tmp_path, [("t", "tune the widget-loss", 0.5)]).read_all())
    alarm = uncovered_regions(st, g)
    assert alarm["fired"] is True                       # data axis untouched -> alarm fires
    assert "data" in alarm["uncovered_axes"] and "loss" not in alarm["uncovered_axes"]
    # and once every axis is touched, it goes quiet
    st2 = fold(_store(tmp_path, [("t", "widget-loss", 0.5), ("u", "gizmo-aug data", 0.6)]).read_all())
    assert uncovered_regions(st2, g)["fired"] is False


def test_first_touch_records_earliest_experiment_index(tmp_path):
    st = fold(_store(tmp_path, _BROAD_RUN).read_all())
    cov = concept_coverage(st, dense_retrieval_skeleton())
    # external mining first appears at node index 1 (the 2nd experiment), distillation at index 2
    assert cov["first_touch"]["negatives/external-mining"] == 1
    assert cov["first_touch"]["distillation/teacher-distill"] == 2


def test_multiparent_concept_counts_toward_all_its_axes(tmp_path):
    # a single ema node touches BOTH regularization and training-schedule via the DAG multi-parent edge
    st = fold(_store(tmp_path, [("ema", "exponential moving average weight averaging", 0.7)]).read_all())
    cov = concept_coverage(st, dense_retrieval_skeleton())
    assert cov["axis_touch"].get("regularization") == 1
    assert cov["axis_touch"].get("training-schedule") == 1


def test_analytics_are_deterministic(tmp_path):
    st = fold(_store(tmp_path, _DCL_RUN).read_all())
    g = dense_retrieval_skeleton()
    assert concept_coverage(st, g) == concept_coverage(st, g)
    assert uncovered_regions(st, g) == uncovered_regions(st, g)


def test_top_concept_tie_break_is_deterministic(tmp_path):
    # one node touching several concepts that all tie at count 1: the winner must be the lexicographically
    # SMALLEST concept id, not whichever the (hash-seed-randomized) frozenset iteration yielded first.
    nodes = [("x", "r-drop and ema and dropout and temperature and mnr loss", 0.5)]
    st = fold(_store(tmp_path, nodes).read_all())
    cov = concept_coverage(st, dense_retrieval_skeleton())
    tied = sorted(cov["concept_touch"])   # all count 1
    assert cov["top_concept"]["id"] == tied[0]   # smallest id wins the tie, deterministically


def test_heuristic_tagger_respects_word_boundaries(tmp_path):
    # 'schema' must not fire the 'ema' alias; 'include' must not fire 'dcl' — raw-substring false positives
    st = fold(_store(tmp_path, [("x", "redesign the database schema and include indexes", 0.5)]).read_all())
    tags = tag_nodes_heuristic(st, dense_retrieval_skeleton())
    assert tags[0] == frozenset()


def test_report_renders_alarm(tmp_path):
    st = fold(_store(tmp_path, _DCL_RUN).read_all())
    rep = concept_report(st, dense_retrieval_skeleton())
    assert "UNCOVERED-REGION ALARM" in rep
    assert "negatives/external-mining" in rep


# --------------------------------------------------------------------------- #
# LLM tagger (optional richer path) — degrade-don't-block + growth
# --------------------------------------------------------------------------- #

class _TagClient:
    """Fake LLM tagger: returns a fixed concept-id set (tool_call only)."""
    def __init__(self, ids):
        self.ids = ids
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"concept_ids": self.ids}

    def complete_text(self, messages):
        return "not json"


class _BadClient:
    def complete_tool(self, messages, json_schema):
        raise RuntimeError("boom")

    def complete_text(self, messages):
        return "nope"


def test_llm_tagger_assigns_and_grows(tmp_path):
    st = fold(_store(tmp_path, [("x", "some run", 0.5)]).read_all())
    g = dense_retrieval_skeleton()
    client = _TagClient(["negatives/external-mining", "negatives/brand-new-family"])
    tags = tag_nodes_llm(st, g, client, grow=True)
    assert client.calls == 1
    assert "negatives/external-mining" in tags[0]
    # a proposed new id was grown into the graph under its axis
    assert "negatives/brand-new-family" in g
    assert "negatives/brand-new-family" in tags[0]


def test_incremental_tagging_only_calls_llm_for_new_nodes(tmp_path):
    """§21.16 Phase 2c EVAL: the headline win — with `known_tags` covering the old nodes, a repeated
    tagging pass pays ONLY for the new node's LLM call, not the whole history."""
    st = fold(_store(tmp_path, [("a", "run a", 0.5), ("b", "run b", 0.6), ("c", "run c", 0.7)]).read_all())
    g = dense_retrieval_skeleton()
    client = _TagClient(["negatives/external-mining"])
    # First pass: no known tags -> one LLM call per experiment node (3).
    tags1 = tag_nodes_llm(st, g, client, grow=True)
    assert client.calls == 3 and all("negatives/external-mining" in tags1[i] for i in (0, 1, 2))
    # Second pass: feed back nodes 0,1 as known -> ONLY node 2 is (re)tagged by the LLM.
    client.calls = 0
    known = {0: ["negatives/external-mining"], 1: ["negatives/external-mining"]}
    tags2 = tag_nodes_llm(st, g, client, grow=True, known_tags=known)
    assert client.calls == 1                         # <-- the incremental win: 1, not 3
    # reused nodes keep their tags and the concept stays in the graph
    assert "negatives/external-mining" in tags2[0] and "negatives/external-mining" in tags2[1]
    assert "negatives/external-mining" in tags2[2]   # the freshly-tagged node


def test_incremental_tagging_reuses_grown_ids_without_a_call(tmp_path):
    """A reused node whose recorded tag is a GROWN `axis/slug` id is re-materialized into the graph with
    NO LLM call (so a later cadence's coverage still sees it)."""
    st = fold(_store(tmp_path, [("z", "run z", 0.5)]).read_all())
    g = dense_retrieval_skeleton()
    client = _TagClient(["negatives/external-mining"])
    known = {0: ["negatives/brand-new-grown"]}       # a grown id not in the skeleton
    tags = tag_nodes_llm(st, g, client, grow=True, known_tags=known)
    assert client.calls == 0                          # fully reused, no LLM
    assert "negatives/brand-new-grown" in g           # re-ensured into the graph
    assert "negatives/brand-new-grown" in tags[0]


def test_node_concepts_event_round_trips_and_is_replay_safe(tmp_path):
    """§21.16 Phase 2c: node_concepts events fold into RunState.node_concepts (last-write-wins,
    order-tolerant); a log WITHOUT them folds to an empty dict (additive / byte-identical on old logs)."""
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 5, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 5, "metric": 0.8})
    # old-log shape first: no node_concepts -> empty dict
    assert fold(s.read_all()).node_concepts == {}
    # now record, then re-record (refinement) — last write wins
    s.append("node_concepts", {"node_id": 5, "concepts": ["loss/x"], "mode": "llm"})
    s.append("node_concepts", {"node_id": 5, "concepts": ["loss/x", "regularization/y"], "mode": "llm"})
    st = fold(s.read_all())
    assert st.node_concepts == {5: ["loss/x", "regularization/y"]}
    assert st.node_concept_provenance == {5: "classifier"}


@pytest.mark.parametrize(("mode_present", "mode", "expected"), [
    (False, None, "classifier"),       # genuine legacy cadence event
    (True, "llm", "classifier"),
    (True, "agentic", "classifier"),
    (True, "offline-heuristic", "offline-heuristic"),
    (True, None, "untrusted-source"),
    (True, "classifier-v-next", "untrusted-source"),
    (True, 7, "untrusted-source"),
])
def test_node_concept_source_provenance_is_explicit_and_future_safe(
        tmp_path, mode_present, mode, expected):
    from looplab.core.models import classifier_verified_node_concepts
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r"}})
    data = {"node_id": 0, "concepts": ["loss/x"], "at_vocab": 7}
    if mode_present:
        data["mode"] = mode
    s.append("node_concepts", data)

    state = fold(s.read_all())
    assert state.node_concepts == {0: ["loss/x"]}          # all producers remain displayable
    assert state.node_concept_provenance == {0: expected}
    if expected == "classifier":
        assert classifier_verified_node_concepts(state, 0) == ["loss/x"]
        assert state.node_concepts_at_vocab == {0: 7}
    else:
        assert classifier_verified_node_concepts(state, 0) == []
        assert state.node_concepts_at_vocab == {}


@pytest.mark.parametrize("concepts", [
    [f"axis/c{i:03d}" for i in range(65)],
    ["valid/x", "bad!"],
])
def test_partial_classifier_membership_is_display_only_not_verified_evidence(tmp_path, concepts):
    from looplab.core.models import classifier_verified_node_concepts
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r"}})
    s.append("node_concepts", {"node_id": 0, "concepts": concepts, "mode": "llm"})

    state = fold(s.read_all())
    assert state.node_concepts[0]                         # valid bounded subset stays displayable
    assert state.node_concept_materialization_receipts[0]["status"] == "partial"
    assert classifier_verified_node_concepts(state, 0) == []


@pytest.mark.parametrize("order", ["offline-first", "classifier-first"])
def test_classifier_receipt_dominates_offline_replay_in_either_order(tmp_path, order):
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r"}})
    offline = {"node_id": 0, "concepts": ["coarse/x"], "mode": "offline-heuristic"}
    classifier = {"node_id": 0, "concepts": ["reviewed/y"], "mode": "llm", "at_vocab": 8}
    for row in ((offline, classifier) if order == "offline-first" else (classifier, offline)):
        s.append("node_concepts", row)

    state = fold(s.read_all())
    assert state.node_concepts == {0: ["reviewed/y"]}
    assert state.node_concept_provenance == {0: "classifier"}
    assert state.node_concepts_at_vocab == {0: 8}


def test_offline_display_receipt_survives_same_idea_rebuild_until_classifier_upgrade(tmp_path):
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    created = {"node_id": 0, "parent_ids": [], "operator": "draft",
               "idea": {"operator": "draft", "params": {}, "rationale": "same",
                        "concepts": ["researcher/claim"]}}
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", created)
    s.append("node_concepts", {
        "node_id": 0, "concepts": ["offline/display"], "mode": "offline-heuristic",
        "generation": 0,
    })
    s.append("node_reset", {"node_id": 0, "from_stage": "implement", "generation": 0})
    s.append("node_created", {**created, "generation": 1})

    state = fold(s.read_all())
    assert state.node_concepts == {0: ["offline/display"]}
    assert state.node_concept_provenance == {0: "offline-heuristic"}
    # The next reviewed classifier event replaces the display-only receipt normally.
    s.append("node_concepts", {
        "node_id": 0, "concepts": ["classifier/reviewed"], "mode": "llm", "generation": 1,
    })
    upgraded = fold(s.read_all())
    assert upgraded.node_concepts == {0: ["classifier/reviewed"]}
    assert upgraded.node_concept_provenance == {0: "classifier"}


def test_node_concepts_invalidated_on_propose_rerun_only(tmp_path):
    """M1 (§21.18): tags staleify only when the IDEA changes. The snapshot tagger reads only the idea
    (tools=None), so `propose` (re-proposes a new idea) drops the cached tags, while `eval` (re-score) and
    `implement` (re-develop CODE, idea unchanged) KEEP them — scope tied to the tagger's inputs."""
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 3, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 3, "metric": 0.8})
    s.append("node_concepts", {"node_id": 3, "concepts": ["loss/x"], "mode": "llm"})
    assert fold(s.read_all()).node_concepts == {3: ["loss/x"]}
    assert fold(s.read_all()).node_concept_provenance == {3: "classifier"}
    # an EVAL re-score keeps the tags (idea+code unchanged)
    s.append("node_reset", {"node_id": 3, "from_stage": "eval"})
    assert fold(s.read_all()).node_concepts == {3: ["loss/x"]}
    # an IMPLEMENT re-develop keeps them too (CODE changes, idea unchanged; the idea-only tagger is stable)
    s.append("node_reset", {"node_id": 3, "from_stage": "implement"})
    assert fold(s.read_all()).node_concepts == {3: ["loss/x"]}
    # a PROPOSE re-propose invalidates them (the idea itself is re-generated)
    s.append("node_reset", {"node_id": 3, "from_stage": "propose"})
    reset_state = fold(s.read_all())
    assert reset_state.node_concepts == {}
    assert reset_state.node_concept_provenance == {}
    # ...and a fresh re-tag after the rerun repopulates
    s.append("node_concepts", {"node_id": 3, "concepts": ["negatives/y"], "mode": "llm",
                                "generation": 3})
    assert fold(s.read_all()).node_concepts == {3: ["negatives/y"]}


def test_late_classifier_event_cannot_cross_a_propose_generation(tmp_path):
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {},
                                       "rationale": "old", "concepts": ["author/old"]}})
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/old"], "generation": 0})
    s.append("node_reset", {"node_id": 0, "from_stage": "propose"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {},
                                       "rationale": "new", "concepts": ["author/new"]},
                              "generation": 1})
    # Both an explicitly old result and an indistinguishable legacy result are unsafe after reset.
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/late"], "generation": 0})
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/unstamped"]})
    st = fold(s.read_all())
    assert st.node_concepts == {0: ["author/new"]}
    assert st.node_concept_provenance == {0: "researcher-authored"}

    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/current"], "generation": 1})
    current = fold(s.read_all())
    assert current.node_concepts == {0: ["classifier/current"]}
    assert current.node_concept_provenance == {0: "classifier"}


@pytest.mark.parametrize("stage", ["eval", "implement"])
def test_legacy_unstamped_classifier_receipt_survives_same_idea_retry(tmp_path, stage):
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    created = {"node_id": 0, "parent_ids": [], "operator": "draft",
               "idea": {"operator": "draft", "params": {}, "rationale": "same idea"}}
    s.append("node_created", created)
    s.append("node_evaluated", {"node_id": 0, "metric": 0.7, "generation": 0})
    s.append("node_reset", {"node_id": 0, "from_stage": stage})
    if stage == "implement":
        s.append("node_created", {**created, "generation": 1})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8, "generation": 1})
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/current"],
                                "at_vocab": 11})

    st = fold(s.read_all())
    assert st.nodes[0].attempt == 1
    assert st.node_concepts == {0: ["classifier/current"]}
    assert st.node_concept_provenance == {0: "classifier"}
    assert st.node_concepts_at_vocab == {0: 11}


def test_legacy_unstamped_classifier_receipt_cannot_cross_subject_replacement(tmp_path):
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "old"}})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "new"},
                              "generation": 1})
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/ambiguous"]})

    st = fold(s.read_all())
    assert st.node_concepts == {}
    assert st.node_concept_provenance == {}


def test_concept_consolidation_event_accumulates_and_is_replay_safe(tmp_path):
    """B3 (§21.18): concept_consolidation events ACCUMULATE into RunState.concept_consolidation (order-
    tolerant, idempotent, malformed-safe); a log without them folds to {} (additive)."""
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    assert fold(s.read_all()).concept_consolidation == {}
    s.append("concept_consolidation", {"rename": {"aug/x": "data-aug/x"}})
    s.append("concept_consolidation", {"rename": {"opt/y": "optimization/y"}})   # accumulates
    s.append("concept_consolidation", {"rename": "notadict"})                    # malformed -> ignored
    st = fold(s.read_all())
    assert st.concept_consolidation == {"aug/x": "data-aug/x", "opt/y": "optimization/y"}


def test_consolidate_reuses_known_renames_authoritatively():
    """B3: a recorded rename is FIXED — never re-decided — even if the model would merge it elsewhere, so the
    vocabulary is stable across cadences."""
    from looplab.search.concept_graph import ConceptGraph, consolidate_concepts

    class _MergeAll:  # a client that would (wrongly) merge everything into 'z/z'
        def complete_tool(self, m, j):
            return {"merges": [{"raw": "aug/mixup", "canonical": "z/z"},
                               {"raw": "newaxis/newconcept", "canonical": "aug/mixup"}]}
        def complete_text(self, m): return "x"
    g = ConceptGraph(task_type="t")
    for cid in ("aug/mixup", "z/z", "newaxis/newconcept"):
        g.ensure(cid, axes=(cid.split("/", 1)[0],))
    tags = {0: frozenset({"aug/mixup"}), 1: frozenset({"newaxis/newconcept"})}
    # KNOWN says aug/mixup stays canonical (maps to itself is dropped as identity); feed a real fixed decision
    known = {"aug/mixup": "data-aug/mixup"}
    g.ensure("data-aug/mixup", axes=("data-aug",))
    _, t2, rn = consolidate_concepts(g, tags, client=_MergeAll(), known_renames=known)
    # the recorded aug/mixup -> data-aug/mixup is KEPT (NOT overridden to z/z); the new concept CAN be decided
    assert rn["aug/mixup"] == "data-aug/mixup"
    assert t2[0] == frozenset({"data-aug/mixup"})


def test_consolidate_freezes_known_canonicals_no_flap():
    """B3 defect-#1 regression: a known CANONICAL (a value in known_renames) must NOT be re-merged by the
    model, or `_final` would rewrite A->B into A->C — the cross-cadence flap B3 exists to stop."""
    from looplab.search.concept_graph import ConceptGraph, consolidate_concepts

    class _RemapCanonical:  # the model tries to re-canonicalize the KNOWN canonical data-aug/mixup
        def complete_tool(self, m, j):
            return {"merges": [{"raw": "data-aug/mixup", "canonical": "augment/mixup"}]}
        def complete_text(self, m): return "x"
    g = ConceptGraph(task_type="t")
    for cid in ("aug/mixup", "data-aug/mixup", "augment/mixup", "newthing/z"):
        g.ensure(cid, axes=(cid.split("/", 1)[0],))
    known = {"aug/mixup": "data-aug/mixup"}   # data-aug/mixup is the recorded CANONICAL
    # a NEW concept present so `undecided` is non-empty and the LLM step actually runs
    tags = {0: frozenset({"aug/mixup"}), 1: frozenset({"newthing/z"})}
    _, t2, rn = consolidate_concepts(g, tags, client=_RemapCanonical(), known_renames=known)
    assert "data-aug/mixup" not in rn                 # the canonical was NOT re-merged (frozen)
    assert rn.get("aug/mixup") == "data-aug/mixup"    # the original decision is intact
    assert t2[0] == frozenset({"data-aug/mixup"})     # node still on the stable canonical (no flap to augment/mixup)


def test_consolidate_skips_llm_when_nothing_undecided():
    """B3: when every concept is already covered by known_renames (raw or canonical), the LLM step is
    skipped — the incremental efficiency win + total stability."""
    from looplab.search.concept_graph import ConceptGraph, consolidate_concepts

    class _Boom:  # must NOT be called
        def complete_tool(self, m, j): raise AssertionError("LLM called though nothing was undecided")
        def complete_text(self, m): raise AssertionError("no")
    g = ConceptGraph(task_type="t")
    g.ensure("aug/mixup", axes=("aug",))
    g.ensure("data-aug/mixup", axes=("data-aug",))
    known = {"aug/mixup": "data-aug/mixup"}   # aug/mixup is raw, data-aug/mixup is canonical -> all covered
    _, _, rn = consolidate_concepts(g, {0: frozenset({"aug/mixup"})}, client=_Boom(), known_renames=known)
    assert rn == {"aug/mixup": "data-aug/mixup"}


def test_hypothesis_concepts_event_round_trips_and_is_replay_safe(tmp_path):
    """HT (§21.18): hypothesis_concepts folds into RunState.hypothesis_concepts (str-keyed, last-write-wins,
    malformed-safe); a log without them folds to {} (additive / byte-identical on old logs)."""
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    assert fold(s.read_all()).hypothesis_concepts == {}          # old-log shape
    s.append("hypothesis_concepts", {"hyp_id": "h1", "concepts": ["loss/x"], "mode": "llm"})
    s.append("hypothesis_concepts", {"hyp_id": "h1", "concepts": ["loss/x", "reg/y"], "mode": "llm"})  # re-derive
    s.append("hypothesis_concepts", {"concepts": ["z"]})         # no hyp_id -> ignored
    s.append("hypothesis_concepts", {"hyp_id": "h2", "concepts": "notalist"})  # bad concepts -> []
    st = fold(s.read_all())
    assert st.hypothesis_concepts == {"h1": ["loss/x", "reg/y"], "h2": []}


def test_hypothesis_concepts_at_vocab_folds_and_staleness_works_on_str_ids(tmp_path):
    """B1-ext (§21.18): hypothesis_concepts events carry at_vocab -> folds into
    hypothesis_concepts_at_vocab; the shared staleness helper works on STRING hypothesis ids too."""
    from looplab.events.eventstore import EventStore
    from looplab.search.concept_graph import stale_tagged_nodes
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("hypothesis_concepts", {"hyp_id": "h0", "concepts": ["loss/x"], "at_vocab": 3})
    s.append("hypothesis_concepts", {"hyp_id": "h1", "concepts": ["reg/y"], "at_vocab": 30})
    s.append("hypothesis_concepts", {"hyp_id": "h2", "concepts": ["z"]})   # pre-B1: no at_vocab
    st = fold(s.read_all())
    assert st.hypothesis_concepts_at_vocab == {"h0": 3, "h1": 30}
    # h0 tagged at vocab 3 << latest 30 -> stale; h1 (30) fresh; h2 (missing -> 0) stale
    stale = stale_tagged_nodes(["h0", "h1", "h2"], st.hypothesis_concepts_at_vocab, growth=0.7, cap=20)
    assert "h0" in stale and "h2" in stale and "h1" not in stale


def test_hypothesis_concepts_last_write_clears_stale_vocabulary_receipt(tmp_path):
    """A last-write-wins tag update must not inherit an older vocabulary-version receipt."""
    from looplab.events.eventstore import EventStore

    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("hypothesis_concepts", {"hyp_id": "h1", "concepts": ["loss/x"], "at_vocab": 30})
    s.append("hypothesis_concepts", {"hyp_id": "h1", "concepts": ["reg/y"]})

    st = fold(s.read_all())

    assert st.hypothesis_concepts == {"h1": ["reg/y"]}
    assert "h1" not in st.hypothesis_concepts_at_vocab


def test_tag_text_llm_shared_tagger(tmp_path):
    """The shared agentic single-text tagger: pins to KNOWN ids (grow=False), respects an empty verdict,
    recovers via tag_text on all-unknown / no client. `tag_idea_llm` now delegates to it."""
    from looplab.search.concept_graph import skeleton_for, tag_text, tag_text_llm

    class _C:
        def __init__(self, ids): self.ids = ids
        def complete_tool(self, m, j): return {"concept_ids": self.ids}
        def complete_text(self, m): return "x"
    g = skeleton_for("dense-retrieval")
    txt = "decoupled contrastive with r-drop"
    # known + unknown -> only known kept, graph not grown
    assert tag_text_llm(txt, g, _C(["loss/decoupled-contrastive", "made/up"])) == frozenset({"loss/decoupled-contrastive"})
    assert "made/up" not in g
    # empty verdict respected (even though tag_text WOULD tag it)
    assert tag_text(txt, g) and tag_text_llm(txt, g, _C([])) == frozenset()
    # all-unknown -> recover via heuristic; no client -> heuristic
    assert tag_text_llm(txt, g, _C(["only/unknown"])) == tag_text(txt, g)
    assert tag_text_llm(txt, g, None) == tag_text(txt, g)


def test_stale_tagged_nodes_selects_the_oldest_vocab_first():
    """B1 (§21.18): pick items tagged against < growth× the latest vocab, most-stale first, capped;
    a strict no-op until the vocabulary has grown."""
    from looplab.search.concept_graph import stale_tagged_nodes
    # latest vocab = 100; growth 0.7 -> threshold 70. Nodes at 10, 50 are stale; 80, 100 are fresh.
    at_vocab = {1: 10, 2: 50, 3: 80, 4: 100}
    assert stale_tagged_nodes([1, 2, 3, 4], at_vocab, growth=0.7, cap=20) == [1, 2]   # oldest-first
    # cap bounds it (most-stale wins)
    assert stale_tagged_nodes([1, 2, 3, 4], at_vocab, growth=0.7, cap=1) == [1]
    # a node with NO recorded at_vocab is treated as 0 (oldest) -> stale
    assert 9 in stale_tagged_nodes([9, 4], {4: 100}, growth=0.7, cap=20)
    # no growth yet (all equal / empty) -> nothing stale
    assert stale_tagged_nodes([1, 2], {1: 5, 2: 5}, growth=0.7) == []
    assert stale_tagged_nodes([1, 2], {}, growth=0.7) == []


def test_node_concepts_at_vocab_folds_and_syncs_with_rerun(tmp_path):
    """B1: the at_vocab of each node_concepts event folds into node_concepts_at_vocab; a propose rerun
    invalidates it alongside the tags (M1 sync); a pre-B1 event without at_vocab leaves it unset."""
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "improve",
                              "idea": {"operator": "improve", "params": {}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 2, "metric": 0.8})
    s.append("node_concepts", {"node_id": 2, "concepts": ["loss/x"], "at_vocab": 12})
    st = fold(s.read_all())
    assert st.node_concepts_at_vocab == {2: 12} and st.node_concepts == {2: ["loss/x"]}
    # A later legacy classifier result without a vocabulary receipt replaces, rather than inherits, it.
    s.append("node_concepts", {"node_id": 2, "concepts": ["loss/y"]})
    assert fold(s.read_all()).node_concepts_at_vocab == {}
    # a pre-B1 event (no at_vocab) doesn't set the map
    s.append("node_concepts", {"node_id": 5, "concepts": ["reg/y"]})
    assert 5 not in fold(s.read_all()).node_concepts_at_vocab
    # a propose rerun clears BOTH the tags and the at_vocab for node 2
    s.append("node_reset", {"node_id": 2, "from_stage": "propose"})
    st2 = fold(s.read_all())
    assert 2 not in st2.node_concepts and 2 not in st2.node_concepts_at_vocab


def test_node_concepts_event_ignores_malformed_payloads(tmp_path):
    from looplab.events.eventstore import EventStore
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_concepts", {"concepts": ["a"]})               # no node_id -> ignored
    s.append("node_concepts", {"node_id": 9, "concepts": "notalist"})   # unknown node -> ignored
    st = fold(s.read_all())
    assert st.node_concepts == {}
    assert st.node_concept_provenance == {}


def test_build_concept_map_exposes_raw_tags_for_recording():
    """`build_concept_map` returns `raw_tags` (pre-consolidation) so the engine can record them as
    node_concepts events; offline fallback exposes them too."""
    import tempfile
    d = tempfile.mkdtemp()
    st = fold(_store(Path(d), [("dcl", "decoupled contrastive loss", 0.5)]).read_all())
    from looplab.search.concept_graph import build_concept_map
    m = build_concept_map(st, client=None, seed_graph=dense_retrieval_skeleton())
    assert "raw_tags" in m and isinstance(m["raw_tags"], dict)
    assert m["raw_tag_modes"] == {0: "offline-heuristic"}


def test_llm_tagger_degrades_to_heuristic_on_failure(tmp_path):
    # a node whose text DOES match a heuristic alias: on LLM failure it must fall back to that tag
    st = fold(_store(tmp_path, [("dcl", "decoupled contrastive loss run", 0.5)]).read_all())
    g = dense_retrieval_skeleton()
    modes = {}
    tags = tag_nodes_llm(st, g, _BadClient(), producer_modes=modes)
    assert "loss/decoupled-contrastive" in tags[0]   # heuristic fallback, harness never crashed
    assert modes == {0: "offline-heuristic"}


def test_llm_tagger_preserves_successful_and_reused_empty_results(tmp_path):
    st = fold(_store(tmp_path, [("dcl", "decoupled contrastive loss run", 0.5)]).read_all())

    fresh_modes = {}
    fresh = tag_nodes_llm(
        st, dense_retrieval_skeleton(), _TagClient([]), producer_modes=fresh_modes)
    assert fresh == {0: frozenset()}
    assert fresh_modes == {0: "llm"}

    reused_client = _TagClient(["loss/decoupled-contrastive"])
    reused_modes = {}
    reused = tag_nodes_llm(
        st,
        dense_retrieval_skeleton(),
        reused_client,
        known_tags={0: []},
        producer_modes=reused_modes,
    )
    assert reused == {0: frozenset()}
    assert reused_client.calls == 0
    assert reused_modes == {}


def test_llm_tagger_rejects_mixed_invalid_output_as_untrusted_fallback(tmp_path):
    st = fold(_store(tmp_path, [("dcl", "decoupled contrastive loss run", 0.5)]).read_all())
    modes = {}
    tags = tag_nodes_llm(
        st,
        dense_retrieval_skeleton(),
        _TagClient(["loss/decoupled-contrastive", "bad!"]),
        producer_modes=modes,
    )
    assert "loss/decoupled-contrastive" in tags[0]
    assert "bad!" not in tags[0]
    assert modes == {0: "offline-heuristic"}


def test_llm_tagger_rejects_duplicate_overflow_as_untrusted_fallback(tmp_path):
    st = fold(_store(tmp_path, [("dcl", "decoupled contrastive loss run", 0.5)]).read_all())
    modes = {}

    tags = tag_nodes_llm(
        st,
        dense_retrieval_skeleton(),
        _TagClient(["loss/decoupled-contrastive"] * 65),
        producer_modes=modes,
    )

    assert "loss/decoupled-contrastive" in tags[0]
    assert modes == {0: "offline-heuristic"}


ROOT = Path(__file__).resolve().parents[1]  # sanity: importable package layout
assert ROOT.exists()


# --------------------------------------------------------------------------- #
# Universal per-task importance derivation (§21.13) — no hardcoded key list
# --------------------------------------------------------------------------- #

def test_derive_reference_concepts_degrades_without_client():
    # No LLM reachable -> best-effort empty, never raises (keeps the diagnostic alive).
    from looplab.search.concept_graph import derive_reference_concepts
    assert derive_reference_concepts("some task", {"concept_touch": {"loss/x": 1}}, client=None) == []


def test_derive_reference_concepts_filters_explored(monkeypatch):
    # The derivation must DROP anything already explored and normalize ids — universal, no domain pack.
    import looplab.core.parse as parse_mod
    from looplab.search import concept_graph as cg

    class _It:
        def __init__(self, cid, why=""):
            self.concept_id, self.why = cid, why

    class _Out:
        missing = [_It("Data/Synthetic-Queries", "generate queries"),
                   _It("loss/decoupled-contrastive", "already tried")]  # explored -> dropped

    monkeypatch.setattr(parse_mod, "parse_structured", lambda *a, **k: _Out())
    out = cg.derive_reference_concepts(
        "dense retrieval", {"concept_touch": {"loss/decoupled-contrastive": 5}},
        client=object())
    ids = [m["concept_id"] for m in out]
    assert ids == ["data/synthetic-queries"]        # normalized + explored filtered out
    assert out[0]["why"] == "generate queries"


def test_build_concept_map_offline_fallback(tmp_path):
    # No client -> deterministic heuristic build over the seed pack; returns the full map shape, no crash.
    from looplab.events.replay import fold
    from looplab.search.concept_graph import build_concept_map, dense_retrieval_skeleton
    st = fold(_store(tmp_path, [("dcl-rdrop", "decoupled contrastive with r-drop", 0.80),
                                ("dcl-rdrop-ema", "dcl r-drop ema averaging", 0.81),
                                ("temperature", "tune the contrastive temperature", 0.82)]).read_all())
    out = build_concept_map(st, "dense retrieval", client=None, seed_graph=dense_retrieval_skeleton())
    assert out["mode"] == "offline-heuristic"
    assert set(out) == {
        "graph", "tags", "raw_tags", "raw_tag_modes", "coverage", "important_uncovered", "mode"}
    assert out["important_uncovered"] == []          # no importance derivation without a client
    assert out["coverage"]["experiments"] == 3


# --------------------------------------------------------------------------- #
# Vocabulary consolidation (§21.11 follow-up) — keep a grown graph from fragmenting
# --------------------------------------------------------------------------- #

def test_consolidate_applies_llm_rename(monkeypatch):
    import looplab.core.parse as parse_mod
    from looplab.search import concept_graph as cg
    g = cg.ConceptGraph([cg.Concept("augmentation/mixup", "mixup", ("augmentation",)),
                         cg.Concept("data-augmentation/cutmix", "cutmix", ("data-augmentation",))])
    tags = {0: frozenset({"augmentation/mixup"}), 1: frozenset({"data-augmentation/cutmix"})}

    class _P:
        def __init__(s, r, c): s.raw, s.canonical = r, c
    class _Out:
        merges = [_P("data-augmentation/cutmix", "augmentation/cutmix")]
    monkeypatch.setattr(parse_mod, "parse_structured", lambda *a, **k: _Out())

    g2, t2, rename = cg.consolidate_concepts(g, tags, client=object())
    assert rename == {"data-augmentation/cutmix": "augmentation/cutmix"}
    assert "augmentation/cutmix" in g2 and "data-augmentation/cutmix" not in g2
    assert g2.axes() == ["augmentation"]                     # the fragmented axis is gone
    assert t2[1] == frozenset({"augmentation/cutmix"})       # tag rewritten to canonical


def test_consolidate_resolves_transitive_chain(monkeypatch):
    import looplab.core.parse as parse_mod
    from looplab.search import concept_graph as cg
    g = cg.ConceptGraph([cg.Concept("a/x"), cg.Concept("b/x"), cg.Concept("c/x")])
    tags = {0: frozenset({"a/x"})}
    class _P:
        def __init__(s, r, c): s.raw, s.canonical = r, c
    class _Out:
        merges = [_P("a/x", "b/x"), _P("b/x", "c/x")]        # a->b->c
    monkeypatch.setattr(parse_mod, "parse_structured", lambda *a, **k: _Out())
    _, t2, rename = cg.consolidate_concepts(g, tags, client=object())
    assert rename["a/x"] == "c/x"                            # collapsed transitively
    assert t2[0] == frozenset({"c/x"})


def test_consolidate_no_client_never_crashes():
    from looplab.search import concept_graph as cg
    g = cg.ConceptGraph([cg.Concept("a/x"), cg.Concept("a/y")])
    g2, t2, rename = cg.consolidate_concepts(g, {0: frozenset({"a/x"})}, client=None)
    assert isinstance(rename, dict) and 0 in t2         # fallback ran, no crash


def test_consolidation_preserves_aliases_for_heuristic_tagging():
    # Regression: rebuilding concepts during consolidation must NOT erase aliases, or the heuristic
    # tagger goes blind on the consolidated graph (and a merged concept must inherit the synonym's alias).
    from looplab.search.concept_graph import (Concept, ConceptGraph, _apply_consolidation, tag_text)
    g = ConceptGraph([Concept("loss/a", "A", ("loss",), ("alpha-loss",)),
                      Concept("loss/b", "B", ("loss",), ("beta-loss",))])
    g2, _ = _apply_consolidation(g, {}, {"loss/b": "loss/a"})   # merge b -> a
    a = g2.get("loss/a")
    assert set(a.aliases) == {"alpha-loss", "beta-loss"}         # own + merged-away synonym's aliases
    assert "loss/a" in tag_text("using a beta-loss run", g2)     # heuristic still tags on consolidated graph


def test_arbitrary_depth_hierarchy_and_crosslinks():
    """Multi-level concept ids (`loss/contrastive/dcl/dclx`) materialize the whole ancestor chain, each
    level linked to its immediate prefix, with working up/down traversal — while top-level `axes()` and
    coverage roll-up stay depth-independent and cross-axis DAG membership is preserved (§21.6 unbounded)."""
    from looplab.search.concept_graph import ConceptGraph

    g = ConceptGraph(task_type="dense-retrieval")
    g.ensure("loss/contrastive/dcl/dclx")

    # every intermediate level exists, linked to its immediate prefix
    for level in ("loss", "loss/contrastive", "loss/contrastive/dcl", "loss/contrastive/dcl/dclx"):
        assert level in g
    assert g.parents_of("loss/contrastive/dcl/dclx") == ("loss/contrastive/dcl",)
    assert g.ancestors_of("loss/contrastive/dcl/dclx") == ["loss/contrastive/dcl", "loss/contrastive", "loss"]
    assert g.children_of("loss/contrastive") == ["loss/contrastive/dcl"]
    assert "loss/contrastive/dcl/dclx" in g.descendants_of("loss")
    assert g.depth_of("loss/contrastive/dcl/dclx") == 3 and g.depth_of("loss") == 0

    # depth-independent roll-up: any-depth concept still rolls up to its top-level axis
    assert g.axes() == ["loss"]
    assert g.axes_of("loss/contrastive/dcl/dclx") == ("loss",)

    # cross-axis DAG membership (a concept under TWO roots) still works and is reflected in axes()
    g.ensure("loss/margin-mse", axes=("loss", "distillation"))
    assert g.parents_of("loss/margin-mse") == ("loss", "distillation")
    assert g.axes_of("loss/margin-mse") == ("distillation", "loss")
    assert "distillation" in g.axes()
