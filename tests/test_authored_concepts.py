"""Researcher-authored concepts remain visible but carry explicit, replay-stable provenance.

A later `node_concepts` classifier event refines them last-write-wins; admission consumers can therefore
distinguish a proposal claim from independent classifier evidence without migrating old event logs.
"""
import pytest

from looplab.core.models import (Idea, IdeaEmission, Node, authored_node_concepts,
                                 classifier_verified_node_concepts, durable_idea_payload)
from looplab.engine.concept_cadence import ConceptCadenceMixin
from looplab.events.concept_authorship import concept_authorship_report
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


class _CadenceHost(ConceptCadenceMixin):
    """The engine surface `_concept_coverage_snapshot` actually needs: a reflect client and a store.

    A REAL mixin instance rather than a `SimpleNamespace`, because the producer drives its tagging /
    edge / hypothesis steps through `self` (doc 25 EC-09) — a duck host would only exercise whichever
    steps happened to be inlined on the day it was written.
    """

    def __init__(self, store):
        self.store = store

    def _reflect_client(self):
        return object()


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
    assert schema["additionalProperties"] is False
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
        {"operator": "draft", "concept_mode": "delta", "concepts_aded": ["model/a"]},
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

    from looplab.search import concept_analytics as ca
    from looplab.search import concept_graph as cg
    from looplab.search import concept_map as cm
    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"loss/decoupled-contrastive"}),
            1: frozenset({"classifier/known"})}

    def fake_build(*args, known_tags=None, **kwargs):
        captured["known_tags"] = dict(known_tags or {})
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            "coverage": ca.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cm, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = _CadenceHost(store)
    assert host._concept_coverage_snapshot(state) is not None

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

    from looplab.search import concept_analytics as ca
    from looplab.search import concept_graph as cg
    from looplab.search import concept_map as cm
    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"classifier/repaired"}),
            1: frozenset({"classifier/known"})}

    def fake_build(*args, known_tags=None, **kwargs):
        captured["known_tags"] = dict(known_tags or {})
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            "coverage": ca.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cm, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = _CadenceHost(store)
    assert host._concept_coverage_snapshot(state) is not None

    assert captured["known_tags"] == {1: ["classifier/known"]}
    emitted = [data for event_type, data in store.events if event_type == "node_concepts"]
    assert any(row["node_id"] == 0 and row["concepts"] == ["classifier/repaired"]
               for row in emitted)


def test_cadence_persists_per_node_fallback_provenance(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.append("node_created", _created(0, None))
    s.append("node_created", _created(1, None))
    s.append("node_created", _created(2, None))
    state = fold(s.read_all())

    from looplab.search import concept_analytics as ca
    from looplab.search import concept_graph as cg
    from looplab.search import concept_map as cm
    graph = cg.dense_retrieval_skeleton()
    for i in range(70):
        graph.ensure(f"axis/c{i:03d}")
    tags = {
        0: frozenset({"loss/decoupled-contrastive"}),
        1: frozenset({"hyperparameter/temperature"}),
        2: frozenset(f"axis/c{i:03d}" for i in range(70)),
    }

    def fake_build(*args, **kwargs):
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            # Exercise both JSON/string keys and a defensive over-wide classifier row.
            "raw_tag_modes": {"0": "offline-heuristic", "1": "llm", "2": "llm"},
            "coverage": ca.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cm, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = _CadenceHost(store)
    assert host._concept_coverage_snapshot(state) is not None

    emitted = {data["node_id"]: data for event_type, data in store.events
               if event_type == "node_concepts"}
    assert emitted[0]["mode"] == "offline-heuristic"
    assert emitted[1]["mode"] == "llm"
    assert emitted[2]["mode"] == "offline-heuristic"
    assert emitted[2]["concepts"] == [f"axis/c{i:03d}" for i in range(64)]


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

    from looplab.search import concept_analytics as ca
    from looplab.search import concept_graph as cg
    from looplab.search import concept_map as cm
    graph = cg.dense_retrieval_skeleton()
    tags = {0: frozenset({"operator/hand-tag"}), 1: frozenset({"classifier/known"})}

    def fake_build(*args, known_tags=None, **kwargs):
        captured["known_tags"] = dict(known_tags or {})
        return {
            "graph": graph,
            "tags": tags,
            "raw_tags": tags,
            "coverage": ca.concept_coverage(state, graph, tags),
            "important_uncovered": [],
            "consolidated": {},
            "mode": "llm",
        }

    monkeypatch.setattr(cm, "build_concept_map", fake_build)

    class CaptureStore:
        def __init__(self): self.events = []
        def append(self, event_type, data): self.events.append((event_type, data))

    store = CaptureStore()
    host = _CadenceHost(store)
    assert host._concept_coverage_snapshot(state) is not None

    # The operator node (0) AND the classifier node (1) are both KNOWN — the operator node survives the
    # staleness filter despite its at_vocab=0, so neither enters the LLM todo set.
    assert captured["known_tags"] == {0: ["operator/hand-tag"], 1: ["classifier/known"]}
    emitted = [data for event_type, data in store.events if event_type == "node_concepts"]
    assert not any(row["node_id"] == 0 for row in emitted)   # operator node never re-tagged


# --------------------------------------------------------------------------- the AUTHORED record
# The classifier REWRITES a membership rather than adding to it, so before `node_concepts_authored`
# existed the proposer's own ids survived only in the raw log — and `events/digest.py::_folded_axes`
# forbids every read surface from resurrecting `idea.concepts` (rightly: a deliberately cleared node
# must not keep classifying under its old authored axis). These drive the record itself: what it
# keeps, what it is not allowed to keep, and what the instrument over it computes.
def test_a_classifier_row_replaces_the_membership_and_keeps_the_authored_claim(tmp_path):
    """The measured case, in miniature: node 3's exactly-curated `regularization/r-drop` was replaced
    by the invented `regularization/rdrop`, and nothing folded held the original."""
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["regularization/r-drop"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["regularization/rdrop"], "at_vocab": 4})
    state = fold(s.read_all())

    assert state.node_concepts == {0: ["regularization/rdrop"]}      # the membership is the classifier's
    assert state.node_concept_provenance == {0: "classifier"}
    assert authored_node_concepts(state, 0) == ["regularization/r-drop"]
    # …and the authored claim is NOT evidence: the one door admission crosses still answers the
    # classifier's set, so nothing gained an admission input from this record.
    assert classifier_verified_node_concepts(state, 0) == ["regularization/rdrop"]


def test_the_authored_record_survives_a_retag_in_either_order(tmp_path):
    """Invariant 5, over the orders the fold actually admits. A classifier row that ARRIVES FIRST is
    dropped by the pre-existing unknown-node rule (`_on_node_concepts` fails closed on a node it has
    not seen), so the two orders differ in the MEMBERSHIP — which is exactly why the authored write
    may not be a branch of the membership. It is a fact about the Idea and answers identically."""
    def _folded(order):
        s = _store(tmp_path / order)
        rows = {"c": ("node_created", _created(0, ["a/authored"])),
                "t": ("node_concepts", {"node_id": 0, "concepts": ["b/tagged"]})}
        for key in order:
            s.append(*rows[key])
        return fold(s.read_all())
    (tmp_path / "ct").mkdir()
    (tmp_path / "tc").mkdir()
    created_first, tagged_first = _folded("ct"), _folded("tc")
    for order, state in (("ct", created_first), ("tc", tagged_first)):
        assert authored_node_concepts(state, 0) == ["a/authored"], order
    assert created_first.node_concepts[0] == ["b/tagged"]     # the retag landed
    assert tagged_first.node_concepts[0] == ["a/authored"]    # …the early row never did


def test_an_operator_edit_cannot_erase_the_authored_claim(tmp_path):
    """The operator owns the MEMBERSHIP (`concept_tag_edited` is authoritative and the classifier
    yields to it). It does not own what the proposer said, which is a fact about the Idea."""
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["a/authored"]))
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/hand-tag"]})
    state = fold(s.read_all())
    assert state.node_concepts == {0: ["operator/hand-tag"]}
    assert state.node_concept_provenance == {0: "operator-edited"}
    assert authored_node_concepts(state, 0) == ["a/authored"]


def test_a_propose_reset_drops_the_authored_claim_with_its_idea(tmp_path):
    """The record follows the IDEA, so the boundary that abandons an Idea must take it — otherwise a
    re-proposed node reports the previous proposal's concepts as its author's claim forever."""
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["a/authored"]))
    s.append("node_reset", {"node_id": 0, "from_stage": "propose"})
    assert authored_node_concepts(fold(s.read_all()), 0) == []
    # …and an IMPLEMENT reset keeps it: the same idea is being re-developed, not re-proposed.
    s2 = _store(tmp_path / "impl")
    s2.append("node_created", _created(0, ["a/authored"]))
    s2.append("node_reset", {"node_id": 0, "from_stage": "implement"})
    assert authored_node_concepts(fold(s2.read_all()), 0) == ["a/authored"]


def test_a_replacement_idea_replaces_the_authored_claim_even_under_a_classifier_receipt(tmp_path):
    """The concept envelope is EXCLUDED from the subject-equality test, so a re-emitted `node_created`
    may legitimately carry a new authored set while a classifier receipt keeps the membership. The
    authored record must follow the newest authoring, not freeze at the first one."""
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["a/first"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["b/tagged"]})
    s.append("node_created", _created(0, ["a/second"]))
    state = fold(s.read_all())
    assert state.node_concepts == {0: ["b/tagged"]}          # the receipt is protected, as before
    assert state.node_concept_provenance == {0: "classifier"}
    assert authored_node_concepts(state, 0) == ["a/second"]


def test_a_node_that_authored_nothing_has_no_authored_record(tmp_path):
    """Absence is the honest answer for a proposal with no concept envelope — the state every log
    written before this record is in. An explicit `full: []` is the OTHER statement and is kept."""
    s = _store(tmp_path)
    s.append("node_created", _created(0, None))
    s.append("node_concepts", {"node_id": 0, "concepts": ["b/tagged"]})
    empty = _created(1, [])
    empty["idea"]["concept_mode"] = "full"
    s.append("node_created", empty)
    state = fold(s.read_all())
    assert 0 not in state.node_concepts_authored
    assert state.node_concepts_authored[1] == []


def test_a_delta_node_keeps_its_authored_operands_where_they_already_live(tmp_path):
    """`node_concepts_authored` is FULL SETS ONLY on purpose: a delta node's authored operands are
    already durable in `node_concept_deltas`, which no classifier writer clears, so a second copy
    would be a record that can drift from the one the materialization reads."""
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    delta = _created(0, None)
    delta["idea"]["concept_mode"] = "delta"
    delta["idea"]["concepts_added"] = ["a/added"]
    s.append("node_created", delta)
    s.append("node_concepts", {"node_id": 0, "concepts": ["b/tagged"]})
    state = fold(s.read_all())
    assert 0 not in state.node_concepts_authored
    assert state.node_concept_deltas[0] == {"added": ["a/added"], "removed": []}
    assert state.node_concepts == {0: ["b/tagged"]}


def test_the_authorship_instrument_counts_survival_and_resolves_renames(tmp_path):
    """The instrument makes the hand measurement a command. The rename half is the interpretation it
    applies: an id a later consolidation RENAMED is the same concept, so it must not be reported as a
    classifier replacement — that is the only reason both sides are canonicalized before comparison."""
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["a/kept", "a/dropped"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["a/kept", "c/invented"]})
    s.append("node_created", _created(1, ["b/old-spelling"]))
    s.append("node_concepts", {"node_id": 1, "concepts": ["b/new-spelling"]})
    s.append("concept_consolidation", {"rename": {"b/old-spelling": "b/new-spelling"}})
    s.append("node_created", _created(2, None))              # no authored claim -> not a row at all
    report = concept_authorship_report(fold(s.read_all()))

    assert [row["node_id"] for row in report["nodes"]] == [0, 1]
    assert report["nodes"][0]["replaced"] == ["a/dropped"]
    assert report["nodes"][1]["replaced"] == []              # a rename is not a replacement
    assert report["authored_nodes"] == 2 and report["reclassified_nodes"] == 2
    assert report["authored_ids"] == 3 and report["survived_ids"] == 2
    assert report["survival_rate"] == pytest.approx(2 / 3)
    assert report["by_producer"] == {"classifier": 2}
    # A run that authored nothing reports `None`, never 0.0 — "no claim was made" and "every claim
    # was replaced" are opposite readings and the rate may not conflate them.
    bare = _store(tmp_path / "bare")
    bare.append("node_created", _created(0, None))
    assert concept_authorship_report(fold(bare.read_all()))["survival_rate"] is None


def test_the_instrument_bounds_the_list_and_not_the_totals(tmp_path):
    s = _store(tmp_path)
    for node_id in range(5):
        s.append("node_created", _created(node_id, [f"a/c{node_id}"]))
    report = concept_authorship_report(fold(s.read_all()), limit=2)
    assert len(report["nodes"]) == 2 and report["truncated"] == 3
    assert report["authored_nodes"] == 5 and report["authored_ids"] == 5
