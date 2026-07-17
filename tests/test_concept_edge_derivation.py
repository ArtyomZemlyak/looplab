"""F3: an offline / retro-tagged run has no EV_CONCEPT_EDGE, so the frame derives co_occurs edges from
co-tagging at read time — giving those runs a real co_occurs lens with no new events, matching what a
live run would have persisted. A run that DID persist edges keeps them (derivation is a fallback only).
"""
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.serve.concept_frame import bounded_inputs
from looplab.search.concept_graph import default_lenses


def _store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dense-retrieval", "goal": "g", "direction": "max"})
    return s


def _created(node_id, concepts):
    return {"node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"seed": float(node_id)}, "rationale": "r",
                     "concepts": concepts}}


def test_co_occurs_is_derived_when_no_edges_were_persisted(tmp_path):
    s = _store(tmp_path)
    # two nodes co-tag loss/dcl + hyperparameter/temperature -> the pair co-occurs on 2 nodes.
    s.append("node_created", _created(0, ["loss/dcl", "hyperparameter/temperature"]))
    s.append("node_created", _created(1, ["loss/dcl", "hyperparameter/temperature"]))
    inp = bounded_inputs(fold(s.read_all()), default_lenses())
    co = {(k[0], k[2]): v for k, v in inp["edges"].items() if k[1] == "co_occurs"}
    assert ("hyperparameter/temperature", "loss/dcl") in co       # sorted src<dst
    assert co[("hyperparameter/temperature", "loss/dcl")]["confidence"] == 2.0
    assert co[("hyperparameter/temperature", "loss/dcl")]["provenance"] == "co-tag (derived)"


def test_single_cooccurrence_is_noise_and_not_derived(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "hyperparameter/temperature"]))   # pair on 1 node
    inp = bounded_inputs(fold(s.read_all()), default_lenses())
    assert not any(k[1] == "co_occurs" for k in inp["edges"])      # <2 nodes -> not derived


def test_persisted_edges_suppress_derivation(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "hyperparameter/temperature"]))
    s.append("node_created", _created(1, ["loss/dcl", "hyperparameter/temperature"]))
    # a live run persisted its own edge -> derivation must NOT run (fallback only), leaving the real edges.
    s.append("concept_edge", {"src": "loss/dcl", "rel": "co_occurs", "dst": "hyperparameter/temperature",
                              "provenance": "evidenced", "confidence": 5})
    inp = bounded_inputs(fold(s.read_all()), default_lenses())
    co = [v for k, v in inp["edges"].items() if k[1] == "co_occurs"]
    assert len(co) == 1 and co[0]["provenance"] != "co-tag (derived)"   # the persisted edge, not derived
