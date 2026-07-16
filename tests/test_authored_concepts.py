"""Researcher-authored concepts remain visible but carry explicit, replay-stable provenance.

A later `node_concepts` classifier event refines them last-write-wins; admission consumers can therefore
distinguish a proposal claim from independent classifier evidence without migrating old event logs.
"""
from types import SimpleNamespace

import pytest

from looplab.core.models import Idea
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
    (tmp_path / "ab").mkdir(); (tmp_path / "ba").mkdir()
    assert _fold("ab") == {"x": "a"}
    assert _fold("ba") == {"x": "a"}                      # reversed order -> identical


def test_at_vocab_rejects_bool(tmp_path):
    # bool is an int subclass; `at_vocab: true` must NOT be stored as a vocabulary size of 1.
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["a/x"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/x"], "at_vocab": True})
    assert fold(s.read_all()).node_concepts_at_vocab == {}   # bool rejected, no receipt


def test_idea_concepts_round_trip():
    idea = Idea(operator="draft", params={"seed": 1.0}, rationale="try dcl",
                concepts=["loss/contrastive/dcl", "regularization/r-drop"])
    d = idea.model_dump()
    assert d["concepts"] == ["loss/contrastive/dcl", "regularization/r-drop"]
    assert Idea(**d).concepts == idea.concepts            # rides on the Idea through the event log
    assert Idea(operator="draft", params={}, rationale="r").concepts == []   # default empty


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
