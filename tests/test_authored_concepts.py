"""Researcher-authored concepts remain visible but carry explicit, replay-stable provenance.

A later `node_concepts` classifier event refines them last-write-wins; admission consumers can therefore
distinguish a proposal claim from independent classifier evidence without migrating old event logs.
"""
from types import SimpleNamespace

import pytest

from looplab.core.models import Idea, IdeaEmission, Node, durable_idea_payload
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    return s


def _created(node_id, concepts=None):
    idea = {"operator": "draft", "params": {"seed": float(node_id)}, "rationale": "r"}
    if concepts is not None:
        idea["concepts"] = concepts
    return {"node_id": node_id, "parent_ids": [], "operator": "draft", "idea": idea}


def test_consolidation_conflict_resolves_order_independently(tmp_path):
    # Invariant 5 (order-tolerance): a CONFLICTING re-map of the same raw id must fold to the same result
    # regardless of event order — a deterministic winner (lexicographically smallest canonical), never
    # last-write. (The real producer never conflicts; this hardens adversarial / spliced logs.)
    def _fold(order):
        s = _store(tmp_path / order)
        for canon in order:
            s.append("concept_consolidation", {"rename": {"x": canon}})
        return fold(s.read_all()).concept_consolidation
    (tmp_path / "ab").mkdir()
    (tmp_path / "ba").mkdir()
    assert _fold("ab") == {"x": "a"}
    assert _fold("ba") == {"x": "a"}                      # reversed order -> identical


def test_at_vocab_rejects_bool(tmp_path):
    # bool is an int subclass; `at_vocab: true` must NOT be stored as a vocabulary size of 1.
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["a/x"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/x"], "at_vocab": True})
    assert fold(s.read_all()).node_concepts_at_vocab == {}   # bool rejected, no receipt


def test_valid_concept_id_charset_gate():
    from looplab.core.models import valid_concept_id
    for ok in ["loss/decoupled-contrastive", "hyperparameter/learning-rate", "данные/размер",
               "architecture/resnet50", "loss/r-drop", "a/b_c.d", "loss/x y"]:  # space→dash normalizes
        assert valid_concept_id(ok), ok
    for bad in ["a/b#c==", "loss/💥", "<script>", "a/..", "", "a//b", "   ", 7, None,
                "B3czR8YJ74OGBOyfVzhZ#Ea5og4_Pq3dkVsLy9ooaIRjQffav"]:
        assert not valid_concept_id(bad), repr(bad)


def test_idea_drops_malformed_authored_concepts():
    idea = Idea(operator="draft", params={}, rationale="r",
                concepts=["loss/good", "arch/moe", "loss/💥", "junk#base64=="])
    assert idea.concepts == ["loss/good", "arch/moe"]      # garbage dropped, order preserved


def test_idea_applies_the_same_charset_gate_to_delta_operands():
    idea = Idea(
        operator="draft", concept_mode="delta",
        concepts_added=["loss/good", "junk#base64=="],
        concepts_removed=["model/old", "loss/💥"],
    )
    assert idea.concepts_added == ["loss/good"]
    assert idea.concepts_removed == ["model/old"]


def test_authored_garbage_concept_dropped_at_fold(tmp_path):
    # The Idea field-validator runs at fold too (Idea rebuilt via Idea(**d)), so authored garbage never
    # reaches node_concepts / the /concepts tree even in an already-written log.
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/keep", "junk#base64==", "loss/💥"]))
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/keep"]
    assert st.node_concept_provenance[0] == "researcher-authored"


def test_idea_concepts_round_trip():
    idea = Idea(operator="draft", params={"seed": 1.0}, rationale="try dcl",
                concepts=["loss/contrastive/dcl", "regularization/r-drop"])
    d = idea.model_dump()
    assert d["concepts"] == ["loss/contrastive/dcl", "regularization/r-drop"]
    assert Idea(**d).concepts == idea.concepts            # rides on the Idea through the event log
    assert Idea(operator="draft", params={}, rationale="r").concepts == []   # default empty


def test_strict_emission_requires_consistent_bounded_canonical_mode():
    schema = IdeaEmission.model_json_schema()
    assert "concept_mode" in schema["required"]
    assert schema["properties"]["concepts"]["maxItems"] == 64
    valid = IdeaEmission.model_validate({
        "operator": "draft", "concept_mode": "delta", "concepts_added": ["model/new"]})
    assert valid.to_idea().concept_mode == "delta"
    invalid = [
        {"operator": "draft"},
        {"operator": "draft", "concept_mode": "future"},
        {"operator": "draft", "concept_mode": "full", "concepts_added": ["model/new"]},
        {"operator": "draft", "concept_mode": "delta", "concepts": ["model/full"]},
        {"operator": "draft", "concept_mode": "full", "concepts": ["bad!"]},
        {"operator": "draft", "concept_mode": "delta",
         "concepts_added": ["Model/A"], "concepts_removed": ["model/a"]},
        {"operator": "draft", "concept_mode": "full",
         "concepts": ["Model/A", "model/a"]},
        {"operator": "draft", "concept_mode": "full",
         "concepts": [f"axis/c{i}" for i in range(65)]},
    ]
    for payload in invalid:
        with pytest.raises(ValueError):
            IdeaEmission.model_validate(payload)


def test_tolerant_reader_keeps_node_shape_bounded_and_omits_absent_mode_nested():
    idea = Idea.model_validate({
        "operator": "draft",
        "concepts": [f"axis/c{i:03d}" for i in range(100)] + ["bad!", 7],
        "concepts_added": "not-a-list",
    })
    assert len(idea.concepts) == 64
    assert idea.concepts_added == []
    durable = durable_idea_payload(idea)
    assert "concept_mode" not in durable
    assert "concepts_removed" not in durable
    assert "concepts" in durable and "concepts_added" in durable
    node_dump = Node(id=0, operator="draft", idea=idea).model_dump(mode="json")
    assert "concept_mode" not in node_dump["idea"]


def test_authored_concepts_fold_into_node_concepts(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "architecture/moe"]))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/dcl", "architecture/moe"]
    assert st.node_concept_provenance[0] == "researcher-authored"


def test_no_concepts_leaves_node_concepts_absent(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, None))           # Researcher emitted no concepts
    st = fold(s.read_all())
    assert 0 not in st.node_concepts                       # never write an empty membership set
    assert 0 not in st.node_concept_provenance


def test_cadence_node_concepts_override_authored(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl"]))
    # a later tagging cadence refines the node's tags against a grown vocabulary — last write wins
    s.append("node_concepts", {"node_id": 0,
                               "concepts": ["loss/contrastive/dcl", "hyperparameter/temperature"],
                               "at_vocab": 12})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/contrastive/dcl", "hyperparameter/temperature"]
    assert st.node_concept_provenance[0] == "classifier"


@pytest.mark.parametrize("stage", ["eval", "implement"])
def test_non_propose_rebuild_preserves_classifier_receipt(tmp_path, stage):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["researcher/claim"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/finding"],
                                "at_vocab": 12})
    s.append("node_reset", {"node_id": 0, "from_stage": stage})
    rebuilt = _created(0, ["researcher/repeated-claim"])
    s.append("node_created", {**rebuilt, "generation": 1})

    st = fold(s.read_all())
    assert st.node_concepts == {0: ["classifier/finding"]}
    assert st.node_concept_provenance == {0: "classifier"}
    assert st.node_concepts_at_vocab == {0: 12}


def test_changed_non_propose_rebuild_discards_stale_classifier_receipt(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["researcher/old"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/old"], "at_vocab": 9})
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    changed = _created(0, ["researcher/new"])
    changed["idea"]["rationale"] = "a different classifier subject"
    s.append("node_created", {**changed, "generation": 1})

    st = fold(s.read_all())
    assert st.node_concepts == {0: ["researcher/new"]}
    assert st.node_concept_provenance == {0: "researcher-authored"}
    assert st.node_concepts_at_vocab == {}


def test_changed_non_propose_rebuild_discards_stale_authored_receipt(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["researcher/old"]))
    s.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    changed = _created(0, None)
    changed["idea"]["rationale"] = "a replacement idea without authored taxonomy"
    s.append("node_created", {**changed, "generation": 1})

    st = fold(s.read_all())
    assert st.nodes[0].idea.rationale == "a replacement idea without authored taxonomy"
    assert st.node_concepts == {}
    assert st.node_concept_provenance == {}
    assert st.node_concepts_at_vocab == {}


def test_propose_reset_clears_prior_concepts_and_provenance(tmp_path):
    # A propose reset starts a new idea lifecycle. Its old authored claim cannot leak into the rebuilt
    # idea when that new idea carries no concepts.
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl"]))
    s.append("node_reset", {"node_id": 0, "from_stage": "propose"})
    s.append("node_created", {**_created(0, None), "generation": 1})
    st = fold(s.read_all())
    assert 0 not in st.node_concepts
    assert 0 not in st.node_concept_provenance


def test_authored_concepts_replay_stable(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["b/x", "a/y"]))
    events = s.read_all()
    assert fold(events).node_concepts == fold(events).node_concepts == {0: ["b/x", "a/y"]}
    assert fold(events).node_concept_provenance == {0: "researcher-authored"}


def test_cadence_retags_authored_claim_and_stamps_classifier_generation(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["researcher/claim"]))
    s.append("node_created", _created(1, None))
    s.append("node_concepts", {"node_id": 1, "concepts": ["classifier/known"], "at_vocab": 4})
    state = fold(s.read_all())
    captured = {}

    from looplab.search import concept_graph as cg
    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"loss/decoupled-contrastive"}),
            1: frozenset({"classifier/known"})}

    def fake_build(*args, known_tags=None, **kwargs):
        captured["known_tags"] = dict(known_tags or {})
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            "coverage": cg.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cg, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = SimpleNamespace(_reflect_client=lambda: object(), store=store)
    assert StrategyCadenceMixin._concept_coverage_snapshot(host, state) is not None

    assert captured["known_tags"] == {1: ["classifier/known"]}
    emitted = [data for event_type, data in store.events if event_type == "node_concepts"]
    assert any(row["node_id"] == 0 and row["generation"] == 0 for row in emitted)


def test_cadence_repairs_partial_classifier_instead_of_caching_subset(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.append("node_created", _created(0, None))
    s.append("node_created", _created(1, None))
    s.append("node_concepts", {
        "node_id": 0, "concepts": [f"axis/c{i:03d}" for i in range(65)],
        "mode": "llm", "at_vocab": 4,
    })
    s.append("node_concepts", {
        "node_id": 1, "concepts": ["classifier/known"], "mode": "llm", "at_vocab": 4,
    })
    state = fold(s.read_all())
    assert state.node_concept_materialization_receipts[0]["status"] == "partial"
    captured = {}

    from looplab.search import concept_graph as cg
    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"classifier/repaired"}),
            1: frozenset({"classifier/known"})}

    def fake_build(*args, known_tags=None, **kwargs):
        captured["known_tags"] = dict(known_tags or {})
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            "coverage": cg.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cg, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = SimpleNamespace(_reflect_client=lambda: object(), store=store)
    assert StrategyCadenceMixin._concept_coverage_snapshot(host, state) is not None

    assert captured["known_tags"] == {1: ["classifier/known"]}
    emitted = [data for event_type, data in store.events if event_type == "node_concepts"]
    assert any(row["node_id"] == 0 and row["concepts"] == ["classifier/repaired"]
               for row in emitted)


def test_cadence_never_retags_an_operator_edited_node(tmp_path, monkeypatch):
    # PART V cross-phase: an operator-edited node's tags are authoritative for THIS node, so the coverage
    # cadence must treat them as KNOWN and never re-tag. Two paths would otherwise leak: (1) the node is
    # excluded from `all_known` (fixed by including OPERATOR provenance), and (2) an operator node has NO
    # at_vocab receipt (fold pops it), so it reads as maximally stale (at_vocab=0) and is dropped from
    # `known` whenever any classifier node has a higher at_vocab — re-tagged every cadence, and the fold
    # then REJECTS the re-tag (never converges). Node 1 below carries at_vocab=4 precisely to trip that
    # staleness path for the un-versioned operator node 0 unless it is excluded from the stale candidates.
    s = _store(tmp_path)
    s.append("node_created", _created(0, None))
    s.append("node_created", _created(1, None))
    s.append("node_concepts", {"node_id": 1, "concepts": ["classifier/known"], "at_vocab": 4})
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/hand-tag"]})
    state = fold(s.read_all())
    assert state.node_concept_provenance == {1: "classifier", 0: "operator-edited"}
    captured = {}

    from looplab.search import concept_graph as cg
    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"operator/hand-tag"}), 1: frozenset({"classifier/known"})}

    def fake_build(*args, known_tags=None, **kwargs):
        captured["known_tags"] = dict(known_tags or {})
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            "coverage": cg.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cg, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = SimpleNamespace(_reflect_client=lambda: object(), store=store)
    assert StrategyCadenceMixin._concept_coverage_snapshot(host, state) is not None

    # The operator node (0) AND the classifier node (1) are both KNOWN — the operator node survives the
    # staleness filter despite its at_vocab=0, so neither enters the LLM todo set.
    assert captured["known_tags"] == {0: ["operator/hand-tag"], 1: ["classifier/known"]}
    emitted = [data for event_type, data in store.events if event_type == "node_concepts"]
    assert not any(row["node_id"] == 0 for row in emitted)   # operator node never re-tagged
